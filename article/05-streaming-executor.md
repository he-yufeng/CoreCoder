# 第五篇：边想边做

人类程序员 debug 的时候不是"先想完所有步骤，再一步步执行"。更常见的模式是：看到报错，脑子里还在分析，手已经开始打开相关文件了。思考和行动是重叠的。

Claude Code 的 `StreamingToolExecutor`（530 行）让 AI Agent 也能这样工作。这是我在整个代码库里觉得工程上最精巧的一个组件。

---

## 先说问题

假设用户说"帮我重构 auth 模块"。LLM 决定需要先读三个文件：`auth.py`、`test_auth.py`、`config.py`。在大多数 Agent 框架里，执行流程是这样的：

```
时间线 ──────────────────────────────────────────►

LLM 生成中... (2秒)
├─ "我需要先看看这几个文件"
├─ tool_use: read_file("auth.py")
├─ tool_use: read_file("test_auth.py")
└─ tool_use: read_file("config.py")
                                    │
                                    ▼ (LLM 生成完毕)
                              解析 tool_calls
                                    │
                                    ▼
                              执行 read_file("auth.py")     200ms
                              执行 read_file("test_auth.py") 150ms
                              执行 read_file("config.py")   100ms
                                                             ────
                                                   总工具耗时: 450ms
```

用户感知到的延迟 = LLM 生成时间（2s）+ 工具执行时间（450ms）= 2.45 秒。

其中 LLM 在生成第一个 `tool_use` 块的时候，后端其实已经收到了 `read_file("auth.py")` 的完整参数。但没人去执行它——所有人都在等 LLM 说完。

---

## Claude Code 的做法

```
时间线 ──────────────────────────────────────────►

LLM 生成中... (2秒)
├─ tool_use: read_file("auth.py")   ←── 参数完整！立即执行
│   ↓ 开始执行 (200ms)                 ← 和 LLM 生成并行
│
├─ tool_use: read_file("test_auth.py") ←── 参数完整！立即执行
│   ↓ 开始执行 (150ms)
│
├─ tool_use: read_file("config.py")  ←── 参数完整！立即执行
│   ↓ 开始执行 (100ms)
│
└─ LLM 生成完毕
   此时三个文件已经全部读完了
```

用户感知到的延迟 ≈ LLM 生成时间（2s）。工具执行时间被**完全藏在了 LLM 生成时间里**。

这就是 StreamingToolExecutor 的核心价值：**把工具执行时间从用户可感知的延迟中移除。**

---

## 实现：事件驱动的状态机

Anthropic 的 Streaming API 使用 Server-Sent Events（SSE）格式。每个 content block 有独立的生命周期事件：

```
content_block_start   → 新 content block 开始（可能是文本或 tool_use）
content_block_delta   → 内容片段陆续到达
content_block_stop    → 这个 block 结束了
message_stop          → 整个消息结束
```

关键洞察：**一个 tool_use block 的 `content_block_stop` 事件到达时，这个工具的输入 JSON 就完整了**。不需要等其他 block，不需要等 `message_stop`。

StreamingToolExecutor 就是利用这一点：

```typescript
// src/services/tools/StreamingToolExecutor.ts (简化)

class StreamingToolExecutor {
  // 正在接收参数的工具（还没收到 stop 事件）
  private pendingBlocks = new Map<number, {
    id: string
    name: string
    inputJson: string   // 逐块拼接的 JSON 字符串
  }>()

  // 已经在跑的工具
  private runningTools: Array<{
    promise: Promise<ToolResult>
    tool: ToolDef
    isConcurrencySafe: boolean
  }> = []

  async onEvent(event: StreamEvent) {
    if (event.type === 'content_block_start' && event.content_block.type === 'tool_use') {
      // 新工具调用开始，开始追踪
      this.pendingBlocks.set(event.index, {
        id: event.content_block.id,
        name: event.content_block.name,
        inputJson: '',
      })
    }

    if (event.type === 'content_block_delta' && event.delta.type === 'input_json_delta') {
      // JSON 碎片到了，拼上去
      const block = this.pendingBlocks.get(event.index)!
      block.inputJson += event.delta.partial_json
    }

    if (event.type === 'content_block_stop') {
      const block = this.pendingBlocks.get(event.index)
      if (block) {
        // 参数收集完毕，立即提交执行
        const input = JSON.parse(block.inputJson)
        await this.scheduleExecution(block, input)
        this.pendingBlocks.delete(event.index)
      }
    }
  }
}
```

### 并发安全调度

不是所有工具都能并行跑。`read_file` 和 `grep` 是只读的，可以并行。`edit_file` 和 `bash` 有副作用，需要独占。

每个工具有一个 `isConcurrencySafe` 标记。调度逻辑：

```typescript
async scheduleExecution(block, input) {
  const tool = this.findTool(block.name)

  if (tool.isConcurrencySafe) {
    // 只读工具：直接开始执行，不等其他工具
    const promise = this.executeTool(tool, input)
    this.runningTools.push({ promise, tool, isConcurrencySafe: true })
  } else {
    // 有副作用的工具：等前面所有工具完成，再独占执行
    await this.waitForAllRunning()
    const promise = this.executeTool(tool, input)
    this.runningTools.push({ promise, tool, isConcurrencySafe: false })
  }
}
```

结果按**接收顺序**缓冲。即使 `read_file("config.py")` 先完成（因为文件小），它的结果也排在 `read_file("auth.py")` 后面。保证消息历史的确定性。

---

## 实际性能提升

几个典型场景的对比：

**场景 1：读三个文件（全部只读，可并行）**
- 串行：200ms + 150ms + 100ms = 450ms
- 并行但等 LLM 完：max(200, 150, 100) = 200ms
- 流式并行：**接近 0ms**（文件在 LLM 生成期间就读完了）

**场景 2：读文件 + 跑测试（混合读写）**
- 假设 LLM 先说 `read_file`，再说 `bash("npm test")`
- 流式：`read_file` 在 LLM 还在生成 bash 命令参数时就执行完了
- `bash` 在参数完整后立即开始，测试 2 秒
- 总延迟 ≈ max(LLM生成时间, 2s)，而不是 LLM生成时间 + read时间 + 2s

**场景 3：大量小工具调用（一次返回 8 个 read_file）**
- 串行：8 × 150ms = 1.2s
- 流式并行：第一个文件在 LLM 说出第一个 tool_use 后立即开始读，8 个文件几乎同时开始。总延迟 ≈ max(所有文件) ≈ 200ms，而且被 LLM 生成时间完全吸收。

用过 Claude Code 的人应该有感觉——它"想"完之后开始干活的响应速度非常快。StreamingToolExecutor 是核心原因之一。

---

## CoreCoder 的折中方案

完整的流式工具解析在 OpenAI 兼容 API 上实现有两个困难：

1. OpenAI 的流式事件格式跟 Anthropic 不完全一样——tool_call 的 delta 格式不同
2. 不同 provider 的流式行为差异很大，有些（比如 Ollama）会一次性把所有 tool_calls 在最后吐出来

所以 CoreCoder 选了一个折中方案：不在流中解析工具，但当 LLM 一次返回多个 tool_calls 时，用 `concurrent.futures.ThreadPoolExecutor` 并行执行。

```python
# corecoder/agent.py
def _exec_tools_parallel(self, tool_calls, on_tool=None):
    for tc in tool_calls:
        if on_tool:
            on_tool(tc.name, tc.arguments)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
        return [f.result() for f in futures]
```

这保留了"多工具并行"的收益，损失了"LLM 生成期间就开始执行"的时间优势。对于 DeepSeek、Qwen 这些响应速度比较快的模型（生成一次回复通常 1-2 秒），这个折中是合理的——生成时间本身就不长，流式解析能省的那几百毫秒用户感知不明显。

---

## 一个有意思的工程观察

StreamingToolExecutor 的复杂度（530 行）不在于并行执行本身——用 `Promise.all` 就能做。复杂度在于：

1. **部分 JSON 的拼接和解析**。工具输入参数的 JSON 是一个 token 一个 token 到达的，你需要在每个 `content_block_delta` 事件里拼接，在 `content_block_stop` 时解析。如果某个 delta 里的 JSON 片段恰好在一个字符串中间（比如引号之间），你不能提前解析。
2. **错误传播**。如果一个工具执行出错了，怎么把错误传回主循环？其他还在跑的工具怎么办？Cancel 还是等它们完成？
3. **UI 更新**。每个工具的进度要实时推到终端。并行跑的多个工具的进度信息不能互相覆盖。
4. **AbortController**。用户按 Ctrl+C 时，所有正在跑的工具都要被正确取消。

这些 edge case 单独处理都不难，但组合在一起就产生了大量的状态管理代码。这也是为什么 CoreCoder 选择了简化实现——完整版的复杂度跟收益不成正比（对于一个 1300 行的教学项目来说）。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 5 篇。配套实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)
