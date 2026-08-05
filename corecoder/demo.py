"""Offline scripted demo: watch the full agent loop with no API key.

Runs one scripted task through the real Agent loop with a ScriptedLLM, so the
thought -> tool call -> observation cycle renders exactly as it would against
a live model. Useful for demos, screenshots, and as a smoke test of the loop.
"""

import tempfile
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .agent import Agent
from .llm import LLMResponse, ScriptedLLM, ToolCall

console = Console()

_TASK = (
    "Write a Python function `fib(n)` in fib.py returning the nth Fibonacci "
    "number, add a pytest file, then run the tests."
)


def _script(workdir: Path) -> list[LLMResponse]:
    fib_py = (
        "def fib(n):\n"
        "    if n < 2:\n"
        "        return n\n"
        "    a, b = 0, 1\n"
        "    for _ in range(2, n + 1):\n"
        "        a, b = b, a + b\n"
        "    return b\n"
    )
    test_py = (
        "from fib import fib\n"
        "\n"
        "def test_fib():\n"
        "    assert fib(0) == 0\n"
        "    assert fib(1) == 1\n"
        "    assert fib(10) == 55\n"
    )
    return [
        LLMResponse(
            content="I'll write fib.py with an iterative implementation.",
            tool_calls=[ToolCall(id="c1", name="write_file", arguments={"file_path": str(workdir / "fib.py"), "content": fib_py})],
        ),
        LLMResponse(
            content="Now the test file covering the base cases and fib(10).",
            tool_calls=[ToolCall(id="c2", name="write_file", arguments={"file_path": str(workdir / "test_fib.py"), "content": test_py})],
        ),
        LLMResponse(
            content="Running the tests.",
            tool_calls=[ToolCall(id="c3", name="bash", arguments={"command": f"cd {workdir} && python -m pytest test_fib.py -q"})],
        ),
        LLMResponse(
            content="All three assertions pass. `fib` is iterative, and the test covers the base cases plus fib(10) == 55."
        ),
    ]


def _summarize(args: dict) -> str:
    parts = []
    for key, value in args.items():
        text = str(value)
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
    return " ".join(parts)


def run_demo() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="corecoder-demo-"))
    agent = Agent(llm=ScriptedLLM(_script(workdir)))

    console.print(Panel.fit(f"[bold]{_TASK}[/]", title="corecoder demo (offline)"))
    result = agent.chat(
        _TASK,
        on_tool=lambda name, args: console.print(f"[cyan]tool:[/] {name} {_summarize(args)}"),
    )
    console.print(Panel.fit(Markdown(result), title="final"))
    console.print(f"[dim]workspace kept at {workdir}[/]")
    return 0
