# Claude Code Source Code Guide

On March 31, 2026, a residual `.map` file in Anthropic's npm package leaked the entire source code of Claude Code. 1903 files, 512,664 lines of TypeScript.

This is a series of articles I wrote after reading the entire source code. It's not a comprehensive document (that 160,000-word complete version), but it rather focuses on what I believe are the 7 most important aspects for developers to understand. Each article revolves around a core question with accurate code references.

If you're working on AI Agent-related projects, you'll inevitably use these design patterns sooner or later.

## Table of Contents

1. **[The 510,000 Lines Codebase](01-architecture-overview_EN.md)** — Claude Code's technology stack, directory structure, and ten design philosophies. Building a global mental model.

2. **[The while(true) on line 1729](02-agent-loop_EN.md)** — The core loop of the AI ​​Agent: how query.ts drives tool calls, message orchestration, and interrupt recovery.

3. **[Let AI Safely Modify Your Code](03-tool-system_EN.md)** — The ingenuity of the tool system's interface design, two-phase gating, and search-replace-edit.

4. **[Finite Window, Infinite Tasks](04-context-compression_EN.md)** — Engineering details of the four-layer context compression strategy, and why it's not simply "truncating old messages".

5. **[Think and Do](05-streaming-executor_EN.md)** — How StreamingToolExecutor starts executing the tool before the model has finished speaking.

6. **[When One Claude Isn't Enough](06-multi-agent_EN.md)** — Multi-Agent Collaboration System: Sub-Agent Generation, Worktree Isolation, Team Orchestration.

7. **[The Secret Behind Feature Flags](07-hidden-features_EN.md)** — Technical Details of 44 Unreleased Features: KAIROS Persistent Mode, Buddy Pet System, Voice Mode, Bridge Mode.

## Supporting Project

I've created a working reference implementation of the core architectural patterns discussed in these articles in 1300 lines of Python: [NanoCoder](https://github.com/he-yufeng/NanoCoder). You can compare the code and the articles.

## Full Version

You can find a comprehensive guide (16 articles, 160,000 words, covering the build system to every subsystem of the MCP protocol) [here](https://github.com/he-yufeng/NanoCoder/tree/main/docs).

---

Author: [He Yufeng](https://github.com/he-yufeng) · [Zhihu: Claude Code Source Code Analysis (170,000+ views)](https://zhuanlan.zhihu.com/p/1898797658343862272)