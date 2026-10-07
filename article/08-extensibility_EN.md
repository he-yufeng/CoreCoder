# Three Ways to Extend Without Touching the Loop: MCP, Hooks, and Plan Mode

By the end of article seven you had a working agent. Almost immediately you will want three things: plug in new tools (a filesystem service, a database service), run your own logic around tool calls (a check before every bash run), and occasionally make the agent read-only (a plan before any work). Those three wants are exactly the three additions in v0.6.0: `mcp.py` (237 lines), `hooks.py` (87 lines), and one boolean in `agent.py` about fifty lines deep. What they share is that none of them touches the main loop from articles one through six. This piece is about why "not touching the loop" is not restraint but the precondition that makes all three possible.

## MCP: cutting the protocol down to the slice an agent uses

The Model Context Protocol is the standard for calling external tool services from an agent. The full spec has lifecycle management, capability negotiation, resource subscriptions, prompt templates, and sampling callbacks. `mcp.py` keeps only the slice an agent actually exercises: spawn a process, handshake, ask what tools it has, call them, and shut it down at the end.

Servers live in `~/.corecoder/mcp.json`:

```json
{"mcpServers": {"fs": {"command": "npx", "args": ["-y", "some-fs-server", "/tmp"]}}}
```

Each server is a subprocess speaking JSON-RPC 2.0, one message per line over stdio. Startup sends `initialize`, then `tools/list` pulls the remote tools down, and each one is registered as `mcp__<server>__<tool>`. The rename does more than avoid collisions: article two established that a tool, inside the loop, is just a name, arguments, and a string result. A renamed remote tool takes the same registration path, so the consent layer, hooks, and the main loop treat it exactly like a built-in. Nothing downstream knows it came from outside.

The real engineering is two threading problems. First, tool calls run concurrently (article five), so when several threads call the same server, how do responses find their callers? A daemon thread owns stdout and parks every response in a `_responses` dict under its request id; the caller waits on its own id:

```python
threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()
```

Second, stdin is a single pipe, and two requests must never interleave their bytes, so a `_write_lock` serializes writes. One reader demultiplexing by id, one lock sequencing writes: that is the standard shape for talking both ways over one pipe, no queues and no event loop needed.

The most deliberate part is failure handling. What happens when a server hangs, dies, or answers with garbage?

```python
# a server that hangs or dies fails that one call as an ordinary
# error string; it never kills the loop
```

The failure surfaces as an ordinary error string from that one tool call. The model reads "this service is unavailable" and works around it; the main loop never even sees an exception. It is the same principle article three established for the model interface: the outside world can always break, and the loop must not break with it. And when a server does die, it stays down only until you ask otherwise: `/mcp` in the REPL lists every server with its state, and `/mcp reconnect <name>` spawns a fresh process and redoes the handshake. The tools the agent already holds route through the client, not the process, so they work again the moment the new handshake lands.

## Hooks: allowed to veto, never allowed to kill

MCP adds capabilities; hooks change the behavior of tools that already exist. They are defined in `~/.corecoder/hooks.json`:

```json
{"PreToolUse":  [{"matcher": "bash", "command": "python check.py"}],
 "PostToolUse": [{"matcher": "*",    "command": "./log.sh"}]}
```

Each hook is a shell command fed the tool call as JSON on stdin. A PreToolUse hook vetoes the call with exit code 2, and its stderr travels back to the model as the reason:

```python
if out is not None and out.returncode == 2:
    reason = out.stderr.strip() or "no reason given"
    return "Blocked by hook: " + reason
```

Notice where that reason goes: not to the user's screen but back to the model as the tool result. The hook is not blocking a person; it is redirecting what the model does next, so the reason only matters if it lands inside the model's context.

PostToolUse hooks only observe. There is also a safety net, because a hook is itself untrusted perimeter code that can error or hang:

```python
TIMEOUT = 10  # seconds; a hung hook must not hang the agent
```

On timeout the hook is skipped with a warning and the agent continues. The closing comment of the module states the whole design in one line: hooks assist the loop, they never get to kill it. A hook may veto a single call, but no hook can stop the agent. That is the same invariant as MCP's: everything at the perimeter is a guest, and guests may advise but never overturn the table.

## Plan mode: a fifty-line boolean that outranks the whole permission layer

The third feature is the smallest. One field in `agent.py`:

```python
self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused
```

Typing `/plan` flips it to True, and from then on every mutating tool is refused, with the priority explicitly pinned to the top:

```python
# plan mode outranks consent, even --yes: while it's on nothing mutates
if self.plan_mode and tc.name not in Permission.READ_ONLY:
```

Article two's `--yes` escape hatch loses here: plan mode outranks the user's own "approve everything". It looks backwards until you think about it: `--yes` buys fewer confirmations, it does not waive the right to hear the plan first. A constraint is only credible if even the most convenient back door is closed.

The refusal text is crafted with equal care, and it is an instruction written for the model, not the user:

```python
"Plan mode is on, so this call was refused: plan mode is "
"read-only tools, then present the plan and stop. The user can "
'approve it by typing "approve", or exit plan mode with /plan.'
```

Reading it, the model learns the correct move: keep investigating with read-only tools, then present the plan and stop, and wait for the user to type approve or `/plan` again. One boolean plus one carefully worded refusal produces the entire "plan first, act later" workflow. No state machine, no approval queue.

## Coda: why all three extensions need no kernel changes

Put the three side by side and they hang on one contract, which articles one through six kept quietly building:

1. Tools are uniform. Whatever the source (built-in or remote MCP), a tool is a name, arguments, and a string result, so consent, hooks, and plan mode can govern every tool with the same code.
2. Errors are ordinary return values. A dead MCP server, a timed-out hook, a refused call: each comes back to the model as a string, and none of them travels up to the main loop as an exception.
3. The loop cannot be killed by its perimeter. A dead server does not kill it, a stuck hook does not kill it, and plan mode merely turns "you may not" into a next step the model understands.

Together those three invariants are the structural reason extensions need no kernel work: anything that honors the contract can attach, and the kernel never has to know it exists. Article seven ended with you building your own agent; article eight's point is that when you want to extend it, the first question is which layer the idea belongs to — new tools (where MCP sits), new behavior (where hooks sit), or new rules (where plan mode sits) — and then holding the same contract. Hold it, and your kernel stays small too.

That closes the series for now. From the main loop outward to this perimeter, the deepest lesson distilled from 512K lines of Claude Code is not any single mechanism but the recurring theme: a good kernel is stingy. It commits to very few contracts and keeps the rest of the world outside.
