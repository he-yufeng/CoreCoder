# 接入任意大模型，顺便把钱算清楚

前两篇讲的是循环和工具，也就是 agent 的手脚。这一篇讲大脑接口：模型怎么接进来，流式输出怎么处理，provider 抽风了怎么扛，以及一个被很多教程跳过、但你上线后第一天就会关心的问题，这一轮到底花了多少钱。

对应的文件是 `corecoder/llm.py`，332 行，是引擎里最大的单文件。它大，是因为它替你扛下了和真实 API 打交道时所有不优雅的部分。

## 一个赌注：大家都长得像 OpenAI

`llm.py` 开头的注释把整个设计的赌注讲明白了：

> 既然大多数 provider（DeepSeek、Qwen、Kimi、GLM、Ollama 等）都暴露了 OpenAI 兼容的接口，我们就直接用 openai SDK。换 provider 只需要改 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`，没了。

这个赌注在 2026 年基本是稳赢的。OpenAI 的 `/v1/chat/completions` 接口形状已经成了事实标准，国内外绝大多数模型服务要么原生兼容，要么提供一个兼容端点。所以 CoreCoder 的主力 `LLM` 类，本质上就是 openai 官方 SDK 外面薄薄一层包装。你想从 OpenAI 换到 DeepSeek，不改一行代码，改两个环境变量：

```bash
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com CORECODER_MODEL=deepseek-chat
```

这个「一套接口接住所有人」的选择，是 CoreCoder 能这么小的重要原因之一。它没有为每家 provider 写一个 adapter，因为它赌大家都会向 OpenAI 的形状靠拢。

那不兼容的怎么办？比如 AWS Bedrock、Google Vertex 这些。`llm.py` 末尾有一个 `LiteLLM` 子类兜底，后面会讲。先看主路径。

## 流式输出，比你想的麻烦一点

`LLM.chat()` 把消息发出去，开了 `stream=True`，然后一块一块地收。文本好办，来一块拼一块。真正麻烦的是工具调用，因为工具调用的参数也是流式吐出来的，一个调用的 JSON 参数会被切成好几个碎片，分散在多个 chunk 里到达，你得自己把它们按调用的次序缝回去。

CoreCoder 用一个以 index 为键的字典来缝：

```python
tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}

for chunk in stream:
    # ...
    if delta.tool_calls:
        for tc_delta in delta.tool_calls:
            idx = tc_delta.index
            if idx not in tc_map:
                tc_map[idx] = {"id": "", "name": "", "args": ""}
            if tc_delta.id:
                tc_map[idx]["id"] = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    tc_map[idx]["name"] = tc_delta.function.name
                if tc_delta.function.arguments:
                    tc_map[idx]["args"] += tc_delta.function.arguments
```

注意那个 `+=`。参数字符串是累加上去的，因为它一次只到一截。等流收完，再把每个调用累积的参数字符串 `json.loads` 成真正的字典：

```python
for idx in sorted(tc_map):
    raw = tc_map[idx]
    try:
        args = json.loads(raw["args"])
    except (json.JSONDecodeError, KeyError):
        args = {}
    parsed.append(ToolCall(id=raw["id"], name=raw["name"], arguments=args))
```

这里有个防御：如果累积出来的参数串不是合法 JSON（模型偶尔会吐出半截或者格式坏掉的参数），不是让整个流程崩掉，而是退化成一个空字典 `{}`，让这次调用带着空参数往下走，再由上一篇讲的参数校验去给模型一句「参数不对」的反馈。流式解析里，「坏数据要能优雅降级」是个反复出现的主题，因为流随时可能给你残缺的东西。

文本部分还支持一个 `on_token` 回调，每收到一截文本就喊一声，CLI 拿这个回调实现「打字机」效果，让你看着字一个个冒出来，而不是干等几十秒蹦出一整段。

## token 是怎么数出来的

要算钱，先得知道每次调用花了多少 token。CoreCoder 不自己估，它问 provider 要准数。办法是在请求里加一个 `stream_options`：

```python
params["stream_options"] = {"include_usage": True}
```

加了这个，provider 会在流的最后一个 chunk 里带回 `usage`，里头有 `prompt_tokens` 和 `completion_tokens`。代码在循环里接住它：

```python
if chunk.usage:
    # some providers send usage with null fields; coerce to 0 so the
    # running totals below don't blow up on int + None
    prompt_tok = chunk.usage.prompt_tokens or 0
    completion_tok = chunk.usage.completion_tokens or 0
```

那个 `or 0` 不是多余的。有些 provider 会把 `usage` 发回来但字段是 `null`，要是直接拿去和累计值相加，`int + None` 会抛异常，把整个会话搞挂。一句 `or 0` 把这种脏数据压平。这又是一个「真实 API 不会按文档那么干净」的例子，写包装层的人得替上层把这些坑都垫平。

数出来的 token 累加进 `total_prompt_tokens` 和 `total_completion_tokens`，CLI 的 `/tokens` 命令随时能查。

## provider 抽风了，自己扛

这是上一篇结尾埋的伏笔。主循环 `agent.py` 里没有任何重试逻辑，因为重试被下放到了这一层。`_call_with_retry` 负责扛住瞬时故障：

```python
def _call_with_retry(self, params: dict, max_retries: int = 3):
    """Retry on transient errors with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return self.client.chat.completions.create(**params)
        except (RateLimitError, APITimeoutError, APIConnectionError):
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            time.sleep(wait)
        except APIError as e:
            # retry 5xx server errors but not 4xx; base APIError has no
            # status_code so read it defensively
            status_code = getattr(e, "status_code", None)
            if status_code and status_code >= 500 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
```

逻辑是经典的指数退避：限流、超时、连接错误这类一看就是瞬时的故障，等 1 秒、2 秒、4 秒重试，最多三次还不行就放弃抛出去。服务端 5xx 也重试，但客户端 4xx（参数错、鉴权失败这种）绝不重试，因为重试一百次结果都一样，只是白等。

这段重试逻辑值得专门点出来，因为「这么小的项目应该没空管重试吧」是个很自然的误会，而事实正相反。重试不但在，还考虑得相当周到，连基类 `APIError` 可能没有 `status_code` 属性都用 `getattr` 防住了（不同版本的 SDK 异常层级不一样，硬取属性会炸）。

CoreCoder 没做的，是 Claude Code 那种更重的优雅降级：服务端持续 529 时自动切换到一个备用模型（fallbackModel），以及按美元数的预算上限。这两样它确实没做，但原因不是疏忽，而是取舍。fallback 模型涉及「哪个模型能替哪个」这种跨 provider 难以统一的策略，美元预算又得维护一张随时在变的价目表。CoreCoder 把能干净实现的（退避重试）做了，把会引入大量 provider 专属逻辑的（fallback、硬预算）留给了你按需去加。知道「它做了什么」和「它故意没做什么」，比笼统说一句「它很简陋」有用得多。

还有个容易被忽略的协调细节。`stream_options` 是 OpenAI 的扩展，有些 provider 不认，会回一个 400。CoreCoder 的处理是：

```python
try:
    stream = self._call_with_retry(params)
except BadRequestError:
    params.pop("stream_options", None)
    stream = self._call_with_retry(params)
```

捕获 `BadRequestError`（400），把 `stream_options` 去掉再试一次。注释里特意说明了，为什么只在这里 fallback、不在 `_call_with_retry` 里一起处理：因为 `_call_with_retry` 已经把瞬时错误重试过了，如果把这个「去参数重试」也塞进去，会让重试次数翻倍。两种重试，一种针对「参数被拒」、一种针对「瞬时故障」，原因不同，就分开放，别耦合。这种对「为什么这段代码在这里而不在那里」的交代，是读源码时最值钱的东西。

## 不兼容 OpenAI 的，交给 LiteLLM

`LiteLLM` 子类是给那些不走 OpenAI 兼容接口的 provider 准备的逃生通道。它继承 `LLM`，但绕开了父类构造函数里创建 openai client 的那步，转而把请求交给 `litellm` 这个库去路由，litellm 支持上百家 provider：

```python
class LiteLLM(LLM):
    def _call_with_retry(self, params, max_retries=3):
        import litellm
        params["drop_params"] = True   # 不支持的参数自动丢掉，别报错
        if self.api_key:
            params["api_key"] = self.api_key
        if self.base_url:
            params["api_base"] = self.base_url
        # ...同样的指数退避...
```

设了 `CORECODER_PROVIDER=litellm` 之后，你就能用 litellm 的模型串，比如 `anthropic/claude-3-haiku`、`bedrock/anthropic.claude-v2`、`vertex_ai/gemini-pro`。`drop_params=True` 是个贴心的开关，provider 不支持某个参数时自动丢掉而不是报错，省得你为每家去裁参数。绝大多数人用主路径就够了，LiteLLM 是那条「万一你的 provider 太特立独行」的后路。

## 把钱算出来

知道了 token，算钱就是查表乘一乘。`llm.py` 里有一张按百万 token 计价的表（输入价、输出价）：

```python
_PRICING = {
    "gpt-5.5": (5, 30),           # CoreCoder 默认就用它
    "deepseek-chat": (0.27, 1.10),
    "claude-sonnet-4-6": (3, 15),
    "kimi-k2.5": (0.6, 3),
    # ...
}
```

`estimated_cost` 这个 property 拿累计 token 乘上对应单价：

```python
@property
def estimated_cost(self) -> float | None:
    pricing = _PRICING.get(self.model)
    if not pricing:
        return None
    input_rate, output_rate = pricing
    return (
        self.total_prompt_tokens * input_rate / 1_000_000
        + self.total_completion_tokens * output_rate / 1_000_000
    )
```

注意返回类型是 `float | None`。表里没有的模型，它不瞎猜，老老实实返回 `None`，CLI 那边看到 `None` 就不显示价格，而不是编一个数字骗你。这是个小但重要的诚实：宁可说「我不知道」，也不给你一个看着精确、实则瞎编的成本。CLI 的 `/tokens` 命令把它显示出来：

```
Tokens: 12043 prompt + 3201 completion = 15244 total  (~$0.0621)
```

当然，这只是估算，价目表会过时，缓存折扣、批量定价这些它都没算。但对「我这一通操作大概烧了多少钱」这个量级的感知，它足够了。一个让你对成本有体感的 agent，和一个让你月底看账单才心惊的 agent，体验是两回事。

## 配置从哪来

最后串一下 `config.py`（55 行）。它从环境变量读配置，带一个合理的优先级：

```python
api_key = (
    os.getenv("CORECODER_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("DEEPSEEK_API_KEY")
    or ""
)
```

专属变量优先，然后退到通用的 `OPENAI_API_KEY`，再退到 `DEEPSEEK_API_KEY`。它还会从当前目录往上一直找到家目录，加载 `.env` 文件（`override=False`，不覆盖你已经设好的真环境变量）。这样你在项目目录放一个 `.env`，进来就能用，不用每次 export。小事，但顺手。

## 这一篇带走什么

- 「大多数 provider 都兼容 OpenAI 接口」这个赌注，让整个 provider 层薄到只是 openai SDK 的一层包装，换 provider 就是换两个环境变量。
- 流式输出里，工具调用的参数是分片到达的，得按 index 累加再 `json.loads`，还要对坏数据优雅降级。
- 重试该放在 provider 层，不该塞进主循环。指数退避只重试瞬时故障和 5xx，绝不重试 4xx。
- CoreCoder 做了退避重试，故意没做 fallback 模型和美元硬预算，因为后两者会拖进大量 provider 专属逻辑。分清「没做」和「故意没做」。
- 成本估算宁可对未知模型返回 `None`，也不编一个假精确的数字。诚实比好看重要。

下一篇，我们面对 agent 最硬的那道物理约束：上下文窗口就这么大，一个长任务怎么塞得下。
