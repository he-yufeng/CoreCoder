<div align="center">

# CoreCoder

**The nanoGPT of coding agents. 1,081 lines of pure Python — understand how a coding agent actually works, then fork your own.**

*learn from it · fork it · ship something better*

[中文](README_CN.md) | English | [Source-reading series · 8 bilingual essays](article/00-index_EN.md)

[![PyPI](https://img.shields.io/pypi/v/corecoder)](https://pypi.org/project/corecoder/)
[![Python](https://img.shields.io/badge/python-3.10+-blue)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://github.com/he-yufeng/CoreCoder/actions/workflows/ci.yml/badge.svg)](https://github.com/he-yufeng/CoreCoder/actions)
[![engine](https://img.shields.io/badge/engine-1081_LoC-blue)](article/00-index_EN.md)
[![essays](https://img.shields.io/badge/source--reading-8_bilingual-orange)](article/00-index_EN.md)

</div>

- **Readable end to end.** Read the whole engine in an afternoon: 1,081 lines of pure Python, with no magic hidden anywhere you can't follow it.
- **Hackable.** Set a breakpoint on any line, change it, rerun, all on your own machine. It genuinely works, which makes this a living reference rather than a diagram. It just isn't meant to be your daily driver.
- **The gaps are the point.** It deliberately keeps only the minimal core; what's missing isn't half-finished, it's where you branch off and make it your own.

## What this is

I've always felt coding agents get talked about as if they were arcane. Strip a tool like Claude Code or Cursor all the way down and the core is a `while` loop wrapped around a large model, plus seven or eight tools that let it actually do things. The hard part was never the loop; it's everything the loop has to cope with once it meets the real world. CoreCoder is the minimal version that writes that core out honestly.

The engine (loop, model interface, context, tools, sessions) is 1,081 lines once you drop blank lines and comments. Counting the outer CLI, config and packaging too, the whole package is 18 files: 1,714 physical lines, 1,385 net, every one short enough to read in a single sitting.

And it really runs: reads and writes files, executes shell, spawns sub-agents, compacts context in three tiers, and tells you the tokens and dollars a run burned whenever you ask. 86 tests, all green. But the point of it running isn't to become your daily driver. It runs so the walkthrough can't lie: a reference that shows how an agent works has to actually work.

The code came out of a public teardown: open analyses have already exposed a lot of the load-bearing architecture inside production agents like Claude Code. I took the most essential layer and rewrote it honestly, in as little code as I could. So reading CoreCoder is roughly like reading a runnable, annotated take on how that kind of agent works, except it's only a minimal reimplementation, sitting right there on your machine for you to take apart and change.

<p align="center">
  <img src="https://raw.githubusercontent.com/he-yufeng/CoreCoder/main/assets/demo_en.png" width="760"
       alt="A real CoreCoder run: corecoder -p asks it to fix buggy.py; the agent reads the file, edits the code, runs it to confirm, and reports what it changed.">
</p>

<p align="center"><sub><i>These thousand lines really do run a full loop end to end: ask it to fix buggy.py and it reads the file, edits the code, runs it once to confirm, then reports back on its own. Watch it, then come back and read the code.</i></sub></p>

This README follows the same arc: the first half helps you **read it** (the code map, the main loop, eight essays), the second half helps you **fork it** and points at a few directions worth pushing further.

## Run it once first (five minutes before you read)

Before you read the source, get it running on your machine once to build some intuition. It's a foundation meant for forking, so the recommended path is to clone it and install editable, reading and changing as you go:

```bash
git clone https://github.com/he-yufeng/CoreCoder
cd CoreCoder
pip install -e .
```

If you just want to get it running first, `pip install corecoder` works too.

Give it a model and a key and it goes. It speaks the OpenAI-compatible API by default, and switching providers is usually just two environment variables:

| Provider | Example env vars |
|---|---|
| OpenAI (default `gpt-5.5`) | `OPENAI_API_KEY=sk-...` |
| DeepSeek | `OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com CORECODER_MODEL=deepseek-chat` |
| Local Ollama | `OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder` |

Kimi, Qwen and the like are the same two variables; for providers that don't even offer an OpenAI-compatible endpoint, the optional LiteLLM backend (`pip install "corecoder[litellm]"`) routes to a hundred-plus of them. The third essay goes into this in detail. The key can be `export`ed directly or dropped into a `.env` at the project root, which is loaded on startup. Then:

```bash
corecoder                                             # interactive REPL
corecoder -p "add error handling to parse_config()"   # one-shot mode, exits when done
```

## Read it: the code map

Laid out flat, the whole project is this big. Skim it before you clone and you'll know where everything is. This is the most concrete difference from Claude Code's hundreds of thousands of lines: you can read it like the table of contents of a book. Start from the main loop in `agent.py`; that's the heart of the whole agent.

```
corecoder/
├── agent.py        agent loop + parallel tool exec       150 lines   ← start here
├── llm.py          streaming client + retry + cost        336 lines
├── context.py      three-tier context compaction          210 lines
├── session.py      save / resume + path-traversal guard    97 lines
├── prompt.py       system prompt                           33 lines
├── cli.py          REPL + slash commands + one-shot       270 lines
├── config.py       env-var config                          57 lines
└── tools/
    ├── bash.py       shell + dangerous-command gate + cd  127 lines
    ├── edit.py       unique-match search/replace + diff    92 lines
    ├── grep.py       content search                        79 lines
    ├── glob_tool.py  filename matching                     47 lines
    ├── read.py       file read                             53 lines
    ├── write.py      file write                            38 lines
    ├── agent.py      sub-agent spawning                    58 lines
    └── base.py       tool base class                       27 lines
```

Seven tools: `bash`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, and `agent` (which spawns a sub-agent). Add the packaging files like `__init__` and `__main__` and the package is 18 files; strip the CLI shell and config and the engine itself is about 1,081 lines.

## A `while` loop is the whole agent

The whole of an agent fits in one sentence: hand the user's words to the model, run whatever tools it asks for, stuff the results back into the context, ask again, and keep going until it stops asking for tools and gives an answer. In code, that's about a dozen lines:

```python
# corecoder/agent.py · the main loop (trimmed skeleton)
def chat(self, user_input):
    self.messages.append(user_input)

    for _ in range(self.max_rounds):                   # bounded, so it can't run away
        reply = self.llm.chat(self.messages, self.tools)   # ask the model what to do next
        if not reply.tool_calls:                       # model wants no more tools
            return reply.text                          #   -> done, hand the answer back
        results = run_parallel(reply.tool_calls)       # tools requested -> run in parallel
        self.messages += results                       # feed results back, loop again

    return "(hit the round limit)"
```

That's the whole thing. The core skeleton is about twenty lines; counting parallel execution and the bookkeeping after a Ctrl+C interrupt, maybe forty. Almost everything else in CoreCoder's thousand-odd lines is there to clean up the mess that shows up once the loop runs against the real world. `llm.py` is the biggest file in the project, not because calling a model is hard, but because in a streamed response a single tool call's arguments arrive in several fragments, one after another, and you have to stitch them back together in order. A provider will occasionally hand you half a JSON object, or fill the `usage` field with null; rate limits (429), timeouts, dropped connections and 5xx all need backoff-and-retry, while the other 4xx should just raise instead of being retried into the ground. Even an OpenAI extension like `stream_options` gets handled: some providers reject it outright with a 400, so the code strips it and resends exactly once, only on a 400, and never stacks that on top of the retry backoff. Lay all this grunt work out and it's the genuinely hard engineering part of taking an agent from demo to delivery. The unglamorous part turns out to be the loop itself; the thousand lines of fallback around it are where the real work lives.

Three decisions are worth a closer look, because they're the kind of call you can only make after you've understood how others did it, and they're judgments you can lift straight into your own fork.

**`edit_file` does search-and-replace on a unique match, not line numbers.** Line numbers are a trap: the model only has to miscount by one and it quietly edits the wrong place. Anchor on a unique snippet of the original instead. If there's no match, it hands the start of the file back so the model can re-anchor; if there are several matches, it makes the model bring more surrounding context rather than gamble on one. On a successful edit it returns a diff. Recoverable on failure, verifiable on success: the whole loop stays inside the tool.

**Context isn't cut all at once when it's full; it gives ground in three tiers, cheapest first.** At half full (50%) it trims over-long tool outputs in place, a tier that's purely mechanical and costs no model call. If 70% still isn't enough, it has the model summarize the older turns into a single paragraph while keeping the most recent ones verbatim. Only at 90% does it hit the emergency tier and pull everything, summary and recent turns alike, down to its tightest form. Blunt truncation tends to throw away exactly the early decision a long task leans on most; tiering lets it surrender the least important things first instead of lopping off the oldest decisions wholesale from the start.

**You constrain a sub-agent by withholding the tool, not by writing rules and hoping it obeys.** A spawned sub-agent gets an isolated context and its own separate history, with a toolset exactly one item shorter than the parent's: the `agent` tool itself, so it can't recursively spawn more sub-agents. Handing it one fewer tool is cleaner than legislating a rule after the fact. It also reuses the parent's model connection (its spend folded into the same running total), truncates its output once it runs past 5,000 characters down to just the opening, and runs on a shorter round limit than the parent. The same restraint, end to end.

Every one of these *whys* is traced down to the actual lines of code in the series below.

## The source-reading series · 8 bilingual essays

I also wrote a bilingual source-reading series, one intro plus seven parts, each in Chinese with an English mirror. Against CoreCoder's actual code, it walks through how agents like Claude Code work under the hood. One hard rule I set myself: every line count and every snippet is re-read and re-checked from the repo, never written from memory. The first six get you reading, the seventh gets you forking; read them in any order.

- **[Intro · Read Claude Code through CoreCoder, then build your own](article/00-index_EN.md)**
- **[01 · An agent, at its core, is a `while` loop](article/01-the-loop_EN.md)** — the main loop in `agent.py`, interrupts, and the round limit
- **[02 · The tool system: letting the model act, safely](article/02-tools_EN.md)** — the seven tools in `tools/` and the bash safety gate
- **[03 · Plug in any LLM, and keep the bill honest](article/03-llm-and-cost_EN.md)** — `llm.py`'s provider wrapper, retries, and cost accounting
- **[04 · Surviving a long task on a finite window](article/04-context_EN.md)** — `context.py`'s three-tier compaction and orphaned tool messages
- **[05 · Parallel execution and sub-agents](article/05-parallel-and-subagents_EN.md)** — thread-pool concurrency and sub-agent isolation
- **[06 · Turning it into a real command-line tool](article/06-session-and-cli_EN.md)** — `session.py` and path-traversal defense
- **[07 · Fork CoreCoder into your own coding agent](article/07-build-your-own_EN.md)** — from fork to custom tools to swapping models

## Fork it, build something better

Once you understand it, the natural next step is to fork. Getting started doesn't take much:

- **Swap in a model you actually use.** It's the two env vars from above; `llm.py` (336 lines) is the entry point for all provider adaptation.
- **Add a tool of your own.** Write a new file against the tool base class in `tools/base.py` (27 lines): run tests, fetch a page, call an LSP, whatever. The end of the second essay walks you through your first one by hand.
- **Rewrite the system prompt.** `prompt.py` is all of 33 lines; change one line and you'll watch the agent's temperament shift. It's the cheapest "change one thing, see a result" in the whole project.
- **Import it as a library.** The top level exports `Agent`, `LLM`, and `Config`, ready to embed in your own program:

```python
from corecoder import Agent, LLM

llm = LLM(model="deepseek-chat", api_key="sk-...", base_url="https://api.deepseek.com")
print(Agent(llm=llm).chat("find every TODO comment in this project and list them"))
```

Going deeper, the directions are out in the open too. None of the following is in CoreCoder, by design, not because it's unfinished. Flip it around and each one is an entry point you can carry into a real tool of your own:

- **The dangerous-command blocking in bash is just a regex blacklist.** It guards against slips, not a security sandbox. Facing untrusted input means reaching for seccomp or container isolation. This is the hardest of the four; it goes all the way down to the syscall and isolation layer.
- **Retry is only exponential backoff.** No fallback model, no hard dollar budget. Follow `llm.py` down and add a fallback model chain plus a stop-on-over-budget gate; the change stays mostly inside that one file.
- **Sub-agents only run the plainest synchronous execution.** Make it async or a streaming executor and you close the exact gap the fifth essay identifies between this and how production agents stream execution.
- **No MCP, no RAG.** Wire up MCP to give it the external tool ecosystem, or add retrieval-based code location for big repos. Both are real ways to grow from a minimal core into your own stronger agent.

The README only points; the seventh essay picks up the code details for each. Pick one and start; that's the whole reason the core is kept this small.

## How it compares

| | CoreCoder | Claude Code | aider | nanoGPT |
|---|---|---|---|---|
| Lines of code | ~1,081 engine / 1,714 total | hundreds of thousands (closed) | tens of thousands of Python | ~600 (two files) |
| Time to read it all | one afternoon | can't (closed) | a few days of slogging | one afternoon |
| Breakpoint, change, rerun? | yes, every line | no | yes, but there's a lot | yes |
| What it's for | understand one, then fork your own | production coding assistant | terminal pair-programming | minimal GPT for teaching |

The nanoGPT column is there as a reference point: minimal, readable, but it teaches you to train a GPT. CoreCoder is after the same thing, only the subject is an agent that actually edits code. Sitting it next to Claude Code and aider isn't about competing for their users. CoreCoder is the foundation you stand on while you learn from them and get going; it isn't in the same race.

## Commands

Inside the REPL, `/help` lists everything; these are the ones you'll reach for:

```
/model <name>    switch model
/compact         compact the context by hand
/tokens          token usage and cost estimate
/diff            files changed this session
/save  /sessions save / list sessions
quit / exit      exit (Ctrl+C cancels the current round)
```

Session IDs are sanitized to safe characters before they become filenames, every archive lands under `~/.corecoder/sessions`, and a malicious session name can't traverse out.

## Contributing / License

Before you send anything, run `pytest tests/ -q` (86 tests), `ruff check`, and `compileall`, and make sure they're green. MIT licensed: fork it, learn from it, ship something better. A mention of this project is appreciated.

---

By [Yufeng He](https://github.com/he-yufeng), formerly at Moonshot AI (Kimi). I earlier wrote a fairly complete [Claude Code source analysis](https://zhuanlan.zhihu.com/p/1898797658343862272) on Zhihu; this project is its hands-on counterpart: that one walks you through reading it, this one through rebuilding it.

> CoreCoder was formerly named NanoCoder; it was renamed to avoid confusion with [Nano-Collective/nanocoder](https://github.com/Nano-Collective/nanocoder), and old links redirect here automatically.
