# Parallel execution and sub-agents

When piece one covered the main loop, I left a gap at the "execute tools" step: the model wanting one tool at a time and the model wanting several at once take two different paths. This piece first fills that gap, then covers a tool that lets the agent spawn a "clone" of itself. The two are really two sides of one theme: how to let one agent handle several things at once without scrambling them together.

## Several tool calls come back at once

The model doesn't always want one tool at a time. Ask it "tell me what each of these three files does" and it'll very likely return three `read_file` calls in one go. Those three reads are independent of each other, and running them serially is dead waiting when they could clearly go together.

CoreCoder branches on exactly this in the main loop:

```python
if len(resp.tool_calls) == 1:
    tc = resp.tool_calls[0]
    # ...execute directly...
else:
    # parallel execution for multiple tool calls
    results = self._exec_tools_parallel(resp.tool_calls, on_tool)
```

A single one runs directly; multiple go to `_exec_tools_parallel`, a thread pool:

```python
def _exec_tools_parallel(self, tool_calls, on_tool=None) -> list[str]:
    for tc in tool_calls:
        if on_tool:
            on_tool(tc.name, tc.arguments)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(self._exec_tool, tc) for tc in tool_calls]
        return [f.result() for f in futures]
```

At most 8 threads run concurrently, each running one tool, then the results are collected in the original order. Three file reads that would have waited three rounds of IO now wait roughly one. For an agent that frequently reads several files at once, the time this parallelism saves is considerable.

Threads rather than processes or coroutines, because tools mostly do IO-bound work: reading disk, running subprocesses, waiting on the network. In this scenario Python's GIL isn't much of an obstacle, and a thread pool is the most effortless concurrency primitive in the standard library, done in a few lines. The choice is consistent with the project's "keep it simple where you can" tone.

## Where this simplification falls short of Claude Code

Here the boundary of the simplification needs drawing clearly. Claude Code's concurrent execution (public teardowns call it StreamingToolExecutor) is far more aggressive and far more refined. It does two things CoreCoder doesn't.

One is that it executes while generating. With the model still emitting later content, an already-formed tool call up front can start running, without waiting for the whole response to finish. CoreCoder dutifully waits for the model to finish this round and gets the complete tool-call list before it starts executing. Without this "speculative execution," what's saved is implementation complexity, what's lost is a little latency.

Two is that it distinguishes whether a tool is safe to run concurrently. In public teardowns Claude Code tags each tool with an `isConcurrencySafe`: read-only tools (read a file, search) can be run concurrently with confidence, while write tools (edit a file, run a command) need care. CoreCoder doesn't make this distinction; it throws a batch of tool calls into the thread pool all at once, regardless of whether they read or write.

The second point isn't just "a missing optimization," it hides a real problem.

## Parallelism isn't free: shared mutable state will bite

The place concurrent execution most easily goes wrong is multiple concurrent tasks sharing the same piece of mutable state. Without protection, they step on each other.

bash's working-directory tracking in CoreCoder is a perfect case in point. As the last piece mentioned, bash needs to remember where `cd` went across multiple commands, so it has to store the current directory somewhere. The most intuitive place is a module-level global variable:

```python
# the naive way to track cwd: a module-level global
_cwd: str | None = None
```

Under serial execution this is no problem at all; only one bash runs at a time, and reading and writing this global is orderly. But once it reaches the parallel branch and the model returns two bash calls at once, they read and write this same `_cwd` from different threads simultaneously. One call just set it to directory A, and the other call may read exactly that A, or turn around and overwrite it to B. Two commands that should each be independent get tangled together because they shared one piece of global state. This is a textbook race: it's correct ten thousand runs in a row, and then in the one run where concurrency and timing line up badly, it hands you a baffling, hard-to-reproduce error.

So CoreCoder doesn't store it that way. The clean solution is to isolate this kind of "each execution flow's own state" and not let them share. Python's ready-made tool is `threading.local()`, which gives each thread its own copy invisible to other threads, and it's the version `bash.py` actually uses:

```python
import threading
_local = threading.local()

# read: take this thread's own cwd, falling back to the process cwd
cwd = getattr(_local, "cwd", None) or os.getcwd()
# write: touch only this thread's own copy
_local.cwd = running
```

I pull this passage out on its own because it's a particularly good teaching point: **the moment you give an agent parallelism, you simultaneously impose a concurrency-correctness requirement on every tool that holds mutable state.** This requirement normally hides deep; you can't see the problem looking at the bash file alone, and you can't see it looking at the parallel function alone. Only by putting "cwd lives in a global" and "these tools get called concurrently" side by side does the hazard surface. This is exactly the kind of multi-file-spanning invariant from piece four's orphaned tool message; the most insidious bugs almost all look like this. If you intend to fork CoreCoder and add tools that write state, this is the lesson you must think through first: can your tool withstand being called by two threads at once?

## Sub-agents: spawning a clone of yourself

The `agent` tool (`corecoder/tools/agent.py`, 72 lines) solves a different problem. Some subtasks are heavy, say "go over this unfamiliar codebase and tell me how authentication is implemented." If the main agent does this itself, it has to read a pile of files and run a bunch of searches, and all that intermediate process piles into the main conversation's window; by the time it's figured things out, the window is nearly stuffed with exploration garbage and the real task has no room left.

The sub-agent's idea is: dispatch a clone with its own independent context to do this heavy work, let it churn in its own window, and hand back only a distilled conclusion when done. The main agent's window stays clean throughout, with just one extra line, "authentication is implemented this way."

The code is straightforward:

```python
def execute(self, task: str) -> str:
    if self._parent_agent is None:
        return "Error: agent tool not initialized (no parent agent)"

    from ..agent import Agent

    parent = self._parent_agent
    sub = Agent(
        llm=parent.llm,
        tools=[t for t in parent.tools if t.name != "agent"],  # no recursive agents
        max_context_tokens=parent.context.max_tokens,
        max_rounds=20,
    )

    try:
        result = sub.chat(task)
        if len(result) > 5000:
            result = result[:4500] + "\n... (sub-agent output truncated)"
        return f"[Sub-agent completed]\n{result}"
    except Exception as e:
        return f"Sub-agent error: {e}"
```

The main agent builds a new `Agent`, sharing the same LLM but with its own fresh `messages` list, which is the source of the context isolation. The sub-agent runs a complete loop of its own (its round cap dropped to 20), and if the returned result is too long it gets truncated to under 5000 characters, lest the space it painstakingly saved get canceled out by spitting back an overlong conclusion.

Notice the line `_parent_agent`. It's wired up in the main `Agent`'s constructor, the loop mentioned back in piece one:

```python
for t in self.tools:
    if isinstance(t, AgentTool):
        t._parent_agent = self
```

When constructing the agent, point each `AgentTool`'s "parent pointer" back at itself, so that when the tool is called it knows who to ask for the LLM and the tool set.

## Why a sub-agent isn't allowed to spawn a grand-sub-agent

That comment `# no recursive agents` above is the single most important line in this code.

When spawning a sub-agent, its tool set is deliberately filtered to remove the `agent` tool itself: `[t for t in parent.tools if t.name != "agent"]`. So the sub-agent doesn't have the "spawn a clone" ability; whatever it can't do it has to tough out itself, and it can't dispatch further down.

Why forbid it? Because a recursive agent is a bomb that can go out of control at any time. Imagine not forbidding it: the main agent dispatches a sub-agent, the sub-agent thinks the task is still too big and dispatches a grand-sub-agent, the grand-sub-agent dispatches again... each layer burns tokens, occupies threads, adds latency, and the model's judgment of "should this subtask be split further" isn't reliable; it can entirely fall into a bottomless pit of ever-finer splitting that never converges. Cutting recursion off cleanly is the most worry-free safety policy: a clone can be only one layer deep, and either this sub-agent handles it itself or it fails and returns, with no third outcome.

Remember piece one stressing that `_tool_by_name` is instance-level? Precisely because which tools each agent knows is its own business, "drop the agent tool from the sub-agent's set" here actually takes effect. Even if the sub-agent saw the name `agent` somewhere in its history, calling it would only get `unknown tool 'agent'`. Two designs that look unrelated mesh together right here. The test `test_agent_tool_scope_is_per_instance` guards exactly this mesh.

## Compared with Claude Code

Claude Code's sub-agent system (its AgentTool is over a thousand lines in public teardowns) is far richer: sub-agents have several run modes, including running in an independent git worktree and running asynchronously in the background, plus several built-in preset agent types, each with its own system prompt and tool set. CoreCoder converges all this into the most plain one: synchronous spawn, run and return, no recursion.

But the core motivation, "use a sub-agent with independent context to isolate heavy work and protect the main window," is the same in both. Read CoreCoder's 58 lines and you've grasped the most essential point of multi-agent collaboration: it's a context-management means first, and a task-decomposition means second. Many people think a sub-agent is for "doing more work in parallel," but its greatest value is actually "keeping the noise of other work out of the main conversation."

## What this piece leaves you with

- When the model returns multiple independent tool calls at once, running them concurrently on a thread pool is a worthwhile trade; in IO-bound scenarios a thread pool is the most effortless concurrency primitive.
- Parallelism isn't free: it imposes a concurrency-correctness requirement on every tool that holds mutable state. storing bash's cwd in a module global would race under parallelism, so CoreCoder isolates the state per thread with `threading.local`.
- The most insidious bugs often span multiple files: each spot alone looks fine, and only putting two together exposes it. Before adding concurrency, ask whether each tool can withstand being called at the same time.
- A sub-agent's primary value is context isolation, letting heavy work churn in an independent window and handing back only a distilled conclusion to the main conversation; task decomposition is secondary.
- Forbidding sub-agent recursion trades "cutting it off cleanly" for "never out of control." It lands via the instance-level tool-set mechanism.

Next piece, we fit these parts into a genuinely usable command-line tool: how sessions get saved, how to resume from a breakpoint, how slash commands hook in, and a security detail hidden inside session saving.
