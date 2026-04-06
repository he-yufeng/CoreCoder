# 第一篇：从 51 万行说起

`find . -name '*.ts' | wc -l` 返回 1903。`cloc --include-lang=TypeScript .` 返回 512,664 行。

说实话第一反应是关掉终端——这玩意儿用 VS Code 打开都会卡几秒。但我本身就在做 AI Agent 方向（Moonshot AI / Kimi），日常重度使用 Claude Code，很想知道它肚子里到底装了什么。于是就硬着头皮读了。

这篇是系列的第一篇，不深入任何具体模块。目的是在你脑子里建一张地图：Claude Code 是什么，它的技术栈为什么这样选，51 万行代码分布在哪些目录里，以及读完全部代码后我总结出的十个反复出现的设计模式。

---

## 技术栈：Bun + TypeScript + React

对，你没看错。一个终端 CLI 工具用了 React。

TUI 层用 React 不算新鲜，Ink 2017 年就有了，Gatsby CLI、Prisma CLI 都在用。

不过 Claude Code 的场景比一般 CLI 复杂不少：多个 Agent 并行、流式输出、工具执行中用户随时中断、权限弹窗。状态管理的复杂度到了这个级别，用 React 确实比手搓合理。

```
技术选型：
  Bun          → 快速启动（4x faster than Node.js），原生 TS 支持
  TypeScript   → 51万行不用类型系统约等于自杀
  React + Ink  → 声明式终端 UI，复杂状态管理
  Commander.js → CLI 参数解析（Node 生态最成熟的）
  Zod          → 运行时数据校验 + 自动生成 JSON Schema
  ripgrep      → Rust 写的搜索引擎（GrepTool 直接调二进制）
  GrowthBook   → 远程 Feature Flag 和 A/B 测试
```

TypeScript 这个选择不用多说。51 万行代码没有类型系统的话，改一个函数签名你都不知道会炸多少地方。Zod 的定位比较巧——它同时做运行时数据验证和类型推导，等于一份 schema 干两件事：告诉 TypeScript 编译器"这个工具的输入长什么样"，同时在运行时拦截不合法的输入。

---

## 目录结构：四大层次

硬着头皮翻了两天之后，我觉得 Claude Code 的代码可以分成四层来理解。不是什么官方划分，是我自己读完之后的心智模型。

### 第一层：入口与 UI

用户能直接看到、摸到的东西。

```
src/screens/          → React 页面（REPL 主界面、Onboarding 引导、Doctor 诊断）
src/components/       → UI 组件（权限弹窗、工具执行进度、diff 预览）
src/commands/         → 斜杠命令（/commit、/compact、/model、/review）
```

`src/screens/REPL.tsx` 是主界面，3000+ 行，算是整个应用的"壳"。它管输入捕获、消息渲染、命令分发。你在终端里看到的所有东西都从这里出去。

### 第二层：引擎

Claude Code 的大脑。

```
src/query.ts          → 核心 agentic loop（1729 行），while(true) 循环
src/QueryEngine.ts    → 会话状态管理器（1295 行），一个对话一个实例
```

`query.ts` 和 `QueryEngine.ts` 的关系：`QueryEngine` 是外层的"容器"，管理跨轮次的状态（消息历史、token 用量、文件缓存）。`query.ts` 是内层的"引擎"，每次用户输入触发一次 `queryLoop()`，在 while(true) 循环里调 LLM、执行工具、处理错误，直到 LLM 给出纯文本回复。

下一篇会深入分析 `query.ts`。

### 第三层：工具

LLM 能调用的所有能力。Claude Code 有 30+ 个工具。

```
src/tools/BashTool/        → 1143 行，最复杂的单个工具
src/tools/AgentTool/       → 1397 行（目录合计 6700 行），子 Agent 生成
src/tools/FileEditTool/    → 搜索替换编辑
src/tools/FileReadTool/    → 文件读取
src/tools/GrepTool/        → ripgrep 封装
src/tools/GlobTool/        → 文件模式匹配
src/tools/WebFetchTool/    → 网页内容抓取
src/tools/WebSearchTool/   → 网页搜索
src/tools/NotebookEditTool/ → Jupyter Notebook 编辑
src/tools/LSPTool/         → 语言服务器协议交互
... 还有十几个
```

每个工具都是完全自包含的：schema 定义、权限检查、执行逻辑、UI 渲染、上下文压缩摘要，全在一个目录里。没有全局注册表，没有基类继承，全部是 `buildTool()` 工厂函数生成的纯对象。后面第三篇会详细讲。

### 第四层：基础设施

支撑上面三层运作的底层系统。

```
src/services/         → API 客户端、MCP 协议、OAuth、缓存
src/utils/            → 权限系统、Feature Flag、模型配置、埋点
src/memdir/           → 记忆系统（跨会话持久化）
src/skills/           → Skill 系统（可复用的任务模板）
src/buddy/            → 宠物系统（没发布，但代码全在）
src/voice/            → 语音模式（代号 Amber Quartz）
src/bridge/           → 远程控制模式（31 个文件）
src/coordinator/      → 多 Agent 编排
```

权限系统值得单独提一句。Claude Code 有五级权限模式，从"全自动"到"每次都问"。工具调用要过两道关卡：先 `validateInput()` 检查输入是否合法（不合法直接告诉 LLM，不弹窗），再 `checkPermissions()` 检查权限（可能弹窗让用户确认）。这个两阶段设计很聪明——大部分被拒绝的操作是因为输入不合法而不是权限不够，用两阶段可以大大减少弹窗打扰。

---

## 十个设计哲学

读完五十万行代码，有些模式反复出现。我试着总结成十条，后面每篇文章里会引用。

**1. 状态外化。** `QueryEngine` 的所有外部依赖通过 `QueryEngineConfig`（20+ 个字段）注入，不在内部 import。这意味着你可以在单测里传一个 mock 的工具列表和 API 客户端进去，不用启动整个应用。

**2. 渐进式复杂度。** 简单的事保持简单。`FileReadTool` 只有几十行；`BashTool` 有 1143 行。不是每个工具都需要同样的复杂度，复杂度应该在需要的地方集中。

**3. Feature Flag 门控。** 44 个编译时/运行时 flag。功能没通过内部验证？编译时直接删掉，连字符串都不留。不是"if (false)"式的假删除，是 Bun 打包器的 DCE（Dead Code Elimination）物理删除。

**4. 类型即文档。** Zod schema 同时做验证和类型推导。`ToolDef` 类型里的字段名就是最准确的接口文档。新来的人看类型定义就知道一个工具需要实现哪些方法。

**5. 上下文经济学。** token 就是钱。工具输出超过阈值写磁盘，只在上下文里留摘要。上下文有四层压缩。`getToolUseSummary()` 方法为每个工具定义了压缩时的摘要策略。

**6. 安全分层。** 两阶段门控（验证 + 权限）、五级权限模式、BashTool 的命令分类器（把命令解析成 search/read/write 类别来匹配权限规则）、macOS sandbox-exec / Linux seccomp 沙箱。安全不是一道墙，是几道。

**7. 优雅降级。** API 超时重试（指数退避，最多 5 次）。模型不支持 `stream_options` 就去掉这个参数重试。子 Agent 崩溃不影响主循环。输出撞到 `max_output_tokens` 限制自动重试最多 3 次。

**8. 声明式配置。** 工具、命令、Skill、Agent 类型全是声明式的。Skill 就是一个 `.md` 文件加 YAML frontmatter。Agent 类型是一个配置对象。运行时按需组装，不需要在代码里注册。

**9. 流式一切。** API 响应流式处理，StreamingToolExecutor 在模型还在输出时就开始执行工具，进度信息实时推到终端。用户永远在看到进展，而不是等完一个长任务。

**10. 实验性前进。** 消融实验基础设施（`ABLATION_BASELINE` 一键关闭 thinking/compact/memory/background），A/B 测试 flag，每个功能上线前都可以跑对照实验量化价值。不是"感觉有用就上"，是"数据说有用才上"。

---

## 关于这个系列

后面六篇会逐个深入：

- 第二篇：`query.ts` 那个 1729 行的 while(true) 循环
- 第三篇：工具系统和搜索替换编辑的设计
- 第四篇：四层上下文压缩
- 第五篇：StreamingToolExecutor 的流式工具并行
- 第六篇：多 Agent 协作系统
- 第七篇：Feature Flag 背后的未发布功能

我还用 1300 行 Python 复刻了这些核心设计模式：[CoreCoder](https://github.com/he-yufeng/CoreCoder)。每篇文章里会对照源码和 CoreCoder 的实现来讲。

---

> 本文是 [Claude Code 源码导读](00-index.md) 系列第 1 篇。
