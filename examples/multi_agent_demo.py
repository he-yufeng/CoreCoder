"""Offline multi-agent demo: three specialists inspect one implementation.

No API key or model service is used. RoleLLM supplies deterministic findings so
the example demonstrates real AgentTool fan-out, not a model-quality claim.
Run from a source checkout with `python examples/multi_agent_demo.py`.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corecoder.agent import Agent
from corecoder.llm import LLMResponse
from corecoder.tools.agent import AgentTool


class RoleLLM:
    """Small deterministic stand-in for three independent model calls."""

    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        prompt = messages[-1]["content"]
        role = prompt.split("\n", 1)[0].removeprefix("You are ").removesuffix(".")
        findings = {
            "researcher": "`sorted(set(records))` drops duplicates, but also destroys input order.",
            "implementer": "Use `list(dict.fromkeys(records))` to deduplicate while preserving order.",
            "reviewer": "The proposed dict-key approach fails for unhashable records; use an equality-based loop.",
        }
        time.sleep({"researcher": 0.03, "implementer": 0.02, "reviewer": 0.01}[role])
        return LLMResponse(content=findings[role])


def run_demo() -> int:
    llm = RoleLLM()
    tool = AgentTool()
    Agent(llm=llm, tools=[tool])  # wires the tool to its parent
    started = time.monotonic()
    result = tool.execute(
        tasks=[
            {"name": "researcher", "task": "Find semantic risks in sorted(set(records))."},
            {"name": "implementer", "task": "Propose an order-preserving deduplication."},
            {"name": "reviewer", "task": "Find an input that breaks the proposed fix."},
        ]
    )
    print(result)
    print(f"\nThree workers reported in {time.monotonic() - started:.2f}s (offline scripted findings).")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_demo())
