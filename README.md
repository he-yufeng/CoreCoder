# CoreCoder

> Formerly **NanoCoder** — renamed to avoid confusion with [Nano-Collective/nanocoder](https://github.com/Nano-Collective/nanocoder). All links from the old repo redirect here automatically.


[![PyPI](https://img.shields.io/pypi/v/corecoder)](https://pypi.org/project/corecoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/CoreCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/CoreCoder/actions)

[中文](README_CN.md) | [English](README.md) | [Claude Code Architecture Deep Dive (7 articles)](article/)

**512,000 lines of TypeScript → 950 lines of Python.**

I spent two days reverse-engineering the leaked Claude Code source — all half a million lines. Then I stripped it down to the load-bearing walls and rebuilt them in Python. The result: **every key architectural pattern from Claude Code, in a codebase you can read in one sitting.**

CoreCoder is not another AI coding tool. It's a **blueprint** — the [nanoGPT](https://github.com/karpathy/nanoGPT) of coding agents. Read it, fork it, build your own.

---

```
$ corecoder -m kimi-k2.5

You > read main.py and fix the broken import

  > read_file(file_path='main.py')
  > edit_file(file_path='main.py', ...)

--- a/main.py
+++ b/main.py
@@ -1 +1 @@
-from utils import halper
+from utils import helper

Fixed: halper → helper.
```

## What You Get

Claude Code's 512K lines distilled to 7 patterns that actually matter:

| Pattern | Claude Code | CoreCoder |
|---|---|---|
| Search-and-replace editing (unique match + diff) | FileEditTool | `tools/edit.py` — 70 lines |
| Parallel tool execution | StreamingToolExecutor (530 lines) | `agent.py` — ThreadPool |
| 3-layer context compression | HISTORY_SNIP → Microcompact → CONTEXT_COLLAPSE | `context.py` — 145 lines |
| Sub-agent with isolated context | AgentTool (1,397 lines) | `tools/agent.py` — 50 lines |
| Dangerous command blocking | BashTool (1,143 lines) | `tools/bash.py` — 95 lines |
| Session persistence | QueryEngine (1,295 lines) | `session.py` — 65 lines |
| Dynamic system prompt | prompts.ts (914 lines) | `prompt.py` — 35 lines |

Every pattern is a real, runnable implementation — not a diagram or a blog post.

## Install

```bash
pip install corecoder
```

Pick your model — any OpenAI-compatible API works. You can `export` env vars or drop a `.env` file in your project root:

```bash
# Kimi K2.5
export OPENAI_API_KEY=your-key OPENAI_BASE_URL=https://api.moonshot.ai/v1
corecoder -m kimi-k2.5

# Claude Opus 4.6 (via OpenRouter)
export OPENAI_API_KEY=your-key OPENAI_BASE_URL=https://openrouter.ai/api/v1
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

# Ollama (local)
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1
corecoder -m qwen3:32b

# One-shot mode
corecoder -p "add error handling to parse_config()"
```

## Architecture

The whole thing fits in your head:

```
corecoder/
├── cli.py            REPL + commands               160 lines
├── agent.py          Agent loop + parallel tools    120 lines
├── llm.py            Streaming client + retry       150 lines
├── context.py        3-layer compression            145 lines
├── session.py        Save/resume                     65 lines
├── prompt.py         System prompt                   35 lines
├── config.py         Env config                      30 lines
└── tools/
    ├── bash.py       Shell + safety + cd tracking    95 lines
    ├── edit.py       Search-replace + diff            70 lines
    ├── read.py       File reading                     40 lines
    ├── write.py      File writing                     30 lines
    ├── glob_tool.py  File search                      35 lines
    ├── grep.py       Content search                   65 lines
    └── agent.py      Sub-agent spawning               50 lines
```

## Use as a Library

```python
from corecoder import Agent, LLM

llm = LLM(model="kimi-k2.5", api_key="your-key", base_url="https://api.moonshot.ai/v1")
agent = Agent(llm=llm)
response = agent.chat("find all TODO comments in this project and list them")
```

## Add Your Own Tools (~20 lines)

```python
from corecoder.tools.base import Tool

class HttpTool(Tool):
    name = "http"
    description = "Fetch a URL."
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def execute(self, url: str) -> str:
        import urllib.request
        return urllib.request.urlopen(url).read().decode()[:5000]
```

## Commands

```
/model <name>    Switch model mid-conversation
/compact         Compress context (like Claude Code's /compact)
/tokens          Token usage
/save            Save session to disk
/sessions        List saved sessions
/reset           Clear history
quit             Exit
```

## How It Compares

|  | Claude Code | Claw-Code | Aider | CoreCoder |
|---|---|---|---|---|
| Code | 512K lines (closed) | 100K+ lines | 50K+ lines | **1,300 lines** |
| Models | Anthropic only | Multi | Multi | **Any OpenAI-compatible** |
| Readable? | No | Hard | Medium | **One afternoon** |
| Purpose | Use it | Use it | Use it | **Understand it, build yours** |

## The Deep Dive

I wrote [7 articles](article/) breaking down Claude Code's architecture — the agent loop, tool system, context compression, streaming executor, multi-agent, and 44 hidden feature flags. If you want to understand *why* CoreCoder is designed this way, start there.

## License

MIT. Fork it, learn from it, ship something better. A mention of this project is appreciated.

---

Built by **[Yufeng He](https://github.com/he-yufeng)** · Agentic AI Researcher @ Moonshot AI (Kimi)

[Claude Code Source Analysis — 170K+ reads, 6000 bookmarks on Zhihu](https://zhuanlan.zhihu.com/p/1898797658343862272)
