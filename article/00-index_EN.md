# Read Claude Code through CoreCoder, then build your own

This is a series for engineers. It sets out to do two things.

First, use CoreCoder, an open source project whose core runs to just over a thousand lines, to lay out clearly how a production-grade coding agent like Claude Code is built on the inside. Second, walk you through forking CoreCoder and turning it into a coding agent of your own.

## Why take the detour

Because Claude Code itself is too big. Its codebase runs into the hundreds of thousands of lines, and the BashTool alone, the part that runs shell commands, is over a thousand. Read it line by line and most people close the editor by the third file. Yet the core of an agent isn't really that complicated. What's complicated is everything engineered around it to survive the real world: what to do when the model gets interrupted mid-stream, when the context window fills up, when ten tool calls come back at once, when a provider hiccups and returns a 500. Spread across hundreds of thousands of lines, that skeleton is hard to see.

What CoreCoder does is squeeze the skeleton down to just over a thousand lines of pure Python. To be precise, the agent engine (the loop, model interface, context, tools, session) is 1201 lines once you drop blank lines and comments; add the outermost CLI terminal, config, and packaging and the whole package is 1614 lines without blanks and comments, 2008 physical. Every file is short enough to read in one sitting, and what runs is genuinely an agent: it edits code, runs commands, spins up its own subtasks, compresses context, tracks cost. It is not a toy. It takes every design decision a production agent makes, picks the most essential version of each, and writes it out honestly in the least code it can.

Reading it is close to reading a runnable, annotated edition of Claude Code. The difference is that every line of CoreCoder is something you can breakpoint, change, run, and watch on your own machine. Every line count and every snippet I quote in this series is read straight out of the repository, not recalled from memory. I'll keep being pedantic about that, because in the coding-agent space far too many write-ups just make the numbers up.

## Who this series assumes you are

I assume you've written code, called an API, roughly know how LLM function calling works, but have never actually cracked open an agent's main loop. You may have used Claude Code or Cursor, been impressed that it reads files, edits code, and runs tests on its own, and then wondered whether there's magic behind it or just engineering.

The answer is engineering. And the kind that, once you've read it, makes you think "that's it? I could write that too."

## How to read the eight pieces

The first six are about understanding. Each one fixes on a single subsystem of the agent, first describing how Claude Code handles that thing and the tradeoffs it makes, then turning to the real code in CoreCoder to see what the same idea looks like once it's compressed into a few dozen lines. The last piece is about building it yourself, wiring all the parts back together, from fork to a custom tool to swapping the model, landing on something that runs.

1. [An agent is, at heart, a while loop](01-the-loop_EN.md). The most central thing in the whole agent is a loop: ask the model, run a tool, feed the result back, ask again. We look at how CoreCoder's `agent.py` (150 lines) writes it out plainly, and how real-world headaches like interruption, a round cap, and backfilling half-finished tool calls each get handled.
2. [The tool system: letting the model act, safely](02-tools_EN.md). The model on its own only emits text. Tools are what let it read files, write files, run commands. This piece covers CoreCoder's seven tools, with the spotlight on the seemingly unremarkable unique search-and-replace edit that is in fact one of Claude Code's key innovations, plus bash's safety gate. At the end I'll have you write your first tool.
3. [Plug in any model, and get the bill right while you're at it](03-llm-and-cost_EN.md). How `llm.py` (336 lines, the largest single file in the project) uses one OpenAI-compatible interface to catch DeepSeek, Qwen, Kimi, and local Ollama, how it does exponential-backoff retry, and how it tallies tokens and dollar cost right inside the streaming output.
4. [Surviving a long task in a finite window](04-context_EN.md). The context window is the agent's hard constraint. `context.py` (210 lines) implements three layers of compression, lightest to heaviest. This piece also covers a trap that's easy to hit and that the API will always reject: the orphaned tool message. That's a bug I actually fixed while building this project.
5. [Parallel execution and sub-agents](05-parallel-and-subagents_EN.md). When the model returns several tool calls at once, CoreCoder runs them concurrently on a thread pool. This piece is honest about where this simplified version falls short of Claude Code's streaming executor, what new trouble concurrency brings in, and why a sub-agent is not allowed to recurse.
6. [Turning it into a real command-line tool](06-session-and-cli_EN.md). Session save, resume, slash commands, one-shot mode. `session.py` holds an unremarkable but critical security detail: how to stop a malicious session name from turning into a path traversal.
7. [Fork CoreCoder and build your own coding agent](07-build-your-own_EN.md). The hands-on finale. From clone, to switching to the model you actually use, to adding a genuinely useful custom tool, to tuning the system prompt to shape its style, to packaging and release. After the first six pieces you understand the principles; this one leaves you with something real.

You don't have to read in order. Want to understand how it dares to run commands on its own? Jump to piece two. Want to plug in your own model? Jump to piece three. Just want to fork something usable fast? Piece seven stands on its own.

## Five minutes to get it running first

Before you read, let it run once on your machine, so the code that follows has a feel to it.

```bash
git clone https://github.com/he-yufeng/CoreCoder
cd CoreCoder
pip install -e .
```

Then give it a model and a key. CoreCoder speaks the OpenAI-compatible interface by default, so a key from any provider works, and switching providers is just two environment variables:

```bash
# OpenAI
export OPENAI_API_KEY=sk-...

# or DeepSeek, much cheaper, change one base_url
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com CORECODER_MODEL=deepseek-chat

# or local Ollama, free of charge
export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder
```

Run it:

```bash
corecoder
```

Once you're in, give it any task, say "read corecoder/agent.py and tell me the most rounds this loop can run." You'll watch it call `read_file` first, then answer you with what it read. What it does in that moment is, in essence, the same thing Claude Code does. The next six pieces take that same thing apart.

So let's start with that loop.

---

Author: [He Yufeng](https://github.com/he-yufeng). I wrote an earlier piece, a [Claude Code source code analysis](https://zhuanlan.zhihu.com/p/1898797658343862272), which leans toward a full teardown; this series takes a different angle, more about getting you to rebuild it by hand.
