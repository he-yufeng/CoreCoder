# Part Six: When One Claude Isn't Enough

Claude Code's AgentTool is the largest tool besides BashTool. The main file `AgentTool.tsx` has 1397 lines, and the entire `src/tools/AgentTool/` directory exceeds 6700 lines.

It took me almost a whole day to read through this directory. Honestly, I was a little confused at first—what's so special about "starting another agent" that it takes 6700 lines? After reading it, it was clear that it's not just about "starting another agent."

---

## Why Multiple Agents Are Needed

Let's consider a real-world scenario.

A user says, "Help me refactor the error handling in the auth module, add tests to each change, and finally update the API documentation."

In single-agent mode, the three subtasks share a single 128K context window. The auth module's file content, test code, and documentation are all crammed into one window. The output of each tool call continues to expand. When modifying the documentation, the previously read auth module code might have been compressed by the context—the LLM would have to reread the file, wasting time and tokens.

A multi-agent solution: The main agent breaks the task into three subtasks, each assigned to an independent sub-agent. **Each sub-agent has its own 128K context.** The three sub-agents can work in parallel without interfering with each other, and report the results to the main agent for integration upon completion.

This gives you a total context capacity of 128K × 3 = 384K, and each subtask's context is "clean"—containing only information relevant to itself.

---

## Three Modes of AgentTool

The AgentTool source code has three execution modes.

### Normal Mode (Default)

Creates a new agent, executing in the same working directory as the main process. Sub-agents share the file system but have independent contexts and message history. This is the most commonly used mode.

```typescript
// Normal mode call
agent({
  description: "Research the auth module codebase",
  task: "Read all files in src/auth/ and report the error handling patterns used",
})
```

### Worktree Mode

Creates a Git worktree where child agents work in **completely isolated** directory copies.

```typescript
// Worktree Mode
agent({
  description: "Refactor auth module",
  task: "...",
  isolation: "worktree", // Feature flag gating
})
```

This solves a tricky problem: what if two child agents modify `auth.py` simultaneously? In normal mode, this would cause a conflict. In Worktree mode, each child agent makes changes in its own Git worktree, and the main agent decides how to merge them.

Git worktree is a native Git feature that allows a repository to have multiple working directories, each on a different branch. Claude Code uses this feature to create isolated environments for child agents, eliminating the need to copy the entire repository.

### Background Mode

The child agent runs in the background, while the main agent **does not wait** and continues processing other tasks. The background agent notifies the main agent when it finishes. This mode is used under COORDINATOR_MODE. It's suitable for scenarios like "I need you to run a test in the background while I continue writing code."

---

## Child Agents Cannot Create Sub-Agents

I laughed when I came across this code:

```typescript
// src/tools/AgentTool/runAgent.ts
// Child Agent's tool list: filters out agent tools from the parent agent's tool list
const subAgentTools = parentTools.filter(t => t.name !== 'agent')
```

The tool list received by the child agent **does not include the agent tool itself**. In other words, a child agent cannot create sub-sub-agents.

This isn't technically impossible (recursive agents are theoretically entirely feasible), but rather a rational engineering choice:

1. **Recursion Risk:** If an agent creates 3 child agents, and each child agent creates 3 more, that's 27 parallel agents across three layers. API call costs and concurrency explode instantly.
2. **Debugging Hell:** Debugging two layers (master + child) is already complex enough. Passing context between three nested agents means you have no idea which layer caused the problem.
3. **Diminishing Returns:** In practice, two layers cover almost all scenarios. Tasks requiring three nested layers should typically be broken down into multiple independent tasks, rather than using deeper agent trees.

NanoCoder also uses the same design: `AgentTool.execute()` filters out `agent` from the tool list when creating a child agent.

---

## Child Agent Context Construction

Child agents are not starting from scratch. Their context includes:

1. **A concise system prompt.** This includes working directory information, a list of available tools, and environment information. However, it **does not include the main agent's dialogue history**—that is the main agent's private context.
2. **A task description given to it by the main agent** (the `task` parameter). This is the only information passed from the main agent to the child agent.
3. **A special instruction: After completing the task, write a text summary of the result and return it.**

This last point is crucial. The entire execution process of the child agent—potentially involving dozens of tool calls—is ultimately compressed into a single text string, returned to the main agent as `tool_result`.

```
Main Agent: agent(task="Analyze the error handling pattern of the auth module")

Sub-Agent:
  → read_file("src/auth.py")
  → read_file("src/auth/token.py")
  → read_file("src/auth/session.py")
  → grep("except|raise|try", path="src/auth/")
  → ...internal reasoning and analysis...
  → return "
            [Sub-agent completed] 
            The auth module uses two error handling patterns:
              1. verify_token() and decode_payload() use try/except to catch exceptions from the JWT library.
              2. check_permission() uses if/else to check the return value.
            It is recommended to use the try/except pattern consistently because...
          "

```
The main agent only sees this summary and doesn't know what files the sub-agent read or what thought process it undertook. This is good—the main agent doesn't need to know these details; it only needs the conclusions of the subtasks to make its next decision.

NanoCoder also limits the length of the sub-agent's results to a maximum of 5000 characters to prevent overloading the main agent's context.

---

## What are 6700 lines of AgentTool doing?

Still, "starting another agent doesn't require 6700 lines ," so what is the rest of the code doing? Let's break it down:

```
AgentTool.tsx (228KB)          → Routing logic, schema definition, three modes of distribution
runAgent.ts (35KB)             → The actual execution engine: building context, running loops, collecting results
UI.tsx (122KB)                 → Terminal rendering: progress bar, result display, grouped display
agentToolUtils.ts (22KB)       → Utility functions: progress tracking, result processing
forkSubagent.ts (8.5KB)        → Fork mode: cache sharing fork
agentMemory.ts (5.7KB)         → Memory persistence across iterations
agentMemorySnapshot.ts (5.5KB) → Memory state serialization
loadAgentsDir.ts (26KB)        → Agent type discovery and loading
resumeAgent.ts (9.1KB)         → Resuming a paused background agent
prompt.ts                      → Prompt template for child agents
agentColorManager.ts           → Each sub-Agent is assigned a different color
built-in/                      → 6 built-in Agent type definitions
```

The bulk of the work is UI rendering (122KB) and the execution engine (35KB). UI rendering is complex because multiple sub-Agents run in parallel, and the status (running/completed/errored) and output of each agent need to be displayed neatly in the terminal. This is much more difficult to do in the terminal than with a web UI.

---

## Six Built-in Agent Types

The `built-in/` directory contains six predefined Agent types:

| Type | File | Purpose |
|------|------|------|
| `generalPurposeAgent` | General | Default type, all-rounder |
| `planAgent` | Planning | Only plans, does not execute, outputs implementation solutions |
| `exploreAgent` | Exploration | Read-only tool, used for codebase research |
| `verificationAgent` | Verification | Verifies the correctness of modifications and runs tests |
| `claudeCodeGuideAgent` | Help | Answers questions about Claude Code itself |
| `statuslineSetup` | Configuration | Status bar setup wizard |

Interestingly, `exploreAgent`'s tool-list **does not contain any writing tools** (no edit_file, write_file, bash). It only contains read_file, grep, and glob. This ensures that exploratory agents won't accidentally modify the code. You tell it to "explore the architecture of this codebase," and it can only view, not modify.

NanoCoder currently only has one Agent type (general-purpose), but you can simulate this by passing different tool lists in `Agent.__init__`:

```python
# Read-only Agent
from nanocoder.tools.read import ReadFileTool
from nanocoder.tools.grep import GrepTool
from nanocoder.tools.glob_tool import GlobTool

explore_agent = Agent(llm=llm, tools=[ReadFileTool(), GrepTool(), GlobTool()])
```


---

## Team System (Preview)

Beside AgentTool, Claude Code has a higher-level "team" concept. `TeamCreateTool` can create a group of Agents, assign roles, and they communicate with each other through a messaging system.

```typescript
// Team creation (Feature Flag: COORDINATOR_MODE)
teamCreate({ 
  agents: [ 
    { name: "frontend", focus: "React components in src/components/" }, 
    { name: "backend", focus: "API handlers in src/api/" }, 
    { name: "tester", focus: "Write tests for changed files"},
  ]
})

// Inter-Agent Communication
sendMessage({ to: "tester", content: "I just refactored auth.py, please write tests" })
```

The backend supports three methods: tmux pane, in-process, and remote. However, the entire team's system is still behind Feature Flag and has not been released to the public. Judging from the source code completion, it is very close to being usable.

---

> This article is the 6th in the [Claude Code Source Code Guide](00-index_EN.md) series. Accompanying implementation: [NanoCoder](https://github.com/he-yufeng/NanoCoder)