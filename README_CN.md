# CoreCoder

> 原名 **NanoCoder**，为避免与 [Nano-Collective/nanocoder](https://github.com/Nano-Collective/nanocoder) 混淆而改名。旧链接自动跳转到这里。


[English](README.md) | [中文](README_CN.md) | [Claude Code 源码深度导读（7 篇）](article/)

[![PyPI](https://img.shields.io/pypi/v/corecoder)](https://pypi.org/project/corecoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/CoreCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/CoreCoder/actions)

**51万行 TypeScript → 950 行 Python。**

我逆向了 Claude Code 泄露的全部源码，然后把不承重的部分全扔掉，用 Python 重建了核心。成果：**Claude Code 的每一个关键架构模式，浓缩在一个下午能读完的代码库里。**

CoreCoder 不仅是一个 AI 编程工具。它是一份**蓝图**，编程 Agent 领域的 [nanoGPT](https://github.com/karpathy/nanoGPT)。读懂它，fork 它，然后造你自己的。

---

```
$ corecoder -m kimi-k2.5

You > 读一下 main.py，修掉拼错的 import

  > read_file(file_path='main.py')
  > edit_file(file_path='main.py', ...)

--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-from utils import halper
+from utils import helper

修好了：halper → helper。
```

## 你能得到什么

Claude Code 51 万行源码提炼出来的 7 个核心模式：

| 设计模式 | Claude Code | CoreCoder |
|---|---|---|
| 搜索替换编辑（唯一匹配 + diff） | FileEditTool | `tools/edit.py` — 70 行 |
| 并行工具执行 | StreamingToolExecutor（530行） | `agent.py` — ThreadPool |
| 三层上下文压缩 | HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE | `context.py` — 145 行 |
| 子代理隔离上下文 | AgentTool（1,397行） | `tools/agent.py` — 50 行 |
| 危险命令拦截 | BashTool（1,143行） | `tools/bash.py` — 95 行 |
| 会话持久化 | QueryEngine（1,295行） | `session.py` — 65 行 |
| 动态系统提示词 | prompts.ts（914行） | `prompt.py` — 35 行 |

每个模式都是可运行的实现，不是流程图，不是博客文章。

## 安装

```bash
pip install corecoder
```

选你的模型，任何 OpenAI 兼容 API 都行。可以 `export` 环境变量，也可以在项目根目录放一个 `.env` 文件：

```bash
# Kimi K2.5
export OPENAI_API_KEY=你的key OPENAI_BASE_URL=https://api.moonshot.ai/v1
corecoder -m kimi-k2.5

# Claude Opus 4.6（通过 OpenRouter）
export OPENAI_API_KEY=你的key OPENAI_BASE_URL=https://openrouter.ai/api/v1
corecoder -m anthropic/claude-opus-4-6

# OpenAI GPT-5
export OPENAI_API_KEY=sk-...
corecoder -m gpt-5

# DeepSeek V3
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com
corecoder -m deepseek-chat

# Qwen 3.5
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
corecoder -m qwen-max

# Ollama（本地）
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1
corecoder -m qwen3:32b

# 单次模式
corecoder -p "给 parse_config() 加上错误处理"
```

## 架构

整个项目一目了然：

```
corecoder/
├── cli.py            REPL + 命令                   160 行
├── agent.py          Agent 循环 + 并行执行          120 行
├── llm.py            流式客户端 + 重试              150 行
├── context.py        三层压缩                       145 行
├── session.py        会话保存/恢复                   65 行
├── prompt.py         系统提示词                      35 行
├── config.py         环境变量配置                    30 行
└── tools/
    ├── bash.py       Shell + 安全 + cd 追踪          95 行
    ├── edit.py       搜索替换 + diff                  70 行
    ├── read.py       文件读取                         40 行
    ├── write.py      文件写入                         30 行
    ├── glob_tool.py  文件搜索                         35 行
    ├── grep.py       内容搜索                         65 行
    └── agent.py      子代理生成                       50 行
```

## 当库用

```python
from corecoder import Agent, LLM

llm = LLM(model="kimi-k2.5", api_key="your-key", base_url="https://api.moonshot.ai/v1")
agent = Agent(llm=llm)
response = agent.chat("找出项目里所有 TODO 注释并列出来")
```

## 加自定义工具（约 20 行）

```python
from corecoder.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "请求一个 URL。"
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def execute(self, url: str) -> str:
        import urllib.request
        return urllib.request.urlopen(url).read().decode()[:5000]
```

## 命令

```
/model <名称>    切换模型
/compact         压缩上下文（对标 Claude Code 的 /compact）
/tokens          查看 token 用量
/save            保存会话
/sessions        列出已保存的会话
/reset           清空历史
quit             退出
```

## 对比

|  | Claude Code | Claw-Code | Aider | CoreCoder |
|---|---|---|---|---|
| 代码量 | 51万行（闭源） | 10万+行 | 5万+行 | **1300 行** |
| 模型 | 仅 Anthropic | 多模型 | 多模型 | **任意 OpenAI 兼容** |
| 能通读吗？ | 不能 | 很难 | 有点费劲 | **一个下午** |
| 适合 | 直接用 | 直接用 | 直接用 | **先看懂，再造自己的** |

## 源码导读

我还写了 [7 篇 Claude Code 架构深度导读](article/)：Agent 循环、工具系统、上下文压缩、流式执行、多 Agent、隐藏功能。想知道 CoreCoder 为什么这样设计，从那里开始。

## License

MIT。Fork，然后拿去造更好的东西，如果能标注此出处就更好了。

---

作者 **[何宇峰](https://github.com/he-yufeng)** · Agentic AI Researcher @ Moonshot AI (Kimi)

[Claude Code 源码分析（知乎 17 万阅读，6000收藏）](https://zhuanlan.zhihu.com/p/1898797658343862272)
