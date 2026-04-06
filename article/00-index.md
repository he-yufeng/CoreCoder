# Claude Code 源码导读

2026 年 3 月 31 日，Anthropic 的 npm 包里残留的 `.map` 文件泄露了 Claude Code 的全部源码。1903 个文件，512,664 行 TypeScript。

这是我读完全部源码后写的系列文章。不是面面俱到的文档（那个有 16 万字的完整版），而是我认为最值得开发者了解的 7 个切面。每篇都围绕一个核心问题展开，代码引用精确到行号。

如果你在做 AI Agent 相关的工作，这些设计模式迟早会用到。

## 目录

1. **[从 51 万行说起](01-architecture-overview.md)** — Claude Code 的技术栈、目录结构、十大设计哲学。建立全局心智模型。
2. **[1729 行的 while(true)](02-agent-loop.md)** — AI Agent 的核心循环：query.ts 如何驱动工具调用、消息编排、中断恢复。
3. **[让 AI 安全地改你的代码](03-tool-system.md)** — 工具系统的接口设计、两阶段门控、搜索替换编辑的精妙之处。
4. **[有限窗口，无限任务](04-context-compression.md)** — 四层上下文压缩策略的工程细节，以及它为什么不是简单的"截断旧消息"。
5. **[边想边做](05-streaming-executor.md)** — StreamingToolExecutor 如何在模型还没说完的时候就开始执行工具。
6. **[当一个 Claude 不够用](06-multi-agent.md)** — 多 Agent 协作系统：子代理生成、Worktree 隔离、团队编排。
7. **[Feature Flag 背后的秘密](07-hidden-features.md)** — 44 个未发布功能的技术细节：KAIROS 永驻模式、Buddy 宠物系统、Voice Mode、Bridge Mode。

## 配套项目

这些文章中讨论的核心架构模式，我用 1300 行 Python 做了一个可运行的参考实现：[CoreCoder](https://github.com/he-yufeng/CoreCoder)。代码和文章可以对照着看。

## 完整版

如果你想要更详尽的版本（16 篇、16 万字、覆盖构建系统到 MCP 协议的每个子系统），完整导读在[这里](https://github.com/he-yufeng/CoreCoder/tree/main/docs)。

---

作者：[何宇峰](https://github.com/he-yufeng) · [知乎：Claude Code 源码分析（17万+ 阅读）](https://zhuanlan.zhihu.com/p/1898797658343862272)
