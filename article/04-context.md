# 用有限的窗口，扛住一个长任务

agent 有一个绕不过去的物理约束：上下文窗口就那么大。

而编码任务偏偏极其能产 token。模型读一个一千行的文件，那一千行连同行号全进了历史；跑一次测试，几百行输出全进了历史；grep 一下，几十个匹配全进了历史。一个稍微像样的任务转上十几轮，几万 token 就没了。窗口一旦塞满，要么 API 报错，要么你得砍历史，而砍历史砍不好，agent 就会「忘事」，前面读过的文件转头又读一遍，刚做过的决定又推翻重来。

所以怎么在有限窗口里装下一个长任务，是 agent 工程里最硬核的子问题之一。这一篇看 `corecoder/context.py`（222 行）怎么解。

## 分层，从轻到重

Claude Code 的策略公开拆解里是四层，从最廉价的处理逐级升到最激进的。CoreCoder 蒸馏成三层，思路一致：能用便宜手段省出来的空间，绝不动用贵手段。三层分别在窗口用到一定比例时才触发：

```python
self._snip_at = int(max_tokens * 0.50)      # 50% -> 截断臃肿的工具输出
self._summarize_at = int(max_tokens * 0.70)  # 70% -> LLM 摘要旧对话
self._collapse_at = int(max_tokens * 0.90)   # 90% -> 硬折叠，最后手段
```

`maybe_compress` 是调度者，它先估算当前用了多少 token，然后从轻到重地按需施加每一层，每施加一层就重新估一次，够了就停：

```python
def maybe_compress(self, messages, llm=None) -> bool:
    current = estimate_tokens(messages)
    compressed = False

    if current > self._snip_at:
        if self._snip_tool_outputs(messages):
            compressed = True
            current = estimate_tokens(messages)

    if current > self._summarize_at and len(messages) > 10:
        if self._summarize_old(messages, llm, keep_recent=8):
            compressed = True
            current = estimate_tokens(messages)

    if current > self._collapse_at and len(messages) > 4:
        self._hard_collapse(messages, llm)
        compressed = True

    return compressed
```

上一篇和上上篇里那两处 `self.context.maybe_compress(...)`，调的就是这里。它在每次发请求前、每轮工具执行后都喊一声，但绝大多数时候窗口没满，这函数什么都不做就返回了。压缩是惰性的，只在真要撞墙时才花力气。

至于 token 怎么估的，`estimate_tokens` 用了一个糙到可爱的办法：字符数除以 3。

```python
def _approx_tokens(text: str) -> int:
    """Rough token count, roughly 3 chars per token for mixed en/zh content."""
    return len(text) // 3
```

它不准，真要准得上 tokenizer。但压缩判断要的不是精确值，是「现在大概到几成了」这个量级感，除以 3 对中英混合内容够用了，而且零依赖、零开销。在「够用就好」和「精确但重」之间，这里明确选了前者。什么地方该糙、什么地方该较真，是工程品味的一部分。

## 第一层：旧的工具输出，是有保质期的

第一层 `_snip_tool_outputs` 是最便宜的，不调模型，纯文本处理。它把超过 1500 字符的工具结果，截成只留头三行和尾三行：

```python
content = m.get("content", "")
if len(content) <= 1500:
    continue
lines = content.splitlines()
if len(lines) <= 6:
    continue
snipped = (
    "\n".join(lines[:3])
    + f"\n... ({len(lines)} lines, snipped to save context) ...\n"
    + "\n".join(lines[-3:])
)
m["content"] = snipped
```

这一层背后有个我觉得很漂亮的洞察：工具输出是有保质期的。

二十轮之前那次 grep 吐出来的两百行匹配，在当时很有用，模型靠它定位了代码。但到了现在，模型早就用完那个结果、做完了相应的修改，那两百行就成了纯粹的占位垃圾，留着它只是白占窗口。把它截成头尾几行，既保留了「这里曾经查过一次、大致是这些文件」的线索，又把绝大部分死重量扔掉了。新鲜的信息值钱，陈旧的信息廉价，压缩就该优先压陈旧的。公开拆解里管这层叫 HISTORY_SNIP，干的是同一件事。

为什么截头尾而不是只截开头？因为命令输出最有用的信息常常在两端：开头是它在干什么，结尾是结果和报错。中间那一大坨过程往往可以丢。这个「留头尾、弃中间」的选择，和上一篇 bash 输出截断的逻辑是一脉相承的。

## 第二层：让模型给旧对话写个摘要

第一层只压工具输出，压不动对话本身。当窗口涨到 70%，第二层 `_summarize_old` 上场：把旧的对话整段交给模型写一个摘要，只留最近 8 条消息原样不动。

```python
split = self._safe_split(messages, keep_recent)
old = messages[:split]
tail = messages[split:]

summary = self._get_summary(old, llm)

messages.clear()
messages.append({
    "role": "user",
    "content": f"[Context compressed - conversation summary]\n{summary}",
})
messages.append({
    "role": "assistant",
    "content": "Got it, I have the context from our earlier conversation.",
})
messages.extend(tail)
```

旧对话被替换成一条「这是之前对话的摘要」的 user 消息，外加一条模型「我记下了」的 assistant 回应，然后接上原封不动的近期消息。摘要本身由 `_get_summary` 生成，它给模型的指令很聚焦：保留改过的文件路径、做过的关键决定、遇到的错误、当前任务状态；丢掉啰嗦的命令输出、代码清单、来回的废话。这正是一个长任务里真正需要被记住的东西。

如果没有可用的模型（或者摘要调用本身失败了），它退化成 `_extract_key_info`，用正则把文件路径和带 error 的行抽出来拼一个粗摘要。又是优雅降级：宁可给个糙摘要，也不让压缩这一步把整个会话拖垮。

## 那个一定会咬你的坑：孤儿 tool 消息

现在讲这一篇的重头戏，也是我在打磨这个项目时实打实踩过的坑。

回想第一篇那条铁律：一条带 `tool_calls` 的 assistant 消息，后面必须跟着配对的 `tool` 回复，少一个 API 都会拒。压缩这件事的本质，是在历史的某个位置切一刀，前面的压掉、后面的留下。问题来了：如果这一刀正好切在一组工具调用的中间呢？

设想历史是这样一段：assistant 发起了工具调用、紧跟着是对应的 tool 回复。如果「保留最近 N 条」这个边界恰好落在 tool 回复上，那么被保留的尾巴就会以一条 tool 消息开头，而产生它的那条 assistant 消息被切到前面、压进摘要里没了。这条 tool 回复成了孤儿，它前面找不到对应的 tool_calls。下一次请求带着这个孤儿发出去，API 当场拒绝。你的压缩逻辑本来是来救场的，结果亲手把会话搞死了。

`_safe_split` 就是来防这个的。它在切分前，把边界往前挪，直到边界那条消息不再是 tool：

```python
@staticmethod
def _safe_split(messages: list[dict], keep_recent: int) -> int:
    """Index where the kept tail should start.

    Walk the boundary back so a 'tool' result is never separated from the
    assistant message whose tool_calls produced it - an orphaned tool
    message has no preceding tool_calls and OpenAI-compatible APIs reject it.
    """
    split = max(0, len(messages) - keep_recent)
    while split > 0 and messages[split].get("role") == "tool":
        split -= 1
    return split
```

就这么一个 `while` 循环往回退。逻辑短得不能再短，但少了它，压缩就是个定时炸弹，平时不响，偏在长对话、窗口吃紧、最不该出事的时候炸。第二层和第三层切分时都走 `_safe_split`，不直接用 `len - keep_recent`。

这个坑值得你记住，因为它有一切「隐蔽 bug」的特征：它依赖一个跨越多条消息的不变式（tool 必须紧跟 tool_calls），这个不变式没写在任何一处显眼的地方，平时也不触发，只在「切分点恰好落在工具调用中间」这个特定时机才暴露。这种 bug 你很难靠盯着单个函数看出来，得在脑子里同时装着「压缩逻辑」和「API 的配对约束」两件事，才能意识到它们会在某个角落打架。CoreCoder 用两个测试把这个不变式钉死了，`test_safe_split_never_orphans_a_tool_message` 验切分点不落在 tool 上，`test_compress_never_leaves_an_orphan_tool_reply` 验整轮压缩后每条 tool 回复都还紧跟着它的 tool_calls。写这种测试，本质上是把一条「藏在脑子里的不变式」固化成代码，让它以后别再被人不小心破坏。

## 第三层：最后手段

窗口涨到 90%，说明前两层都没压够，第三层 `_hard_collapse` 是急刹车，只保留最后几条消息加一个摘要，其余全部折叠掉。它同样走 `_safe_split` 保证不留孤儿。这层很少触发，它存在的意义是「万一前两层都不够，至少别让 agent 直接撞墙死掉」，宁可丢掉较多上下文，也要让会话活下去。

## 和 Claude Code 的对照

四层和三层的差别，主要在 Claude Code 多了一层带缓存的微压缩（microcompact）和周期性的后台自动压缩，工程上更精细。但「分层、从轻到重、惰性触发、优先压陈旧信息」这套核心思路，两者完全一致。CoreCoder 把它压成三层，刚好够你看清每一层在解决什么、代价是什么，而不至于淹没在缓存和调度的细节里。

这一篇其实也回答了一个开头的问题：为什么 agent 偶尔会「忘事」。因为它真的会忘，压缩就是有损的，被摘要掉的细节就是丢了。好的压缩策略不是不丢，而是丢得聪明，优先丢那些已经没有保质期的东西。

## 这一篇带走什么

- 上下文窗口是 agent 最硬的物理约束，编码任务又极其能产 token，撞墙是迟早的事。
- 压缩要分层、从轻到重、惰性触发：能用纯文本截断省出来的，就别动用 LLM 摘要。
- 工具输出有保质期，陈旧的输出是优先压缩对象。新鲜信息值钱，陈旧信息廉价。
- 孤儿 tool 消息是个典型的隐蔽 bug：它依赖一条跨消息的不变式，平时不发作，只在切分点落在工具调用中间时炸。把这种不变式写成测试钉死，是对抗这类 bug 的正道。
- 压缩是有损的，agent「忘事」是它的固有代价。好策略不是不丢，是丢得聪明。

下一篇，我们回到第一篇里被跳过的另一处：当模型一次返回好几个工具调用，怎么并发地跑，以及它什么时候能开一个子 agent 替自己分担。
