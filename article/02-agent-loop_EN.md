# Part Two: The while(true) on Line 1729

If you only intend to look at one file in the Claude Code, look at `src/query.ts`.

This file's 1729 lines contains all the core logic of the AI ​​programming agent. To put it another way: if you deleted all the other 500,000 lines of Claude Code, leaving only `query.ts` + `QueryEngine.ts` + the tool implementation, it would still be a working agent. Everything else—UI, commands, skills, MCP-is peripheral infrastructure built around this core loop.

---

## The Skeleton of the Loop

After stripping away error handling, Feature Flag, logging, and ablation test switches, the core loop looks like this:

```typescript
// src/query.ts (streamlined loop to highlight the core logic)
async function* queryLoop(params: QueryLoopParams) {
  let state = {
    messages: params.initialMessages,
    turnCount: 1,
    outputTokenRecoveryCount: 0,
  }

  while (true) {
    // 1. context build
    const systemPrompt = buildSystemPrompt(params)
    const messagesForApi = maybeCompressHistory(state.messages)

    // 2. API call (streaming)
    const stream = createMessageStream({
      model: params.model,
      system: systemPrompt,
      messages: messagesForApi,
      tools: params.toolDefinitions,
      max_tokens: calculateMaxTokens(state),
    })

    // 3. Process streaming responses
    const response = await processStreamEvents(stream)

    // 4. Execute tool calls (if any)
    if (response.toolUses.length > 0) {
      const results = await executeToolCalls(response.toolUses, params.toolContext)
      state.messages.push(response.assistantMessage)
      state.messages.push(...results.map(toToolResultMessage))
      state.turnCount++
      continue  // → while(true) 
    }

    // 5. LLM is done
    state.messages.push(response.assistantMessage)
    return  // Exit the loop
  }
}
```

This is the core pattern of every AI Agent: **User speaks → Invokes LLM → LLM executes tools if needed → Feeds results back to LLM → Repeats until LLM provides a plain text response.** 

Claude Code, Cursor, Cline, and Aider all use this underlying loop. The differences lie only in the details. Claude Code, however, builds layers upon layers of defensive logic on top of this framework. After reading it, I think this is probably the most robust agentic loop implementation in the industry today.

---

## Detail 1: System Prompts are Dynamically Assembled

Many agent frameworks use a hard-coded system prompt. Claude Code does not.

`src/constants/prompts.ts` has 914 lines; the system prompt is dynamically assembled during each loop iteration. It constructs different modules based on the current environment:


```typescript
// how prompts are build in prompts.ts (streamlined logic)
function buildSystemPrompt(context) {
  const parts = []

  parts.push(CORE_IDENTITY)            // "You are Claude Code, an AI assistant in the terminal"

  parts.push(getToolDescriptions())    // List of currently available tools and usage suggestions
  parts.push(getEnvironmentInfo())     // OS, working directory, Git status, Python version
  parts.push(getCWDInfo())             // Key files in the current directory
  parts.push(getMemoryFiles())         // Contents of CLAUDE.md and memory files
  parts.push(getSkillInstructions())   // Instructions for loaded skills

  if (context.isResumedSession) {
    parts.push(RESUMED_SESSION_NOTICE)  // "This resumes from a previously interrupted session"
  }

  // @[MODEL LAUNCH] TODO: Behavior correction for different model versions
  if (isCapybara(context.model)) {
    parts.push(CAPYBARA_COMMENT_FIX)   // "Don't over-comment code"
  }

  return parts.join('\n\n')
}
```

This means that the same user will see different system prompts in different directories, at different times, and using different models. This is why Claude Code behaves very differently in different projects—it's not that the LLM has changed, but that the prompts have changed.

An interesting point: there are many `@[MODEL LAUNCH]` comments in the source code, each indicating "a place that needs to be modified when a new model is released." Several of these are related to Capybara, including "v8 version has a false claim rate of 29-30%, which needs to be fixed at the prompt level." This indicates that the behavior correction for the new model is done at the system prompt level, not at the model level.

NanoCoder's `prompt.py` is a simplified version of this mechanism (35 lines): it dynamically concatenates data based on the current working directory, OS information, and a list of available tools. There's no model-specific modification or skill loading, but the core idea is the same.

---

## Detail Two: StreamingToolExecutor

This is the part of the entire source code I want to study the most. It's a separate file, `src/services/tools/StreamingToolExecutor.ts`, 530 lines. Here's why.

The typical approach of an agent framework is: wait for the LLM's complete response to be received → parse all tool_use blocks → execute serially or in parallel. This means that there's a waiting period in between.

Claude Code's approach: **While the LLM is still generating subsequent content, the preceding tools have already started running.**

The implementation involves listening to the Anthropic API's Server-Sent Events stream. Each content block (which could be a text block or a tool_use block) has independent start/delta/stop events. When the stop event of a tool_use block arrives, the tool's input JSON is complete. This means the tool can be readily executed without waiting for other content blocks.

```typescript
// Event handling logic of StreamingToolExecutor.ts (simplified)
class StreamingToolExecutor {
  private pendingBlocks = new Map<number, PartialToolUse>()
  private runningTools: Promise<ToolResult>[] = []

  onStreamEvent(event: StreamEvent) {
    switch (event.type) {
      case 'content_block_start':
        if (event.content_block.type === 'tool_use') {
          this.pendingBlocks.set(event.index, {
            id: event.content_block.id,
            name: event.content_block.name,
            inputJson: '',
          })
        }
        break

      case 'content_block_delta':
        if (event.delta.type === 'input_json_delta') {
          // Fragments of the tool's input JSON arrive one after another, and are pieced together
          const block = this.pendingBlocks.get(event.index)!
          block.inputJson += event.delta.partial_json
        }
        break

      case 'content_block_stop': {
        const block = this.pendingBlocks.get(event.index)
        if (block) {
          // Input complete, parse JSON, **immediately** commit and execute
          const input = JSON.parse(block.inputJson)
          const promise = this.executeToolWithPermissionCheck(block, input)
          this.runningTools.push(promise)
          this.pendingBlocks.delete(event.index)
        }
        break
      }
    }
  }
}
```

Another key design element: **concurrency safety flags**: each tool has an `isConcurrencySafe` property. Read-only operations like reading files, grep, and glob are marked as safe and can be performed in parallel. Operations with side effects, such as writing files and bash, require exclusive execution. StreamingToolExecutor maintains a scheduling queue to ensure this constraint.

The resulting effect is the following. Assume the LLM returns three tool calls at once—reading file A (200ms), reading file B (150ms), and running a test (2s). Serial execution takes a total of 2350ms. With StreamingToolExecutor, the three tools start running while the LLM is still being generated, resulting in an actual latency close to max(200, 150, 2000) = 2000ms, or even less (because the tool execution time is "absorbed" by the LLM generation time).

NanoCoder uses a compromise: it doesn't perform streaming parsing, but when the LLM returns multiple tool calls at once, it uses `ThreadPoolExecutor` to execute them in parallel. This sacrifices the advantage of eager execution tools with complete JSON input, i.e."starting execution during LLM generation" but retains the benefit of "multiple tools running in parallel." It involves approximately a dozen lines of code.

---

## Detail 3: Error Recovery – The Unyielding Loop

A production-grade Agent cannot crash just because of a single API timeout. The error handling in query.ts is very detailed

**API 429 (Rate Limiting)**: Exponential backoff retries, up to 5 times. This means the wait time doubles each time.

**API 400/413 (Request Body Too Large)**: Indicates the context has exceeded the limit. Automatic compression is triggered, old messages are truncated, and then the API is re-called with the compressed message, instead of exiting with an error.

**API 529 (Service Overload)**: Switch to the `fallbackModel` in the configuration. For example, if the main model Opus is unavailable, fallback to Sonnet.

**Output Reaching Maximum (max_output_tokens)**: This handling is the most interesting. The model's response is truncated—perhaps a tool's JSON is only half-output. Most frameworks would report an error. Claude Code's approach is to "withhold" this incomplete response and quietly retry, up to 3 times. The code contains a comment in the style of a medieval wizard:

```
// Heed these rules well, young wizard. For they are the rules of thinking,
// and the rules of thinking are the rules of the universe. If ye does not
// heed these rules, ye will be punished with an entire day of debugging
// and hair pulling.
```

Whoever maintains this code has clearly been burned many times.

**User Abort (Ctrl+C)**: The running tool is canceled via the AbortController. The completed tool results are retained in the message history. The next time the user enters, the LLM can see "You previously did A and B, C was canceled by the user".

**Tool Execution Exception**: This is the most crucial point. Exceptions do not propagate to the outer layer. Claude Code wraps the exception information into a `tool_result`, sets its role to error, and then **feeds it back to the LLM**, letting the LLM decide how to handle it. LLM might retry in a different way or tell the user, "This solution doesn't work, I'll try another one."

This is the core meaning of "agentic": the agent solves problems on its own, rather than turning to others for help.

NanoCoder's `agent.py` implements tool exception handling, feeding back to LLM, and setting `max_rounds` limits, but it doesn't implement API-level retries and fallbacks (these vary too much between different LLM providers and are more suitable for higher-level handling).

---
## Detail Four: Token Budget

`QueryEngine` manages two budgets:

**Round Budget** (`maxTurns`). Each completed tool call counts as one turn. Exceeding the limit forces a stop. This primarily prevents LLM from getting stuck in a loop (e.g., repeatedly reading the same file but never finding what it needs).

**Dollar Budget** (`maxBudgetUsd`). The token consumption for each API call is converted into a fee. Exceeding the budget causes a stop. This is especially useful in SDK mode: you can limit the cost of an automation task to a maximum of $0.50.

```typescript
// QueryEngine.ts
type QueryEngineConfig = {
  maxTurns?: number              // Maximum number of rounds
  maxBudgetUsd?: number          // USD budget limit
  taskBudget?: { total: number } // API-side token budget
  // ...
}
```

NanoCoder uses `max_rounds=50` to limit the number of rounds, but deliberately doesn't use a USD budget (rates are inconsistent in a multi-provider environment, making calculation difficult).

---

## Detail 5: Speculative Execution

`AppState` has a field called `speculationState`, which tracks the ending method of each round: bash command, file editing, normal text reply, permission denial. The system uses this to **predict the next operation**.

If the previous round of LLM edited a file, the system will prepare the diff rendering component in advance. If a bash command was executed in the previous round, the system will pre-allocate a terminal buffer.

Those familiar with Claude Code will know—it starts working very quickly after "thinking." Speculative execution is one reason for this.

---

## Take home lessons

If you're developing your own Agent product, the most important design principles you can take away from `query.ts` are:

1. **Feed back the LLM from tool exceptions**, don't make decisions for the Agent.

2. **Compress and retry when context exceeds limits**, don't exit with errors.

3. **Eager and Parallel execution of tools**, significantly reducing user-perceived latency.

4. **Per-iteration budgeting**, preventing infinite LLM loops.

5. **Dynamically assemble system prompts**, allowing the same Agent to behave differently in different environments.

I've implemented these design patterns minimally in NanoCoder (`agent.py`, 110 lines). If you want to see what they look like in production code, `query.ts` is the best example.

---

> This is the second article in the [Claude Code Source Code Guide (EN)](00-index_EN.md) series. Accompanying implementation: [NanoCoder](https://github.com/he-yufeng/NanoCoder)