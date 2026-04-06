# 第四篇：有限窗口，无限任务

128K token 听起来很多。但我跟你算一笔账。

一个稍微复杂的编程任务——"重构这个模块的错误处理"。LLM 先要读相关文件：三四个文件，每个几百行，大概 4000-8000 token。然后它改文件、跑测试、看报错、再改。每一轮工具调用的输出少则几百 token（读一个小文件），多则几千 token（跑一次 `npm test` 的完整日志）。十几轮下来，光是工具输出可能就占了 50K-80K token。再加上系统提示词（通常 3K-5K）、历史对话、LLM 的回复，128K 就快见底了。

我第一次用 Claude Code 干大活的时候，它突然说"我需要压缩上下文了"，我以为就是简单截断。读了源码才发现，Claude Code 的上下文管理是我见过的最精细的——**四种不同粒度的压缩机制同时工作**，而且按严格顺序触发，前面能搞定就不动后面。

---

## 四层策略

我在源码里找到了四个相关的 Feature Flag：

```
HISTORY_SNIP          → 第一层：裁工具输出
CACHED_MICROCOMPACT   → 第二层：缓存式 LLM 摘要
CONTEXT_COLLAPSE      → 第三层：结构化归档
REACTIVE_COMPACT      → 第四层：后台自动压缩（即 Autocompact）
```

### 第一层：HISTORY_SNIP — 删噪声

最容易膨胀的不是用户消息，也不是 LLM 回复，而是**工具输出**。

一个 `grep` 搜索返回了 200 行匹配结果，LLM 可能只用了其中 3 行。剩下 197 行就是纯噪声——占着宝贵的上下文位置，但对后续决策毫无用处。

HISTORY_SNIP 做的事情：遍历历史消息中所有 `role: "tool"` 的结果，如果内容超过阈值，替换成精简版。精简策略是保留前几行和后几行（最有用的信息通常在开头的命令回显和结尾的错误/总结信息里），中间用 `[snipped N lines]` 替代。

```
之前：
  [tool result] (482 lines)
  src/auth.py:12: import jwt
  src/auth.py:34: jwt.decode(token, SECRET)
  src/auth.py:56: jwt.encode(payload, SECRET)
  ... (479 more lines of grep results)

之后：
  [tool result] (snipped to 6 lines)
  src/auth.py:12: import jwt
  src/auth.py:34: jwt.decode(token, SECRET)
  src/auth.py:56: jwt.encode(payload, SECRET)
  [snipped 476 lines]
  src/utils/crypto.py:89: verify_jwt(token)
  src/utils/crypto.py:102: refresh_jwt(token)
```

这是成本最低的压缩——不需要调 LLM，不会丢失关键信息（头尾保留），效果立竿见影。一次 grep 的输出从 2000 token 压到 100 token，什么都没丢。

CoreCoder 的 `context.py` 里的 `_snip_tool_outputs()` 是这一层的实现。

### 第二层：CACHED_MICROCOMPACT — 花钱压缩

如果第一层裁完还是太长（因为轮次多，每轮都有有效信息），第二层启动。

这一层拿老的对话片段（比如最早的 10 轮交互），发给 LLM 做一次专门的摘要调用：

```
System: "把这段对话压缩成关键信息。
         保留：文件路径、做出的决策、遇到的错误、当前任务状态。
         丢弃：冗长的工具输出、重复的讨论、格式化的代码。"

User: [10 轮旧对话的拼接文本]
```

LLM 返回的摘要通常只有原对话的 1/5 到 1/10。然后用这个摘要替换那 10 轮旧对话。

"Cached" 指的是**摘要会被缓存**。如果下次又需要压缩，上次的摘要直接用，不重新调 LLM。这避免了每次循环迭代都花钱做摘要。

这一层还利用了 Anthropic API 的 `cache_deleted_input_tokens` 能力——在 API 的缓存层面标记某些 token 为"已删除"，不占用 cached token 配额。等于是在不改变消息内容的情况下，把缓存里的空间也省出来了。

CoreCoder 的 `_summarize_old()` 实现了核心逻辑：用同一个 LLM 做摘要，如果 LLM 调用失败就 fallback 到基于正则的关键信息提取（文件路径、错误信息）。

### 第三层：CONTEXT_COLLAPSE — 结构化归档

如果前两层都做了还是不够（用户在一个超长会话里处理了多个不同任务），第三层启动。

CONTEXT_COLLAPSE 和第二层的区别：第二层只是把旧对话变短，结构还在。第三层把旧对话**完全替换**成一段结构化的总结，类似 Git log——每一轮做了什么、结论是什么、修改了哪些文件。

```
[Context collapsed - 30 turns summarized]

Turn 1-5: Read auth module, identified 3 functions without error handling
Turn 6-12: Added try/except to verify_token(), refresh_token(), decode_payload()
Turn 13-15: Ran tests, found regression in test_expired_token
Turn 16-20: Fixed test, all 47 tests passing
Turn 21-25: Updated API documentation for new error responses
Turn 26-30: Code review suggestions applied

Files modified: src/auth.py, tests/test_auth.py, docs/api.md
Current state: All changes committed, ready for PR
```

这一层会丢细节。但比简单截断好：截断是按时间顺序砍（最早的先丢），而 CONTEXT_COLLAPSE 至少保留了每轮的决策要点。LLM 不知道具体代码改了什么，但知道"之前改过 auth.py 的错误处理"，不会重复做已经做过的事。

CoreCoder 的 `_hard_collapse()` 是这一层。

### 第四层：Autocompact — 自动驾驶

Claude Code 有一个 `/compact` 命令让用户主动触发压缩。但 REACTIVE_COMPACT（也叫 Autocompact）是**自动的**：

系统在每次调 API 前检查当前 token 用量。如果接近上限，在用户无感知的情况下自动执行压缩。用户不需要关心 token 管理。

CoreCoder 的 `maybe_compress()` 就是这个机制——在 `agent.chat()` 每次循环迭代开头和每轮工具执行后自动检查。

---

## 压缩的工程权衡

做上下文压缩最难的不是"怎么压"，而是"压什么"。

**什么信息绝对不能丢？**

文件路径——LLM 需要知道之前编辑了哪些文件，不然可能重复读取或覆盖。做出的关键决策——"用户说不要改 config.yaml"这种指令如果被压掉了，LLM 就可能违反。未解决的错误——正在处理的 bug 信息如果丢了，LLM 会从头排查。

Claude Code 的摘要 prompt 里明确列出了这些保留项。CoreCoder 也是。

**摘要本身占多少 token？**

如果摘要写太长，压缩就白做了。Claude Code 通过 `max_tokens` 参数限制摘要调用的输出长度。CoreCoder 把摘要调用的 prompt 限制在 15000 字符以内。

**压缩后 LLM 会不会"忘记"承诺？**

这是最大的风险。你说"帮我改完 auth 模块后跑一次全量测试"，前半句被 LLM 做完了，后半句在压缩时被摘要吸收了，LLM 可能就忘了跑测试。

Claude Code 的策略是在摘要 prompt 里强调"保留用户明确要求的操作和约束"。但这不是 100% 可靠的——摘要 LLM 本身也可能犯错。这是一个已知的不完美，目前没有完美解。

**用什么模型做摘要？**

Claude Code 用的是同一个模型。一次摘要调用的成本 ≈ 一次正常对话的成本。CoreCoder 也是这样。如果你想省钱，可以用一个便宜的模型专门做摘要（比如用 DeepSeek 做主力但用 GPT-4o-mini 做摘要）。

---

## 信息的"保质期"不一样

读完上下文管理这部分代码，我最大的感触是：**不同信息的保质期不一样，应该用不同的策略处理。**

工具的中间输出几轮之后就没用了（grep 结果在你找到要改的文件后就不再需要）。但用户描述的需求背景可能整个会话都要保留。LLM 上一轮的思考过程可以扔掉，但它做出的决策（"我选择用 try/except 而不是 if/else"）应该保留。

Claude Code 的四层策略本质上就是在按"保质期"分级处理：第一层丢最短命的（冗长的工具输出），最后一层才动最长命的（对话结构和决策历史）。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 4 篇。配套实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)
