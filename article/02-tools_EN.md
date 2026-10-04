# The tool system: letting the model act, safely

In the loop from the last piece, one step got glossed over: executing tools. This piece unpacks it.

The model itself does only one thing, emitting the next text given the text so far. It can't read your files, can't run your tests, can't write a single byte to disk. What turns it from "able to talk" into "able to do" is tools. A tool is the hand through which an agent actually touches the world. So how strong an agent is depends largely on how well its tools are designed: whether the interface is clear, whether the error feedback lands, whether dangerous operations get stopped.

CoreCoder gives the model eight tools: `bash`, `read_file`, `write_file`, `edit_file`, `glob`, `grep`, `todo_write`, `agent`. In this piece we first look at the skeleton they share, then dig into the two most worth discussing, and finally I'll have you write one of your own.

## What a tool looks like

Every tool inherits from `Tool` in `tools/base.py`, the whole base class being 32 lines:

```python
class Tool(ABC):
    """Minimal tool interface. Subclass this to add new capabilities."""

    name: str
    description: str
    parameters: dict  # JSON Schema for the function args

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """Run the tool and return a text result."""
        ...

    def schema(self) -> dict:
        """OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
```

A tool is four things: a name, a description for the model to read, a JSON Schema describing the parameters, and an `execute` that does the actual work. `schema()` assembles the first three into the shape OpenAI function calling wants and sends it to the model, which decides from it whether to call and how to fill the arguments.

There's a design choice here worth one remark. CoreCoder has no tool inheritance hierarchy, no `FileTool` deriving `ReadTool` deriving whatever. Each tool is a direct subclass of `Tool`, minding its own business. Claude Code goes further: in public teardowns it doesn't use class inheritance at all, but a `buildTool()` factory function that takes name, schema, execution logic, and permission check as configuration and assembles a tool object. Both rest on the same judgment: tools share little genuinely common behavior, and forcing inheritance only adds coupling. Composition over inheritance shows up especially cleanly here.

Registering a tool is just as plain, a list in `tools/__init__.py`:

```python
ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    AgentTool(),
]
```

To add a tool, drop an instance into this list. We'll actually do that at the end of this piece.

## edit_file: a key innovation that looks unremarkable

If I could only discuss one of the eight tools, I'd pick `edit_file`. Because "let the model modify an existing file," a need that looks simple, has several dead bodies behind it.

The first dead end is having the model patch by line number, say "replace line 42 with this." The problem is the model's sense of line numbers is wildly unreliable; the line 42 in its head and the real line 42 in the file often don't match, and being off by one means editing the wrong place. Worse, the moment something earlier in the file gets touched, every line number after it shifts.

The second dead end is having the model rewrite the whole file and send it back. Fine for small files, but once a file is large it's slow and expensive, and while the model is transcribing the parts it shouldn't touch, it'll occasionally fumble a character or two, and you'll have a hard time noticing.

The third dead end is having the model produce a standard diff/patch format. It sounds elegant, but in practice the model's unified diffs, with those `@@ -42,7 +42,8 @@` line-number headers, have an exasperating error rate; it can't get those context-line counts and offsets right.

Claude Code's solution, which CoreCoder copies wholesale, is the fourth path: search and replace, plus a uniqueness constraint. The model gives a chunk of "text to find" and a chunk of "text to replace it with"; the tool finds that text in the file, requires it to appear exactly once, then replaces it. Here's the core of `tools/edit.py`:

```python
occurrences = content.count(old_string)

if occurrences == 0:
    preview = content[:500] + ("..." if len(content) > 500 else "")
    return (
        f"Error: old_string not found in {file_path}.\n"
        f"File starts with:\n{preview}"
    )
if occurrences > 1:
    return (
        f"Error: old_string appears {occurrences} times in {file_path}. "
        f"Include more surrounding lines to make it unique."
    )

new_content = content.replace(old_string, new_string, 1)
```

The brilliance is all in that "exactly once."

If the text isn't found at all, it means the model misremembered the content; the tool doesn't guess, it pastes the file's opening back so the model can re-check. If the text appears more than once, the tool refuses, because it can't be sure which occurrence the model meant, so it replies "this text appears N times, include more surrounding lines to make it unique." That sentence isn't just an error; it's teaching the model how to fix its request: on the next round the model will dutifully expand `old_string` to include enough context until it's unique across the file.

This constraint turns "editing a file" from a fuzzy problem into a determinate one. The model doesn't need to understand line numbers, doesn't need to compute offsets; it only needs to quote verbatim a chunk of the code it wants to change, and the tool guarantees that quote is unambiguous. Claude Code's system prompt specifically reminds the model that "old_string must be unique in the file," and CoreCoder's prompt has the same rule. This is a model design that trades constraint for reliability.

After replacing, the tool also generates a unified diff to return to the model and the user:

```python
diff = _unified_diff(content, new_content, str(p))
return f"Edited {file_path}\n{diff}"
```

Notice the direction reversed: having the model generate a diff is unreliable, but having the tool generate a diff for the model to read is perfectly reliable. The model looks at this diff and confirms whether it edited correctly. Generation on the tool side, consumption on the model side, each doing its own job.

There's also an easily overlooked corner: confirm the file is UTF-8 text before editing.

```python
try:
    content = p.read_text(encoding="utf-8")
except UnicodeDecodeError:
    return f"Error: {file_path} is not a UTF-8 text file (edit_file only edits text files)"
```

Without this check, the moment the model accidentally runs `edit_file` on a binary file, what it gets back is a big lump of Python decode traceback, which pollutes context and helps nobody. With it, the model gets a sentence it can understand. Every error message handed to the model should be plain language, an implicit rule running through all of CoreCoder's tools.

## bash: keep dangerous operations out, but don't pretend it's a sandbox

Tools like `read_file` and `edit_file` can only do limited damage. `bash` is different; it runs arbitrary shell commands, and the moment the model writes `rm -rf /`, the consequences are real.

Claude Code's `BashTool` is 1,143 lines in public teardowns, with a command classifier, a real sandbox built on `sandbox-exec` and `seccomp`, output truncation, and interactive-command interception. CoreCoder's `bash.py` is a 203-line distillation that keeps the four most essential things: dangerous-command detection, output truncation, timeout, and working-directory tracking.

Dangerous-command detection is a regex blocklist:

```python
_DANGEROUS_PATTERNS = [
    # recursive delete aimed at root/home (force flag optional)
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    # recursive (-r/-R) and force (-f) flags together, in any order or spacing
    (r"\brm\b(?=(?:.*\s)?-\w*[rR])(?=(?:.*\s)?-\w*f)", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    # ... plus block-device overwrite, chmod 777 on root, fork bombs, curl/wget pipe-to-shell, and so on
]
```

Run a command through this table before executing, and on a hit block it outright, without even running the command:

```python
warning = _check_dangerous(command)
if warning:
    return f"⚠ Blocked: {warning}\nCommand: {command}\n..."
```

I want to say a bit more about those two `rm` regexes, because they show whether the person writing a blocklist actually thought about the adversary. The first targets "recursive delete aimed at root or home," and note the force flag is written as optional, because `rm -r /` without `-f` is just as dangerous. The second uses two lookahead assertions requiring both `-r` (or `-R`) and `-f` to appear in the command, regardless of their order and spelling. That's because `rm -rf`, `rm -fr`, `rm -r -f`, `rm -f -r` are four spellings of the same thing, and a naive literal match on `rm -rf` would miss the latter three. The test `test_bash_blocks_rm_force_recursive_variants` feeds these variants, along with the long-form `--recursive --force`, in one by one and verifies each gets blocked. At the same time it must let through a normal `rm -f notes.log` or `rm -r ./build_output`, without swinging the bat at every `rm`.

Here a boundary needs drawing clearly: **this blocklist is not a security boundary, it's just a guard against slips of the hand.** A regex blocklist inherently can't stop a determined adversary; a command can be base64-encoded, assembled from variables, evaded a hundred ways. What it can stop is the most common, most direct catastrophe command the model generates in a moment of confusion; it can't stop deliberate attack. The reason Claude Code reaches for a kernel-level sandbox like `seccomp` is precisely that the blocklist road is a dead end for security. CoreCoder choosing a blocklist is a clear tradeoff between teaching clarity and real security: it lets you see at a glance what the "dangerous-operation interception" design point looks like, but it doesn't pretend to be a production-grade security scheme. If you take CoreCoder into an untrusted use case, a sandbox is the lesson you must supply yourself. [Piece seven](07-build-your-own_EN.md) comes back to this.

The other two things deserve a passing mention. Output truncation keeps head and tail: when a command spews tens of thousands of lines, only the first 6000 and last 3000 characters are kept, with one line of explanation standing in for the middle, which neither blows up the context nor loses the most useful opening and ending. Working-directory tracking lets `cd` be remembered across commands, and `_update_cwd` specially handles chained jumps like `cd a && cd b`, resolving b relative to a rather than relative to the starting point (the test `test_bash_chained_cd_resolves_sequentially` watches it). These are small pits that "running commands" throws up in real use, filled in one by one.

## Two phases: validate shape first, then validate safety

Stringing the last piece and this one together, CoreCoder actually puts a tool call through two gates. The first is in `agent._exec_tool`, using `inspect.signature().bind()` to validate whether the arguments fit the function signature, which validates "shape." The second is inside the tool, for example `bash`'s dangerous-command detection or `edit_file`'s UTF-8 check, which validates "whether it should actually be done."

This corresponds to Claude Code's two-phase gating, which public teardowns call `validateInput` and `checkPermissions`: one validates whether the input is legal, the other validates whether the operation is allowed. Splitting "is the format right" and "should it be done" into two gates means each failure gives its own precise feedback, and the model can correct against it specifically. One big merged try-except can't reach that precision.

## Beyond the gates: plan mode, hooks, and tools that live in another process

v0.6.0 added three things along the seam around the call. None of them changes a tool itself; they change who else gets a say between "the model wants to call" and "it actually runs."

The first is plan mode, a third gate with the highest rank. In `agent.py` it is one line: `self.plan_mode and tc.name not in Permission.READ_ONLY`, and a hit refuses the call — it outranks even `--yes`. That ordering is deliberate: a user who turns plan mode on wants the agent to read the code read-only first, and no "I already authorized this" may override. The refusal message is not a bare error either; it explains what plan mode is, so the model knows it should present a plan first. Read first, mutate later is a pattern worth remembering because it is nearly free: one boolean checked ahead of the allowlist.

The second is hooks. `~/.corecoder/hooks.json` lets users hang shell commands on tool calls, in PreToolUse and PostToolUse flavors. A pre hook fires before the consent check (in `agent.py` the ordering is literally `_pre_hooks(tc) or self._permit(tc)`); exit code 2 vetoes the call, and the reason travels from stderr back to the model verbatim, so the model gets text it can understand and fix against, not a stack trace. Post hooks only observe and can never block. The more important tradeoff: a hook that hangs, times out, or exits wrong gets one warning and is skipped. Hooks advise; they never get to kill the loop — the loop's life is not handed to a shell snippet the user dashed off.

The third is MCP. In `mcp.py`, each server is a subprocess speaking newline-delimited JSON-RPC over stdio: handshake, `tools/list`, and then every remote tool registers as `mcp__server__tool`, after which consent, hooks, and plan mode treat it exactly like the built-ins. The lesson is where that "exactly like" comes from: not from special-casing in the MCP code, but from a tool boundary (the `Tool` base class plus the three gates) clean enough that an external tool is just one more implementation. A wedged or dead server costs one error string on that one call; the loop keeps turning.

All three are advanced pieces. They are not on the agent's skeleton — the skeleton is still the loop plus eight tools. But they answer the same question: around a tool call, who else gets to speak? The answer went from "two gates" to "two gates plus a ring of pluggable bypasses," and every bypass is designed to fail without dragging the main loop down with it.

## Hands-on: write your first tool

After all that talk, better to actually add one. Suppose we want to give the agent the ability to check the current time (the model doesn't know what time it is on its own). Create `corecoder/tools/now.py`:

```python
"""A tool that tells the agent the current time."""

import time
from .base import Tool


class NowTool(Tool):
    name = "now"
    description = "Get the current local date and time. Use this when the user asks about the current time or you need a timestamp."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def execute(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")
```

Then register it in `tools/__init__.py`:

```python
from .now import NowTool

ALL_TOOLS = [
    BashTool(),
    # ...the existing tools...
    NowTool(),
]
```

That's it. No other steps. Re-run `corecoder`, ask it "what time is it," and you'll see it call `now`, then answer you with the result.

Look back at what you wrote. `name` is the identifier the model uses to call it by name. `description` is the model's only basis for deciding "when should I use this tool," so this sentence should read like you're briefing a smart colleague who has no prior knowledge: tell it what the tool does and in what situations to use it. `parameters` is empty, because checking the time needs no arguments, but if your tool does need arguments, this is the JSON Schema the model fills in against. `execute` returns a string, and that string becomes a `tool` message fed back to the model verbatim.

The whole tool system's extensibility is concentrated in those few steps: define a class, register it, and the model uses it immediately. Growing a new hand for your agent costs that little. [Piece seven](07-build-your-own_EN.md) we'll write a tool far more useful than checking the time, putting this mechanism where it counts.

## What this piece leaves you with

- Tools are the agent's hand on the world, and how well they're designed directly sets the ceiling on the agent's ability.
- `edit_file`'s "unique search and replace" is the key innovation: with the constraint "the original text must be unique across the file," it turns the unreliable act of editing a file into a determinate operation, and even its error message teaches the model how to get the edit right.
- Having the model generate a diff is unreliable; having the tool generate a diff for the model to read is perfectly reliable. Generation and consumption each in their place.
- bash's regex blocklist is a guard against slips of the hand, not a security boundary. To truly face untrusted scenarios, you supply the sandbox yourself. Getting this clear matters more than pretending to be secure.
- Adding a tool costs almost nothing: one class plus one line of registration. That's where the agent's extensibility comes from.

Next piece, we look at the brain behind this hand, and how to plug in any provider's model while getting the bill right along the way.
