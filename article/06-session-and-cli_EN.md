# Turning it into a real command-line tool

The first five pieces dissected the agent's internals: the loop, the tools, the model interface, context compression, parallelism and sub-agents. As lovely as these parts are, you can't use them directly, because one more layer is missing, a skin, a command-line interface where someone can sit down and talk to it, save, resume, and check status. This piece covers that skin, corresponding to `cli.py` (393 lines) and `session.py` (97 lines).

It's not just "a nice-to-have UI." Hidden in this skin is a security detail well worth discussing, which we save for the finale.

## REPL: a conversation loop wrapped around a conversation loop

The body of `cli.py` is `_repl`, a `while True`. Note it and the agent main loop from piece one are two different layers: this layer is "read user input, hand to the agent, print the reply, read the next line," the human-interaction loop; the layer inside the agent is "ask the model, run tools," the task-execution loop. One human input often triggers several turns inside the agent.

The input part uses `prompt_toolkit`, with history, plus a rather considerate custom keybinding:

```python
@kb.add("enter")
def _submit(event):
    event.current_buffer.validate_and_handle()

@kb.add("escape", "enter")
def _newline(event):
    event.current_buffer.insert_text("\n")
```

Enter submits directly, Esc then Enter is a newline. This way, pasting a multi-line block of code in won't get submitted prematurely the moment you hit the second line. A small detail, but once you've used it there's no going back.

After getting the user input, what actually calls the agent is these few lines, where the two callbacks from pieces one and three land:

```python
def on_token(tok):
    streamed.append(tok)
    print(tok, end="", flush=True)

def on_tool(name, kwargs):
    console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

response = agent.chat(user_input, on_token=on_token, on_tool=on_tool)
```

`on_token` makes the model's text surface in real time one piece at a time, which is what piece three's streaming layer looks like on the interface. `on_tool` prints a gray line before each tool call, telling you "it's about to read which file, run which command." These two callbacks are the only coupling point between the agent core and the interface; the core doesn't care how you display, it just shouts when an event should fire, and how the interface presents it is the interface's business. This "core emits events, shell handles presentation" split is clean, and to embed CoreCoder into another program (say a web service) you only swap these two callbacks, with the core untouched by a single line. Piece seven uses this property.

## Slash commands: managing state without breaking the conversation

The REPL recognizes a set of slash commands that aren't sent to the model but directly operate on the agent's state:

```
/help      show help
/reset     clear conversation history
/model     view or switch model
/tokens    show token usage and estimated cost
/compact   manually trigger context compression
/diff      list files changed this session
/save      save the session to disk
/sessions  list saved sessions
/mcp       MCP server status, and reconnect a dead one
```

These commands expose the core capabilities from the previous pieces as switches the user can flip directly. `/tokens` calls piece three's `estimated_cost`, `/compact` calls piece four's `maybe_compress`, `/diff` reads the "changed files" set that piece two's `edit_file` has been maintaining all along. The core prepared these capabilities long ago, and the CLI just gives each one a handy entry point.

There's an unremarkable judgment here that shows taste. What should happen to an input that starts with `/` but isn't in the list above?

```python
# an unknown /command shouldn't be sent to the model as a prompt
if user_input.startswith("/"):
    console.print(f"[yellow]Unknown command: {user_input.split()[0]} (try /help)[/yellow]")
    continue
```

It won't take `/qiut` (a fat-fingered "quit") as a sentence to send the model; instead it prompts "no such command." Without this interception, mistype a slash command and the model would earnestly interpret it as a task, wasting a round of calls and possibly doing something baffling. Telling "the user obviously meant to type a command but got it wrong" apart from "the user is really giving a task" is a small but very real bit of consideration in interaction design.

## One-shot mode: making it scriptable

Besides the interactive REPL, the CLI has a `-p` one-shot mode that runs one prompt and exits, handy for slotting into a script or a pipe:

```python
def _run_once(agent, prompt):
    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
```

Note the exit codes. Interrupted by Ctrl+C exits 130 (the conventional exit code for "interrupted by a signal" on Unix), an error exits 1, normal exits 0. A command-line tool that wants to be scriptable must take exit codes seriously, because the caller relies on this number to judge whether you succeeded. This is again the kind of detail "a demo won't bother with, a product must."

## The finale: don't let a session name tear up your filesystem

Now for the thing in `session.py` I most want you to remember.

Saving a session looks like the most harmless feature there is: dump `messages` and the model name to JSON and write it to disk, then load it back when reading. The filename uses the session id. The problem is exactly that id, which can come from the user. After `/save`, you resume with `corecoder -r <id>`, and that `<id>` is an arbitrary string the user types on the command line.

Imagine the most naive implementation: `SESSIONS_DIR / f"{session_id}.json"`. What if the user (or some upstream program feeding it a session name) sets the id to `../../etc/passwd`? That path resolves outside the session directory, your "save session" becomes "write a file to an arbitrary location," and "read session" becomes "read an arbitrary file." This is the classic path-traversal vulnerability that countless real systems have fallen to.

CoreCoder guards against it with two gates, defense in depth. The first regularizes the id into a safe, plain filename:

```python
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")

def _normalize_session_id(session_id):
    if not session_id:
        return _new_session_id()
    name = session_id.strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_SESSION_RE.sub("-", name).strip(".-_")
    if len(name) > _MAX_SESSION_ID_LEN:
        name = name[:_MAX_SESSION_ID_LEN].strip(".-_")
    return name or _new_session_id()
```

It first unifies backslashes into forward slashes (blocking Windows's `..\..\` form), then uses `split("/")[-1]` to take only the last segment, throwing away all the directory parts. So `../../etc/passwd` takes `passwd`, and `/etc/shadow` takes `shadow`. Then it replaces every character that isn't alphanumeric or `._-` with `-`, and trims off anything too long. A malicious path goes in, and out comes an ordinary filename that stays obediently inside the session directory.

The second gate is a backstop in case the first somehow has a gap. After `_session_path` resolves the final path, it explicitly checks that its parent directory is the session directory itself:

```python
def _session_path(session_id):
    path = (SESSIONS_DIR / f"{_normalize_session_id(session_id)}.json").resolve()
    root = SESSIONS_DIR.resolve()
    if root != path.parent:
        raise ValueError("Invalid session id")
    return path
```

`resolve()` flattens all the `..` and symlinks in the path into a real absolute path, and then one `root != path.parent` slams it shut: as long as the final landing spot isn't sitting directly inside the session directory, it's rejected immediately.

Why two gates, isn't the first already enough? Because with security, single-point defense is fragile. The first gate is based on "sanitizing input," and if one day someone changes that regex and misses an attack form, the second gate, based on "validating the output's landing spot," still catches it, and vice versa. The two gates use completely different approaches (one handles input, one handles output), so they won't fail together. This is the essence of defense in depth: not counting on any single line of defense being absolutely reliable, but stacking several lines of defense with different principles so the attacker has to fool them all at once. This defense is watched by a string of tests, path traversal, absolute paths, Windows backslashes, overlong names, verifying one by one that the attack strings going in all become well-behaved filenames.

By the way, `load_session` is also restrained about bad files:

```python
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["messages"], data["model"]
except (json.JSONDecodeError, KeyError, OSError):
    # a corrupt or truncated session file shouldn't crash resume
    return None
```

A session file written halfway and cut off by a power loss shouldn't crash in your face the next time you `-r` to resume; it should quietly return `None` and let the upper layer prompt "no such session found." It's handled of a piece with everything before: bad data degrades quietly, don't throw a traceback in the user's face.

## Compared with Claude Code

Claude Code's session and query engine (over a thousand lines in public teardowns) is far more complex; the state it manages, the terminal environments it accommodates, the concurrent sessions it handles are all far more numerous. But CoreCoder's version gathers what "a usable CLI agent" needs: streaming display, tool-event prompts, a set of state-managing commands, one-shot mode, session save and load, plus one proper line of security defense. The benefit of reading it is that you see in full where the seam between "core" and "shell" is, and how much a careful person thinks about behind a seemingly harmless feature (saving a session).

## What this piece leaves you with

- The CLI is a shell on top of the core. The core emits events (`on_token`, `on_tool`), the shell handles presentation, the two couple only through callbacks, and swapping the shell needs no change to the core.
- Slash commands expose core capabilities as switches the user can flip directly; intercept mistyped slash commands and don't send them to the model as a task.
- To be scriptable, take exit codes seriously: interrupt 130, error 1, normal 0.
- A session id can come from the user, and path traversal is a real threat. Meet it with defense in depth: one gate sanitizes input, one validates the landing spot, and the two differ in approach so they won't fail together.
- Bad data should degrade quietly: a corrupt save returns `None`, don't slam the user with a traceback.

The next piece is the finale, and the most hands-on: reassembling the parts dissected across these six pieces, walking you through forking CoreCoder into a coding agent that's genuinely your own.
