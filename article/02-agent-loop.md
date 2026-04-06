# 第二篇：1729 行的 while(true)

如果你只打算看 Claude Code 的一个文件，看 `src/query.ts`。

这个文件 1729 行，包含了 AI 编程 Agent 的全部核心逻辑。这么说吧：你把 Claude Code 其他 50 万行代码全删了，只留 `query.ts` + `QueryEngine.ts` + 工具实现，它还是一个能跑的 Agent。其他一切——UI、命令、Skill、MCP——都是围绕这个核心循环搭建的外围设施。

---

## 循环的骨架

剥掉错误处理、Feature Flag、日志、消融实验开关之后，核心循环长这样：

```typescript
// src/query.ts (简化版，保留核心逻辑)
async function* queryLoop(params: QueryLoopParams) {
  let state = {
    messages: params.initialMessages,
    turnCount: 1,
    outputTokenRecoveryCount: 0,
  }

  while (true) {
    // 1. 构造上下文
    const systemPrompt = buildSystemPrompt(params)
    const messagesForApi = maybeCompressHistory(state.messages)

    // 2. 调 API（流式）
    const stream = createMessageStream({
      model: params.model,
      system: systemPrompt,
      messages: messagesForApi,
      tools: params.toolDefinitions,
      max_tokens: calculateMaxTokens(state),
    })

    // 3. 处理流式响应
    const response = await processStreamEvents(stream)

    // 4. 有工具调用？执行它们
    if (response.toolUses.length > 0) {
      const results = await executeToolCalls(response.toolUses, params.toolContext)
      state.messages.push(response.assistantMessage)
      state.messages.push(...results.map(toToolResultMessage))
      state.turnCount++
      continue  // → 回到 while(true) 顶部
    }

    // 5. 没有工具调用 = LLM 说完了
    state.messages.push(response.assistantMessage)
    return  // 退出循环
  }
}
```

这就是每一个 AI Agent 的核心模式：**用户说话 → 调 LLM → LLM 要工具就执行 → 把结果喂回 LLM → 重复，直到 LLM 给出纯文本回复。**

Claude Code、Cursor、Cline、Aider，底层都是这个循环。差别只在细节处理。而 Claude Code 在这个骨架上堆了一层又一层的防御性逻辑，读完之后我觉得这大概是目前工业界最健壮的 agentic loop 实现。

---

## 细节一：系统提示词是动态拼的

很多 Agent 框架的系统提示词是一个写死的字符串。Claude Code 的不是。

`src/constants/prompts.ts` 有 914 行，系统提示词在每次循环迭代时动态组装。它会根据当前环境拼接不同的模块：

```typescript
// prompts.ts 的组装逻辑（概念性简化）
function buildSystemPrompt(context) {
  const parts = []

  parts.push(CORE_IDENTITY)           // "你是 Claude Code，一个终端里的 AI 助手"
  parts.push(getToolDescriptions())    // 当前可用的工具列表和使用建议
  parts.push(getEnvironmentInfo())     // OS、工作目录、Git 状态、Python 版本
  parts.push(getCWDInfo())             // 当前目录下的关键文件
  parts.push(getMemoryFiles())         // CLAUDE.md 和 memory 文件的内容
  parts.push(getSkillInstructions())   // 已加载 Skill 的指令

  if (context.isResumedSession) {
    parts.push(RESUMED_SESSION_NOTICE)  // "这是从之前中断的会话恢复的"
  }

  // @[MODEL LAUNCH] TODO: 针对不同模型版本的行为修正
  if (isCapybara(context.model)) {
    parts.push(CAPYBARA_COMMENT_FIX)   // "不要过度注释代码"
  }

  return parts.join('\n\n')
}
```

这意味着同一个用户在不同目录下、不同时间点、用不同模型，看到的系统提示词是不一样的。这也是为什么 Claude Code 在不同项目里表现差别很大——不是 LLM 变了，是提示词变了。

有意思的一点：源码里有大量 `@[MODEL LAUNCH]` 注释，每一个都是"当某个新模型发布时需要修改的地方"。其中关于 Capybara 的有好几条，包括"v8 版本的虚假声明率 29-30%，需要在 prompt 层面修补"。说明新模型的行为矫正是在系统提示词层面做的，不是在模型层面。

CoreCoder 的 `prompt.py` 是这个机制的极简版（35 行）：根据当前工作目录、OS 信息、可用工具列表动态拼接。没有模型特定修正和 Skill 加载，但核心思路一样。

---

## 细节二：StreamingToolExecutor

这是整个源码里我最想研究的部分。独立文件 `src/services/tools/StreamingToolExecutor.ts`，530 行。

一般 Agent 框架的做法：等 LLM 的完整响应接收完毕 → 解析所有 tool_use 块 → 串行或并行执行。中间有一段等待时间。

Claude Code 的做法：**LLM 还在生成后面的内容时，前面的工具已经开始跑了。**

实现方式是监听 Anthropic API 的 Server-Sent Events 流。每个 content block（可能是文本块或 tool_use 块）有独立的 start / delta / stop 事件。当一个 tool_use 块的 stop 事件到达时，这个工具的输入 JSON 就完整了。不用等其他 content block。

```typescript
// StreamingToolExecutor.ts 的事件处理逻辑（简化）
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
          // 工具输入 JSON 的碎片陆续到来，拼接
          const block = this.pendingBlocks.get(event.index)!
          block.inputJson += event.delta.partial_json
        }
        break

      case 'content_block_stop': {
        const block = this.pendingBlocks.get(event.index)
        if (block) {
          // 输入完整了，解析 JSON，**立即**提交执行
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

还有一个关键设计：**并发安全标记**。每个工具有一个 `isConcurrencySafe` 属性。读文件、grep、glob 这些只读操作标记为 safe，可以并行。写文件、bash 这些有副作用的需要独占执行。StreamingToolExecutor 维护了一个调度队列来保证这个约束。

效果：假设 LLM 一次返回三个工具调用——读文件 A（200ms）、读文件 B（150ms）、跑测试（2s）。串行执行总共 2350ms。StreamingToolExecutor 下，三个工具在 LLM 还在生成的时候就已经开始跑了，实际延迟接近于 max(200, 150, 2000) = 2000ms，甚至更短（因为工具执行时间被 LLM 生成时间"吸收"了）。

CoreCoder 用了一个折中方案：不做流式解析，但当 LLM 一次返回多个 tool_calls 时，用 `ThreadPoolExecutor` 并行执行。损失了"在 LLM 生成期间就开始执行"的优势，但保留了"多工具并行"的收益。大概十几行代码。

---

## 细节三：错误恢复——不认输的循环

生产级 Agent 不能因为一次 API 超时就崩溃。`query.ts` 里的错误处理写得很细致，我数了一下大致有这些分支：

**API 429（限流）**：指数退避重试，最多 5 次。每次等待时间翻倍。

**API 400/413（请求体太大）**：说明上下文超了。自动触发压缩，裁掉旧消息，然后**用压缩后的消息重新调 API**，而不是报错退出。

**API 529（服务过载）**：切换到配置里的 `fallbackModel`。比如主力模型 Opus 不可用，fallback 到 Sonnet。

**输出触顶（max_output_tokens）**：这个处理最有意思。模型的回复被截断了——可能一个工具调用的 JSON 只输出了一半。大多数框架会报错。Claude Code 的做法是"扣留"这个不完整的响应，悄悄重试，最多 3 次。代码上面有段中世纪巫师口吻的注释：

```
// Heed these rules well, young wizard. For they are the rules of thinking,
// and the rules of thinking are the rules of the universe. If ye does not
// heed these rules, ye will be punished with an entire day of debugging
// and hair pulling.
```

维护这段代码的人显然被坑过不少次。

**用户中断（Ctrl+C）**：正在跑的工具通过 AbortController 取消。已经完成的工具结果保留在消息历史中。下次用户输入时，LLM 可以看到"你之前做了 A 和 B，C 被用户取消了"。

**工具执行异常**：这是最关键的一条。异常不会传播到外层。Claude Code 把异常信息包装成一条 `tool_result`，角色设为 error，然后**喂回 LLM**，让 LLM 自己决定如何处理。LLM 可能会换一种方式重试，或者告诉用户"这个方案行不通，我试试另一个"。

这才是"agentic"的核心含义：Agent 遇到问题时自己想办法，而不是转头问人。

CoreCoder 的 `agent.py` 实现了工具异常捕获喂回 LLM 和 max_rounds 限制，但没有做 API 层面的重试和 fallback（这些在不同 LLM provider 之间差异太大，放在上层处理更合适）。

---

## 细节四：token 预算

`QueryEngine` 管理两种预算：

**轮次预算**（`maxTurns`）。每完成一轮工具调用算一个 turn。超过上限就强制停止。主要防 LLM 陷入循环（比如反复读同一个文件但每次都"没找到"它要的东西）。

**美元预算**（`maxBudgetUsd`）。每次 API 调用的 token 消耗换算成费用。累计超过预算就停。在 SDK 模式下特别有用：你可以限制一个自动化任务最多花 0.5 美元。

```typescript
// QueryEngine.ts
type QueryEngineConfig = {
  maxTurns?: number              // 最大轮次
  maxBudgetUsd?: number          // USD 预算上限
  taskBudget?: { total: number } // API 侧 token 预算
  // ...
}
```

CoreCoder 用 `max_rounds=50` 做了轮次限制，没做美元预算（多 provider 环境下费率不统一，不好算）。

---

## 细节五：投机执行

`AppState` 里有个字段叫 `speculationState`，追踪每一轮的结束方式：bash 命令、文件编辑、正常文本回复、权限拒绝。系统用这个来**预判下一步操作**。

如果上一轮 LLM 编辑了一个文件，系统会提前准备好 diff 渲染组件。如果上一轮跑了 bash 命令，系统会提前分配一个终端 buffer。

用过 Claude Code 的人应该有感觉——它"想"完之后开始干活的速度很快。投机执行是原因之一。

---

## 带走什么

如果你在做自己的 Agent 产品，从 `query.ts` 可以带走的最重要的设计：

1. **工具异常喂回 LLM**，不要替 Agent 做决定
2. **上下文超限时压缩重试**，不要报错退出
3. **工具并行执行**，用户感知延迟可以大幅降低
4. **轮次预算**，防止 LLM 无限循环
5. **系统提示词动态拼装**，同一个 Agent 在不同环境下有不同行为

这些设计模式我在 CoreCoder 里都做了最小实现（`agent.py` 110 行）。如果你想看它们在生产代码里长什么样，`query.ts` 是最好的教材。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 2 篇。配套实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)
