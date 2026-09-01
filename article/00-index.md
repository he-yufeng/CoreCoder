# 用 CoreCoder 读懂 Claude Code，再做一个你自己的

这是一份写给工程师的系列导读。它想干两件事。

第一，借 CoreCoder 这个核心一千行出头的开源项目，把 Claude Code 这类生产级编码 agent 的内部结构讲清楚。第二，带你把 CoreCoder fork 下来，改出一个属于你自己的 coding agent。

## 为什么要绕一圈

因为 Claude Code 本体太大了。它的代码量在几十万行的量级，光一个负责跑 shell 命令的 BashTool 就上千行，真要逐行读完，多数人会在第三个文件就关掉编辑器。可 agent 的核心其实没那么复杂，复杂的是工程化之后那些为真实世界兜底的边角：模型半路被打断怎么办、上下文塞满了怎么办、十个工具调用同时回来怎么办、provider 偶尔抽风返回 500 又怎么办。这些东西摊在几十万行里，你很难一眼看到骨架。

CoreCoder 做的事，是把这套骨架压到一千行出头的纯 Python。准确说，agent 引擎（循环、模型接口、上下文、工具、会话）去掉空行和注释是 1197 行；连最外层的 CLI 终端、配置和打包都算上，整个包去掉空行和注释 1576 行，物理代码 1958 行。每个文件都短到能一口气读完，跑起来又确实是个能改代码、能跑命令、能自己开子任务、能压缩上下文、能算钱的 agent。它不是玩具。它是把生产级 agent 的每个设计决策，拣出最核心的那版，用最少的代码诚实地写一遍。

读它，约等于读一份 Claude Code 的「可运行注释版」。区别在于，CoreCoder 的每一行你都能在自己机器上断点、改、跑、看它出什么效果。本系列里我引用的每一个行数、每一段代码，都是从仓库里现读现核的，不是凭印象。这点我后面会反复较真，因为编码 agent 这个领域，太多文章在凭感觉编数字。

## 这个系列假设你是谁

我假设你写过代码，调过 API，大概知道 LLM 的 function calling 是怎么回事，但没真正拆开过一个 agent 的主循环。你可能用过 Claude Code 或者 Cursor，惊叹过它怎么能自己读文件、改代码、跑测试，然后好奇：这背后到底是魔法还是工程。

答案是工程。而且是那种读完会让人觉得「就这？我也能写」的工程。

## 八篇怎么读

前六篇是「读懂」。每篇盯住 agent 的一个子系统，先讲 Claude Code 在这件事上的做法和取舍，再翻到 CoreCoder 对应的真实代码，看同一个想法被压缩成几十行后长什么样。最后一篇是「自己做」，把前面所有零件接起来，从 fork 到加一个自定义工具到换模型，落到一个能跑的成品。

1. [一个 agent 的本体，是一个 while 循环](01-the-loop.md)。整个 agent 最核心的东西，是一个「问模型、跑工具、把结果喂回去、再问」的循环。我们看 CoreCoder 的 `agent.py`（150 行）怎么把它写明白，以及打断、轮次上限、半截工具调用回填这些真实世界的麻烦各自怎么收场。
2. [工具系统：让模型安全地动手](02-tools.md)。模型本身只会吐字，是工具让它能读文件、写文件、跑命令。这篇讲 CoreCoder 的七个工具，重点是那个看似平平无奇、实则是 Claude Code 关键创新的「唯一性搜索替换」编辑，以及 bash 的安全闸。末尾教你写第一个自己的工具。
3. [接入任意大模型，顺便把钱算清楚](03-llm-and-cost.md)。`llm.py`（336 行，全系列最大的文件）怎么用一套 OpenAI 兼容接口接住 DeepSeek、Qwen、Kimi、本地 Ollama，怎么做指数退避重试，怎么在流式输出里顺手把 token 和美元成本统计出来。
4. [用有限的窗口扛住一个长任务](04-context.md)。上下文窗口是 agent 的硬约束。`context.py`（210 行）实现了三层压缩，从轻到重。这篇还会讲一个特别容易踩、API 一定报错的坑：孤儿 tool 消息。这是我做这个项目时真改过的 bug。
5. [并行执行与子 agent](05-parallel-and-subagents.md)。模型一次返回多个工具调用时，CoreCoder 用线程池并发跑。这篇老实讲这个简化版相对 Claude Code 的流式执行器差在哪，并发又会引入什么新麻烦，以及子 agent 为什么不准递归。
6. [把它跑成一个真正的命令行工具](06-session-and-cli.md)。会话存盘、断点续聊、斜杠命令、一次性模式。`session.py` 里有个不起眼但很要命的安全细节：怎么防住用恶意会话名做路径穿越。
7. [Fork CoreCoder，搭一个你自己的 coding agent](07-build-your-own.md)。收尾的实操篇。从 clone 到换成你常用的模型，到加一个真正有用的自定义工具，到改系统提示词调教它的风格，到打包发布。读完前六篇你已经懂了原理，这篇让你真有一个东西。

不想按顺序也行。想搞懂「它凭什么敢自动跑命令」直接跳第二篇；想接自己的模型直接跳第三篇；只想赶紧 fork 出个能用的，第七篇是自洽的。

## 五分钟先把它跑起来

读之前先让它在你机器上活一次，后面所有代码你才有体感。

```bash
git clone https://github.com/he-yufeng/CoreCoder
cd CoreCoder
pip install -e .
```

然后给它一个模型和 key。CoreCoder 默认走 OpenAI 兼容接口，所以你手上任何一家的 key 都能用，换 provider 只是换两个环境变量：

```bash
# OpenAI
export OPENAI_API_KEY=sk-...

# 或者 DeepSeek，便宜很多，改一个 base_url 就行
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com CORECODER_MODEL=deepseek-chat

# 或者本地 Ollama，一分钱不花
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder
```

跑起来：

```bash
corecoder
```

进去之后随便给个任务，比如「读一下 corecoder/agent.py，告诉我这个循环最多转多少轮」。你会看到它先调 `read_file`，再用读到的内容回答你。那一刻它做的事，和 Claude Code 做的事，本质上是同一件。接下来六篇，就是把这「同一件事」拆开看。

那我们从那个循环开始。

---

作者：[何宇峰](https://github.com/he-yufeng)。早前写过一篇 [Claude Code 源码分析](https://zhuanlan.zhihu.com/p/1898797658343862272)，偏完整拆解；这个系列换了个角度，更想让你动手把它复刻出来。
