# Fork CoreCoder and build your own coding agent

Over the first six pieces we took CoreCoder apart all the way through. The loop, the tools, the model interface, context compression, parallelism, sub-agents, the CLI, every part laid out on the table. This piece puts them back together, and assembles them into one that's yours.

Between understanding and building sits a single act of doing. This piece carries you across. When you're done, you'll have in hand a coding agent that runs, plugs into the model you actually use, carries a tool you added yourself, and has been tuned to your temperament.

## Step zero: get a green baseline

First confirm the starting point is good.

```bash
# fork he-yufeng/CoreCoder to your own account on GitHub, then
git clone https://github.com/<your-username>/CoreCoder
cd CoreCoder
pip install -e .
python -m pytest tests/ -q
```

That last line should show 235 tests all green. Don't skip this step. It confirms your environment is clean, so that when you later break something you can be sure it was you who broke it, not the environment being broken to begin with. The tests this project ships with aren't decoration, they're your safety net while refactoring, and all those pits the earlier pieces kept mentioning (orphaned tool messages, concurrency races, path traversal) are pinned down by tests. Every time you change something next, you should come back and run this line.

## Step one: plug in your own model

CoreCoder defaults to `gpt-5.5`, but you may not want to use it. Piece three covered this: switching models means switching environment variables. During development I strongly suggest a local Ollama first, costing nothing, free to mess with:

```bash
# install Ollama, pull a coder model
ollama pull qwen2.5-coder

export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
export CORECODER_MODEL=qwen2.5-coder

corecoder
```

A local model is a notch weaker than a flagship, but it's more than enough for verifying "did my change keep the flow working," and it spares you from burning API money every time you debug a single tool. Once the logic is all correct, switch the environment variables to DeepSeek or something else and see the real effect.

## Step two: add a genuinely useful tool

In piece two we added a `now` that checks the time, too trivial. This time add something useful: let the agent fetch the text content of a web page or an API. With it, your agent can go read online docs, check what an endpoint returns, and its abilities open right up.

Create `corecoder/tools/fetch.py`:

```python
"""A read-only tool that fetches the text content of a URL."""

import urllib.request
from .base import Tool


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Fetch the text content of an http(s) URL. "
        "Use this to read documentation, API responses, or web pages."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http:// or https:// URL to fetch",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 15)",
            },
        },
        "required": ["url"],
    }

    def execute(self, url: str, timeout: int = 15) -> str:
        if not url.startswith(("http://", "https://")):
            return "Error: only http and https URLs are supported"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CoreCoder"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(1_000_000)  # read at most 1MB so a giant page can't blow up memory
                text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return f"Error fetching {url}: {e}"
        # too long: keep head+tail, the same trick as bash output truncation
        if len(text) > 8000:
            text = text[:6000] + f"\n... (truncated, {len(text)} chars) ...\n" + text[-1000:]
        return text
```

Register it in `tools/__init__.py`:

```python
from .fetch import FetchUrlTool

ALL_TOOLS = [
    BashTool(),
    # ...the existing ones...
    FetchUrlTool(),
]
```

Run it and ask "fetch https://raw.githubusercontent.com/he-yufeng/CoreCoder/main/README.md and tell me what this project does," and it'll call `fetch_url` and then explain it to you.

This little tool actually puts several lessons from the earlier pieces to use. Truncation keeping head and tail is the trick that recurred in pieces two and four. Decoding with `errors="replace"` and turning any exception into one line of plain language via a big try-except is also the "don't pass bad data's buck to the user" you've seen all along. And there's the one from piece five that matters most to take to heart: can your tool withstand being called concurrently? This `fetch_url` can, because it has no shared mutable state at all; each call brings its own URL and produces its own result, and two threads running it at once don't interfere. That isn't luck, it's design. A tool you can make stateless, don't add state to; that's the most effortless way to make your tool concurrency-safe by default.

But, just as piece two did with bash, I have to put one of this tool's soft spots on the table. This `fetch_url` has a real security weakness called SSRF: it can access any URL, which includes `http://localhost`, intranet addresses, and that notorious cloud metadata endpoint `http://169.254.169.254`. Using it yourself on your own machine, no big deal. But the moment you wire this agent into a scenario that executes a stranger's instructions, this tool becomes a hole through which someone probes your intranet. To plug it, you'd parse the URL into an IP inside `execute` and block private address ranges and loopback. I deliberately didn't write that part, to let you clearly see what this hole looks like rather than hiding it and pretending it doesn't exist. Every time you add a tool that can take an outward action, first ask "what's the worst this could be used for," and that habit is worth more than any specific piece of protective code.

## Step three: tune its temperament

An agent's behavioral style isn't in some if-else, it's in the system prompt. Open `corecoder/prompt.py`, and that `# Rules` block is your agent's "code of conduct." The original has a few:

```
1. Read before edit.
3. Verify your work.
4. Be concise.
```

These rules directly shape how it works. Want it more cautious, add "ask for confirmation before destructive operations." Want it to write Chinese comments, add "all code comments in Chinese." Want it to always run tests after editing, write rule 3 harder. Change one line of prompt and its whole work habit shifts. This is the single highest-leverage tuning knob in agent engineering, bar none, taking effect faster than changing any code. It's worth setting aside dedicated time to polish this prompt as part of the product.

## Step four: not just a CLI, use it as a library

CoreCoder's top level exports `Agent`, `LLM`, `Config`, which means you're not limited to its interactive terminal; you can use it as a library and build an agent of an entirely different form.

Here's an interesting example. Pieces one and five covered how each `Agent` only knows its own tool set. Exploiting this, we can assemble a "read-only" code-review agent that is physically incapable of changing your files or running commands, because we simply never gave it the write tool or bash:

```python
from corecoder import Agent, LLM
from corecoder.tools import get_tool

llm = LLM(
    model="deepseek-chat",
    api_key="sk-...",
    base_url="https://api.deepseek.com",
)

# only read, search, and find-files; no write_file, no bash
reviewer = Agent(
    llm=llm,
    tools=[get_tool("read_file"), get_tool("grep"), get_tool("glob")],
    max_rounds=15,
)

report = reviewer.chat(
    "Review corecoder/agent.py, find concurrency-related hazards, and list them."
)
print(report)
```

This `reviewer` is an agent safe enough that you'd dare run it inside automation. It can read through and search the entire codebase, but it can't change a single byte or run a single command, because those tools simply aren't in its tool set. This is exactly the usage planted back when piece one stressed "the tool set is instance-level": the cleanest way to constrain what an agent can do isn't to write a pile of rules begging it not to misbehave, but to withhold the capability at the source. The tools you give it are the whole boundary of what it can do.

Used as a library, you can build far more than a CLI: a review bot running in CI, a chat endpoint hooked into a web backend, a script batch-processing a heap of repos. The kernel is just over thirteen hundred lines, and what shell you wrap around it is up to you.

## Step five: pair your change with a test

After adding `fetch_url`, write it a test while you're at it, in the same style as the project's other tools. Add to `tests/test_tools.py`:

```python
def test_fetch_rejects_non_http():
    fetch = get_tool("fetch_url")
    r = fetch.execute(url="file:///etc/passwd")
    assert "only http" in r
```

Then `python -m pytest tests/ -q`, and watch it go from 227 to 228.

I single out this step because it's the watershed between "dabbling" and "doing it seriously." The most valuable designs in the first six pieces, the defense against orphaned tool messages, concurrency safety, the two gates against path traversal, all come paired with tests. These tests aren't a formality written for others to see; they solidify an invariant hidden in your head, so that the you of three months from now, or whoever takes over, knows immediately on breaking it. Every capability you add to the agent is worth a test to pin its boundary down. This is the work habit this project means to teach you along the way.

## Where you can go further

CoreCoder is a starting point, not a destination. It deliberately leaves blanks in quite a few places, and every one is a direction you can build out, and the earlier pieces mostly named them by name:

- **Put a real sandbox on bash.** Piece two said it plainly, the regex blocklist is only a guard against slips, not a security boundary. To face untrusted input, you need `seccomp` or container-level isolation.
- **Add a fallback model and a hard dollar budget.** Piece three covered how CoreCoder deliberately skipped these two, because they drag in provider-specific logic. For a production deployment, these two eventually have to be added.
- **Make concurrency finer-grained.** Piece five's point about distinguishing whether a tool "reads" or "writes" to decide whether it can run concurrently is something CoreCoder still doesn't do, and is worth filling in seriously.
- **Hook up MCP.** Let your agent plug into the Model Context Protocol tool ecosystem, instantly connecting to a large batch of ready-made external capabilities.
- **Give sub-agents more modes.** Piece five now covers named parallel workers with isolated contexts. Independent worktrees, asynchronous background execution and role-specific tool presets remain open directions.

Pick one you genuinely need and do it. Don't let the length of the list make you anxious; the charm of an agent is exactly that its core is small enough for one person to read through in a weekend, and its frontier is open enough that you can grow in any direction.

## A last word

If this series leaves you with only one thing, I hope it's this: a coding agent is less mysterious than it seems, it's just a stack of engineering decisions you can fully reach, piled up.

Looking back over the six pieces, it's really only a few blocks. A capped loop (piece one), plus a set of clean-interfaced tools that let it act (piece two). The model interface is a thin provider wrapper (piece three), and context fights forgetting with layered compression (piece four). It can split itself and run concurrently, on the strength of restraint about shared state (piece five); the outermost CLI emits events, handles presentation, and plugs the path-traversal hole while it's at it (piece six). The engine adds up to just over fourteen hundred lines, and even with that CLI shell on top the whole package is only 2,364, with not a single spot you can't understand.

Now you don't just understand it, you've forked it, plugged in your own model, added your own tool, and tuned its own temperament. It's yours.

Go give it some ability nobody else has.
