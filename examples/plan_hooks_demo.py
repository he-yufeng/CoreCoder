#!/usr/bin/env python3
"""Offline plan mode + hooks demo (no API key): the v0.6.0 showcase.

Run:  python examples/plan_hooks_demo.py

Seeds a fib.py in a temp dir, then drives the real Agent loop through:
plan mode ON -> read allowed, edit refused -> plan presented -> approve ->
edit/write/pytest flowing through with Pre/PostToolUse hooks firing.
"""

import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corecoder.agent import Agent
from corecoder.cli import _brief
from corecoder.demo import ScriptedLLM
from corecoder.hooks import Hooks
from corecoder.llm import LLMResponse, ToolCall

console = Console()

TASK = "Review fib.py, add validation for negative n, and re-run the tests."

FIB_PY = (
    "def fib(n):\n"
    "    a, b = 0, 1\n"
    "    for _ in range(n):\n"
    "        a, b = b, a + b\n"
    "    return a\n"
)

FIB_GUARDED = (
    "def fib(n):\n"
    "    if n < 0:\n"
    "        raise ValueError(\"n must be non-negative\")\n" + FIB_PY[11:]
)

FIB_TEST = (
    "import pytest\n"
    "from fib import fib\n"
    "\n"
    "def test_fib():\n"
    "    assert fib(3) == 2\n"
    "\n"
    "def test_negative_raises():\n"
    "    with pytest.raises(ValueError):\n"
    "        fib(-3)\n"
)


class DemoHooks:
    """Print each hook firing so the hook seam is visible with no config."""

    def __init__(self):
        self._inner = Hooks(pre=[], post=[])

    def run_pre(self, tool_name: str, tool_input: dict) -> str | None:
        console.print(f"[magenta]hook PreToolUse[/]  {tool_name} -> allow")
        return self._inner.run_pre(tool_name, tool_input)

    def run_post(self, tool_name: str, tool_input: dict, result: str):
        console.print(f"[magenta]hook PostToolUse[/] {tool_name} done")
        return self._inner.run_post(tool_name, tool_input, result)


def _script(workdir: Path) -> list[LLMResponse]:
    edit_args = {
        "file_path": str(workdir / "fib.py"),
        "old_string": FIB_PY,
        "new_string": FIB_GUARDED,
    }
    return [
        LLMResponse(
            content="Reading the current implementation before planning anything.",
            tool_calls=[ToolCall(id="p1", name="read_file", arguments={"file_path": str(workdir / "fib.py")})],
        ),
        LLMResponse(
            content="Straight to the edit then.",
            tool_calls=[ToolCall(id="p2", name="edit_file", arguments=edit_args)],
        ),
        LLMResponse(
            content="Right, plan mode. Plan: guard negative n with ValueError, extend the tests with a raises-case, then run pytest."
        ),
        LLMResponse(
            content="Adding the guard and the negative test.",
            tool_calls=[ToolCall(id="p3", name="edit_file", arguments=edit_args)],
        ),
        LLMResponse(
            content="Extending the tests with the negative case.",
            tool_calls=[ToolCall(id="p4", name="write_file", arguments={"file_path": str(workdir / "test_fib.py"), "content": FIB_TEST})],
        ),
        LLMResponse(
            content="Running the suite.",
            tool_calls=[ToolCall(id="p5", name="bash", arguments={"command": f"cd {workdir} && python -m pytest -q"})],
        ),
        LLMResponse(
            content="Guard is in and both tests pass, including the new negative-n case."
        ),
    ]


def run() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="corecoder-demo-"))
    (workdir / "fib.py").write_text(FIB_PY, encoding="utf-8")
    agent = Agent(llm=ScriptedLLM(_script(workdir)), hooks=DemoHooks())
    agent.plan_mode = True

    console.print(Panel.fit(f"[bold]{TASK}[/]", title="corecoder demo · plan mode + hooks (offline)"))
    console.print("[dim]plan mode ON — mutating tools are refused until the plan is approved[/]")

    plan = agent.chat(
        TASK,
        on_tool=lambda name, args: console.print(f"[cyan]tool:[/] {name} {_brief(args)}"),
    )
    for msg in reversed(agent.messages):
        if msg.get("role") == "tool":
            console.print(f"[red]refused:[/] {str(msg.get('content', '')).splitlines()[0]}")
            break
    console.print(Panel.fit(Markdown(plan), title="plan presented"))
    console.print("[green]You > approve[/]")
    agent.plan_mode = False

    result = agent.chat(
        "approved, proceed",
        on_tool=lambda name, args: console.print(f"[cyan]tool:[/] {name} {_brief(args)}"),
    )
    console.print(Panel.fit(Markdown(result), title="final"))
    console.print(f"[dim]workspace kept at {workdir}[/]")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
