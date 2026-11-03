"""Sub-agent spawning (inspired by Claude Code's AgentTool, 1397 lines).

The idea: for complex sub-tasks, spawn an independent agent with its own
conversation history and tool access. This lets the main agent delegate
work like "go research this codebase and report back" without polluting
its own context window.

A named task list fans out a small team concurrently without sharing
message history or checklist state between its workers.
"""

import concurrent.futures
import copy
from typing import ClassVar

from .base import Tool

_MAX_TEAM_SIZE = 8


class AgentTool(Tool):
    name = "agent"
    description = (
        "Spawn a sub-agent to handle a complex sub-task independently. "
        "The sub-agent has its own context and tool access. Use this for: "
        "researching a codebase, implementing a multi-step change in isolation, "
        "or any task that would benefit from a fresh context window. "
        "For independent work, pass named tasks to run a small team concurrently."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What one sub-agent should accomplish",
            },
            "tasks": {
                "type": "array",
                "description": "Independent tasks to run concurrently and report in input order",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Worker role shown in the combined report",
                        },
                        "task": {
                            "type": "string",
                            "description": "What this worker should accomplish",
                        },
                    },
                    "required": ["task"],
                },
                "minItems": 1,
                "maxItems": _MAX_TEAM_SIZE,
            },
        },
        "required": [],
        "anyOf": [{"required": ["task"]}, {"required": ["tasks"]}],
    }

    # set by Agent.__init__ after construction
    _parent_agent = None

    def _new_subagent(self):
        # import here to avoid circular dep
        from ..agent import Agent
        from .todo import TodoWriteTool

        parent = self._parent_agent
        tools = []
        for original in parent.tools:
            if original.name == "agent":
                continue  # no recursive agents
            tool = copy.copy(original)
            if isinstance(tool, TodoWriteTool):
                tool._tasks = []
            tools.append(tool)
        return Agent(
            llm=parent.llm,
            tools=tools,
            max_context_tokens=parent.context.max_tokens,
            max_rounds=20,
            # the sub-agent answers to the same consent layer and hooks as the parent
            permission=parent.permission,
            hooks=parent.hooks,
        )

    def _run_one(self, item: dict, cwd: str | None) -> str:
        # Each worker gets the session cwd without leaking its own cd back.
        from .bash import get_tracked_cwd, set_tracked_cwd

        previous = get_tracked_cwd()
        try:
            set_tracked_cwd(cwd)
            name = item.get("name")
            prompt = item["task"] if name is None else f"You are {name}.\n\n{item['task']}"
            result = self._new_subagent().chat(prompt)
            if len(result) > 5000:
                result = result[:4500] + "\n... (sub-agent output truncated)"
            return result
        except Exception as e:  # noqa: BLE001
            return f"Sub-agent error: {e}"
        finally:
            set_tracked_cwd(previous)

    def execute(self, task: str | None = None, tasks: list[dict] | None = None) -> str:
        if self._parent_agent is None:
            return "Error: agent tool not initialized (no parent agent)"
        if (task is None) == (tasks is None):
            return "Error: provide exactly one of 'task' or 'tasks'"

        if task is not None:
            if not isinstance(task, str) or not task.strip():
                return "Error: task must be a non-empty string"
            work = [{"task": task}]
        else:
            if not isinstance(tasks, list) or not 1 <= len(tasks) <= _MAX_TEAM_SIZE:
                return f"Error: tasks must contain 1 to {_MAX_TEAM_SIZE} workers"
            work = []
            for i, item in enumerate(tasks, 1):
                prompt = item.get("task") if isinstance(item, dict) else None
                name = item.get("name") if isinstance(item, dict) else None
                if not isinstance(prompt, str) or not prompt.strip():
                    return f"Error: task {i} needs a non-empty 'task' string"
                if name is not None and (not isinstance(name, str) or not name.strip()):
                    return f"Error: task {i} needs a non-empty 'name' when provided"
                work.append({"task": prompt, "name": name.strip() if name else f"agent-{i}"})

        from .bash import get_tracked_cwd

        cwd = get_tracked_cwd()
        if task is not None:
            result = self._run_one(work[0], cwd)
            if result.startswith("Sub-agent error:"):
                return result
            return f"[Sub-agent completed]\n{result}"

        # pool.map returns in task order even when workers finish out of order.
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(work))) as pool:
            results = list(pool.map(lambda item: self._run_one(item, cwd), work))
        reports = [f"### {item['name']}\n{result}" for item, result in zip(work, results)]
        return f"[Sub-agent team: {len(work)} reports]\n\n" + "\n\n".join(reports)
