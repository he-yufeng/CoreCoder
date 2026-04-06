# Part 5: Thinking and Doing Simultaneously

Human programmers don't debug by "thinking through all the steps first, then executing them one by one." A more common pattern is: seeing an error, still analyzing it in their mind, while their hands are already opening the relevant files. Thinking and action overlap.

Claude Code's `StreamingToolExecutor` (530 lines) allows AI agents to work in this way. This is, in my opinion, the most ingenious component in the entire codebase.

---

## Let's start with the problem

Suppose a user says, "Help me refactor the auth module." LLM decides it needs to read three files first: `auth.py`, `test_auth.py`, and `config.py`. In most agent frameworks, the execution flow is as follows:

```
时间线 ──────────────────────────────────────────►

LLM generation in progress... (2 seconds)
├─ "I need to check these files first"
├─ tool_use: read_file("auth.py")
├─ tool_use: read_file("test_auth.py")
└─ tool_use: read_file("config.py")
                │
                ▼ (LLM generation complete)
          Parsing tool_calls
                │
                ▼
          Executing read_file("auth.py")      200ms
          Executing read_file("test_auth.py") 150ms
          Executing read_file("config.py")    100ms
                                              ────
                                      Total time: 450ms
```

User-perceived latency = LLM generation time (2s) + Tool execution time (450ms) = 2.45 seconds.

While LLM generates the first `tool_use` block, the backend has already received the complete parameters for `read_file("auth.py")`. However, no one executes it—everyone is waiting for LLM to finish.

---

## Claude Code's Approach

``` Timeline ────────────────────────────────────────►

LLM generation in progress... (2 seconds)
├─ tool_use: read_file("auth.py")      ←── Complete parameters! Execute immediately
│      ↓ Start execution (200ms) ← Parallel to LLM generation
│ 
├─ tool_use: read_file("test_auth.py") ←── Complete parameters! Execute immediately
│      ↓ Start execution (150ms)
│
├─ tool_use: read_file("config.py")    ←── Parameters complete! Execute immediately
│      ↓ Start execution (100ms)
│ 
└─ LLM generation complete
    At this point, all three files have been read.
```

The user-perceived latency ≈ LLM generation time (2s). The tool execution time is **completely hidden within the LLM generation time**.

This is the core value of StreamingToolExecutor: **removing tool execution time from the user-perceived latency**.**

---

## Implementation: Event-driven state machine

Anthropic's Streaming API uses Server-Sent Events (SSE) format. Each content block has an independent lifecycle event:

```
content_block_start → New content block begins (may be text or tool_use)
content_block_delta → Content fragments arrive successively
content_block_stop  → This block ends
message_stop        → The entire message ends
```

Key insight: **When the `content_block_stop` event of a tool_use block arrives, the input JSON for this tool is complete.** It does not need to wait for other blocks, nor does it need to wait for `message_stop`.

StreamingToolExecutor leverages this:

```typescript
// src/services/tools/StreamingToolExecutor.ts (simplified)

class StreamingToolExecutor {
  // The tool currently receiving parameters (hasn't received the stop event yet)
  private pendingBlocks = new Map<number, {
    id: string
    name: string
    inputJson: string // A JSON string concatenated block by block
  }>()

  // The tool already running
  private runningTools: Array<{
    promise: Promise<ToolResult>
    tool: ToolDef
    isConcurrencySafe: boolean
  }> = []

  async onEvent(event: StreamEvent) {
    if (event.type === 'content_block_start' && event.content_block.type === 'tool_use') {
      // New tool call begins, starting tracking
      this.pendingBlocks.set(event.index, {
        id: event.content_block.id,
        name: event.content_block.name,
        inputJson: '',
      })
    }

    if (event.type === 'content_block_delta' && event.delta.type === 'input_json_delta') {
      // JSON fragments have arrived, piece them together
      const block = this.pendingBlocks.get(event.index)!
      block.inputJson += event.delta.partial_json
    }

    if (event.type === 'content_block_stop') {
      const block = this.pendingBlocks.get(event.index)
      if (block) {
        // Parameters collected, commit execution immediately
        const input = JSON.parse(block.inputJson)
        await this.scheduleExecution(block, input)
        this.pendingBlocks.delete(event.index)
      }
    }
  }
}

```

### Concurrency-Safe Scheduling

Not all tools can run in parallel. `read_file` and `grep` are read-only and can run in parallel. `edit_file` and `bash` have side effects and require exclusive access.

Each tool has an `isConcurrencySafe` flag. Scheduling Logic:

```typescript
async scheduleExecution(block, input) {
  const tool = this.findTool(block.name)

  if (tool.isConcurrencySafe) {
    // Read-only tools: start execution immediately, without waiting for other tools
    const promise = this.executeTool(tool, input)
    this.runningTools.push({ promise, tool, isConcurrencySafe: true })
  } else {
    // Tools with side effects: wait for all previous tools to complete before executing exclusively
    await this.waitForAllRunning()
    const promise = this.executeTool(tool, input)
    this.runningTools.push({ promise, tool, isConcurrencySafe: false })
  }
}
```

Results are buffered in **received order**. Even if `read_file("config.py")` completes first (because the file is small), its result will be placed after `read_file("auth.py")`. This ensures the determinism of message history.

---

## Actual Performance Improvement

Comparison of several typical scenarios:

**Scenario 1: Reading three files (all read-only, can be parallelized)**
- Serial: 200ms + 150ms + 100ms = 450ms
- Parallel but waiting for LLM to finish: max(200, 150, 100) = 200ms
- Streaming parallel: **close to 0ms** (files are read during LLM generation)

**Scenario 2: Reading files + running tests (mixed read/write)**
- Assuming LLM says `read_file` first, then `bash("npm test")`
- Streaming: `read_file` is executed while LLM is still generating bash command arguments
- `bash` starts immediately after the arguments are complete, test takes 2 seconds
- Total latency ≈ max(LLM generation time, 2s), not LLM generation time + read time + 2s

**Scenario 3: Numerous small tool calls (returning 8 read_files at a time)**
- Serial: 8 × 150ms = 1.2s
- Streaming Parallelism: The first file starts reading immediately after the LLM issues the first tool_use, and the 8 files start almost simultaneously. Total latency ≈ max(all files) ≈ 200ms, and this is completely absorbed by the LLM generation time.

Those who have used Claude Code should know that its response speed after "thinking" and starting work is extremely fast. StreamingToolExecutor is one of the core reasons for this.

---

## NanoCoder's Compromise

Implementing complete streaming tool parsing on the OpenAI-compatible API presents two challenges:

1. OpenAI's streaming event format is not entirely the same as Anthropic's—the delta format of tool_calls differs.
2. Different providers exhibit significantly different streaming behaviors; some (like Ollama) will output all tool_calls at the end.

Therefore, NanoCoder opted for a compromise: it doesn't parse tools in the stream, but when the LLM returns multiple tool_calls at once, it uses `concurrent.futures.ThreadPoolExecutor` to execute them in parallel.

```python
# nanocoder/agent.py
def _exec_tools_parallel(self, tool_calls, on_tool=None):
    for tc in tool_calls:
        if on_tool:
            on_tool(tc.name, tc.arguments)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
        return [f.result() for f in futures]
```

This preserves the benefits of "multi-tool parallelism" but sacrifices the time advantage of "execution starting during LLM generation". For models with relatively fast response times like DeepSeek and Qwen (generating a response typically takes 1-2 seconds), this trade-off is reasonable—the generation time itself is short, and the few hundred milliseconds saved by streaming parsing are not noticeable to the user.

---

## An Interesting Engineering Observation

The complexity of StreamingToolExecutor (530 lines) does not lie in the parallel execution itself—this can be done with `Promise.all`. The complexity lies in:

1. **Concatenating and parsing partial JSON**. The JSON input parameters for the tools arrive token by token. You need to concatenate them in each `content_block_delta` event and parse them during `content_block_stop`. If a JSON fragment within a delta happens to be in the middle of a string (e.g., between quotation marks), you cannot parse it beforehand.
2. **Error propagation**. If a tool encounters an error, how do you propagate the error back to the main loop? What about other running tools? Cancel them or wait for them to complete?
3. **UI updates**. The progress of each tool needs to be pushed to the terminal in real time. The progress information of multiple tools running in parallel must not overwrite each other.
4. **AbortController**. When the user presses Ctrl+C, all running tools must be correctly canceled.

These edge cases are not difficult to handle individually, but combined, they generate a large amount of state management code. This is why NanoCoder opted for a simplified implementation—the complexity of the full version is an overkill for a 1300-line educational project.

---

> This is the 5th article in the [Claude Code Source Code Guide](00-index_EN.md) series. Accompanying implementation: [NanoCoder](https://github.com/he-yufeng/NanoCoder)