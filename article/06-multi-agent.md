# 第六篇：当一个 Claude 不够用

Claude Code 的 AgentTool 是除 BashTool 之外最大的工具。主文件 `AgentTool.tsx` 1397 行，整个 `src/tools/AgentTool/` 目录超过 6700 行。

我花了差不多一整天才把这个目录读完。老实说一开始是有点困惑的——"启动另一个 Agent"这件事有什么好写 6700 行的？读完之后理解了：因为它不只是"启动另一个 Agent"。

---

## 为什么需要多 Agent

先说一个实际场景。

用户说："帮我重构 auth 模块的错误处理，给每个改动加测试，最后更新 API 文档。"

在单 Agent 模式下，三个子任务共享一个 128K 的上下文窗口。auth 模块的文件内容、测试代码、文档内容全塞在一个窗口里。每轮工具调用的输出持续膨胀。到改文档的时候，前面读过的 auth 模块代码可能已经被上下文压缩掉了——LLM 得重新读一遍文件，浪费时间和 token。

多 Agent 的解法：主 Agent 把任务拆成三个子任务，每个子任务分配给一个独立的子 Agent。**每个子 Agent 有自己的 128K 上下文**。三个子 Agent 可以并行工作，互不干扰，完成后把结果汇报给主 Agent 整合。

等于你有了 128K × 3 = 384K 的总上下文容量，而且每个子任务的上下文都是"干净"的——只包含跟自己相关的信息。

---

## AgentTool 的三种模式

源码里 AgentTool 有三种执行模式。

### 普通模式（Default）

创建一个新 Agent，在主进程的同一个工作目录下执行。子 Agent 共享文件系统，但有独立的上下文和消息历史。这是最常用的模式。

```typescript
// 普通模式的调用
agent({
  description: "Research the auth module codebase",
  task: "Read all files in src/auth/ and report the error handling patterns used",
})
```

### Worktree 模式

创建一个 Git worktree，子 Agent 在**完全隔离**的目录副本下工作。

```typescript
// Worktree 模式
agent({
  description: "Refactor auth module",
  task: "...",
  isolation: "worktree",  // Feature flag 门控
})
```

这解决了一个棘手问题：如果两个子 Agent 同时修改 `auth.py` 怎么办？在普通模式下会冲突。Worktree 模式下，每个子 Agent 在自己的 Git worktree 里改，完成后主 Agent 决定如何合并。

Git worktree 是 Git 的原生功能，允许一个仓库同时有多个工作目录，每个目录在不同的分支上。Claude Code 利用这个功能给子 Agent 创建隔离环境，不需要复制整个仓库。

### 后台模式

子 Agent 在后台运行，主 Agent **不等待**，继续处理其他事情。后台 Agent 完成后通过通知机制告知主 Agent。

这个模式在 COORDINATOR_MODE 下使用。适合"我需要你后台帮我跑个测试，我先继续写代码"这种场景。

---

## 子 Agent 不能生孩子

翻到这段代码的时候我笑了：

```typescript
// src/tools/AgentTool/runAgent.ts
// 子 Agent 的工具列表：从父 Agent 的工具列表中过滤掉 agent 工具
const subAgentTools = parentTools.filter(t => t.name !== 'agent')
```

子 Agent 拿到的工具列表里**不包含 agent 工具本身**。也就是说子 Agent 没法再创建子子 Agent。

这不是技术上做不到（递归 Agent 理论上完全可行），而是工程上的理性选择：

1. **递归风险。** 一个 Agent 创建 3 个子 Agent，每个子 Agent 再创建 3 个，三层就是 27 个并行 Agent。API 调用费用和并发数瞬间爆炸。
2. **调试地狱。** 两层（主 + 子）的调试已经够复杂了。三层嵌套的 Agent 之间传递上下文，出了问题你根本不知道哪一层搞的。
3. **收益递减。** 实际使用中，两层几乎能覆盖所有场景。需要三层嵌套的任务通常应该被拆解成多个独立任务，而不是用更深的 Agent 树。

CoreCoder 也做了同样的设计：`AgentTool.execute()` 创建子 Agent 时，工具列表过滤掉 `agent`。

---

## 子 Agent 的上下文构建

子 Agent 不是从零开始的白板。它的上下文包含：

1. **一个精简的系统提示词**。包含工作目录信息、可用工具列表、环境信息。但**不包含主 Agent 的对话历史**——那些是主 Agent 的私有上下文。
2. **主 Agent 给它的任务描述**（`task` 参数）。这是唯一从主 Agent 传递给子 Agent 的信息。
3. **一条特殊指令**：完成任务后，把结果写成文字总结返回。

最后一点很关键。子 Agent 的整个执行过程——可能涉及几十轮工具调用——最终被压缩成一段文字，作为 `tool_result` 返回给主 Agent。

```
主 Agent 调用：agent(task="分析 auth 模块的错误处理模式")

子 Agent 执行：
  → read_file("src/auth.py")
  → read_file("src/auth/token.py")
  → read_file("src/auth/session.py")
  → grep("except|raise|try", path="src/auth/")
  → (内部思考和分析)

子 Agent 返回：
  "[Sub-agent completed]
   auth 模块使用了两种错误处理模式：
   1. verify_token() 和 decode_payload() 使用 try/except 捕获 jwt 库的异常
   2. check_permission() 使用 if/else 检查返回值
   建议统一使用 try/except 模式，因为..."
```

主 Agent 看到的只是这段总结，不知道子 Agent 中间读了什么文件、做了什么思考。这很好——主 Agent 不需要知道这些细节，它只需要子任务的结论来做下一步决策。

CoreCoder 也限制了子 Agent 结果的长度（5000 字符上限），防止撑爆主 Agent 的上下文。

---

## AgentTool 6700 行都在干什么

我说"启动另一个 Agent 不需要 6700 行"，那剩下的代码在干什么？分解一下：

```
AgentTool.tsx          (228KB)  → 路由逻辑、Schema 定义、三种模式分发
runAgent.ts            (35KB)   → 真正的执行引擎：构建上下文、跑循环、收集结果
UI.tsx                 (122KB)  → 终端渲染：进度条、结果展示、分组显示
agentToolUtils.ts      (22KB)   → 工具函数：进度追踪、结果处理
forkSubagent.ts        (8.5KB)  → Fork 模式：缓存共享分叉
agentMemory.ts         (5.7KB)  → 跨迭代的记忆持久化
agentMemorySnapshot.ts (5.5KB)  → 记忆状态序列化
loadAgentsDir.ts       (26KB)   → Agent 类型发现和加载
resumeAgent.ts         (9.1KB)  → 恢复暂停的后台 Agent
prompt.ts                       → 子 Agent 的 Prompt 模板
agentColorManager.ts            → 每个子 Agent 分配不同颜色
built-in/                       → 6 种内置 Agent 类型定义
```

大头是 UI 渲染（122KB）和执行引擎（35KB）。UI 渲染复杂是因为多个子 Agent 并行运行时，每个 Agent 的状态（运行中 / 完成 / 出错）和输出要在终端里漂亮地分区展示。这在终端里做起来比 Web UI 难很多。

---

## 内置的六种 Agent 类型

`built-in/` 目录下有六种预定义的 Agent 类型：

| 类型 | 文件 | 用途 |
|------|------|------|
| `generalPurposeAgent` | 通用 | 默认类型，全能型 |
| `planAgent` | 规划 | 只做规划不执行，输出实现方案 |
| `exploreAgent` | 探索 | 只读工具，用于代码库研究 |
| `verificationAgent` | 验证 | 验证修改正确性、跑测试 |
| `claudeCodeGuideAgent` | 帮助 | 回答关于 Claude Code 本身的问题 |
| `statuslineSetup` | 配置 | 状态栏设置向导 |

有意思的是 `exploreAgent`——它的工具列表里**没有任何写工具**（没有 edit_file、write_file、bash）。只有 read_file、grep、glob。这保证了探索型子 Agent 不会意外修改代码。你让它"研究一下这个代码库的架构"，它只能看不能改。

CoreCoder 目前只有一种 Agent 类型（通用型），但你可以在 `Agent.__init__` 里传不同的工具列表来模拟：

```python
# 只读 Agent
from corecoder.tools.read import ReadFileTool
from corecoder.tools.grep import GrepTool
from corecoder.tools.glob_tool import GlobTool

explore_agent = Agent(llm=llm, tools=[ReadFileTool(), GrepTool(), GlobTool()])
```

---

## 团队系统（预告）

在 AgentTool 之上，Claude Code 还有一个更高层的"团队"概念。`TeamCreateTool` 可以创建一组 Agent，分配角色，它们之间通过消息系统通信。

```typescript
// 团队创建（Feature Flag: COORDINATOR_MODE）
teamCreate({
  agents: [
    { name: "frontend", focus: "React components in src/components/" },
    { name: "backend", focus: "API handlers in src/api/" },
    { name: "tester", focus: "Write tests for changed files" },
  ]
})

// Agent 间通信
sendMessage({ to: "tester", content: "I just refactored auth.py, please write tests" })
```

后端支持 tmux pane、in-process、remote 三种方式。但整个团队系统还在 Feature Flag 后面，没对外发布。从源码完成度来看，它已经非常接近可用状态了。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 6 篇。配套实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)
