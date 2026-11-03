"""Multi-agent fan-out and the shared state it must keep safe."""

import concurrent.futures
import threading
import time
from types import SimpleNamespace

from corecoder.agent import Agent
from corecoder.llm import LLM
from corecoder.permissions import Permission
from corecoder.tools.agent import AgentTool
from corecoder.tools.todo import TodoWriteTool
from examples.multi_agent_demo import run_demo


def parent_agent():
    tool = AgentTool()
    todo = TodoWriteTool()
    todo.execute(tasks=[{"content": "parent task", "status": "pending"}])
    parent = Agent(llm=LLM.__new__(LLM), tools=[tool, todo])
    return parent, tool, todo


def test_multi_agent_schema_accepts_named_tasks():
    schema = AgentTool().schema()["function"]["parameters"]
    tasks = schema["properties"]["tasks"]
    assert tasks["type"] == "array"
    assert tasks["maxItems"] == 8
    assert tasks["items"]["required"] == ["task"]
    assert "name" in tasks["items"]["properties"]
    assert schema["anyOf"] == [{"required": ["task"]}, {"required": ["tasks"]}]


def test_offline_multi_agent_demo(capsys):
    assert run_demo() == 0
    output = capsys.readouterr().out
    assert "### researcher" in output
    assert "### implementer" in output
    assert "### reviewer" in output
    assert "unhashable records" in output


def test_single_sub_agent_keeps_its_existing_contract(monkeypatch):
    parent, tool, parent_todo = parent_agent()
    captured = {}

    def fake_chat(self, text, **kwargs):
        captured["text"] = text
        captured["todo"] = self._todo
        captured["has_agent_tool"] = any(isinstance(t, AgentTool) for t in self.tools)
        return "done"

    monkeypatch.setattr(Agent, "chat", fake_chat)
    assert tool.execute(task="inspect the parser") == "[Sub-agent completed]\ndone"
    assert captured["text"] == "inspect the parser"
    assert captured["todo"] is not parent_todo
    assert captured["todo"]._tasks == []
    assert captured["has_agent_tool"] is False
    assert parent.messages == []
    assert parent_todo.render() == "1. [pending] parent task"


def test_named_sub_agents_overlap_and_report_in_input_order(monkeypatch):
    parent, tool, parent_todo = parent_agent()
    barrier = threading.Barrier(3)
    completed = {role: threading.Event() for role in ("researcher", "implementer", "reviewer")}
    events = []
    children = {}

    def fake_chat(self, text, **kwargs):
        role = text.split("\n", 1)[0].removeprefix("You are ").removesuffix(".")
        children[role] = {
            "id": id(self),
            "has_agent_tool": any(isinstance(t, AgentTool) for t in self.tools),
            "todo_tasks": self._todo._tasks.copy(),
            "messages": self.messages + [{"role": "user", "content": text}],
        }
        barrier.wait(timeout=2)
        if role == "implementer":
            assert completed["reviewer"].wait(timeout=2)
        elif role == "researcher":
            assert completed["implementer"].wait(timeout=2)
        events.append(role)
        completed[role].set()
        return f"{role} report"

    monkeypatch.setattr(Agent, "chat", fake_chat)
    tasks = [
        {"name": "researcher", "task": "inspect"},
        {"name": "implementer", "task": "fix"},
        {"name": "reviewer", "task": "check"},
    ]
    result = tool.execute(tasks=tasks)

    assert events == ["reviewer", "implementer", "researcher"]
    assert result.index("### researcher") < result.index("### implementer") < result.index("### reviewer")
    assert "### researcher\nresearcher report" in result
    assert set(children) == {"researcher", "implementer", "reviewer"}
    assert len({child["id"] for child in children.values()}) == 3
    expected_tasks = {"researcher": "inspect", "implementer": "fix", "reviewer": "check"}
    for role, task in expected_tasks.items():
        child = children[role]
        assert child["has_agent_tool"] is False
        assert child["todo_tasks"] == []
        assert child["messages"] == [{"role": "user", "content": f"You are {role}.\n\n{task}"}]
    assert parent.messages == []
    assert parent_todo.render() == "1. [pending] parent task"


def test_multi_agent_failure_does_not_cancel_siblings(monkeypatch):
    _, tool, _ = parent_agent()

    def fake_chat(self, text, **kwargs):
        if "broken" in text:
            raise RuntimeError("synthetic failure")
        return "healthy report"

    monkeypatch.setattr(Agent, "chat", fake_chat)
    result = tool.execute(tasks=[{"task": "healthy"}, {"task": "broken"}])
    assert "healthy report" in result
    assert "Sub-agent error: synthetic failure" in result


def test_multi_agent_rejects_ambiguous_or_unbounded_work(monkeypatch):
    _, tool, _ = parent_agent()

    def unexpected_chat(self, text, **kwargs):
        raise AssertionError("invalid work must not spawn")

    monkeypatch.setattr(Agent, "chat", unexpected_chat)
    invalid_calls = [
        {},
        {"task": "one", "tasks": [{"task": "two"}]},
        {"tasks": []},
        {"tasks": [{"task": str(i)} for i in range(9)]},
        {"tasks": [{"name": "missing task"}]},
        {"tasks": [{"task": "   "}]},
    ]
    for kwargs in invalid_calls:
        assert tool.execute(**kwargs).startswith("Error:")


class InstrumentedInt(int):
    """Yield inside += so a missing usage lock cannot pass by luck."""

    active = 0
    maximum = 0
    guard = threading.Lock()

    def __add__(self, other):
        with self.guard:
            type(self).active += 1
            type(self).maximum = max(type(self).maximum, type(self).active)
        time.sleep(0.01)
        with self.guard:
            type(self).active -= 1
        return int(self) + other


def test_shared_llm_usage_accounting_is_serialized():
    llm = LLM(model="test", api_key="synthetic")
    llm.total_prompt_tokens = InstrumentedInt()
    llm.total_completion_tokens = InstrumentedInt()
    chunk = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        choices=[],
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: llm._drain(iter([chunk]), None, None), range(4)))
    assert llm.total_prompt_tokens == 4
    assert llm.total_completion_tokens == 4
    assert InstrumentedInt.maximum == 1


def test_permission_prompt_is_serialized_for_agent_workers():
    active = 0
    maximum = 0
    guard = threading.Lock()

    def ask(name, arguments):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return "once"

    permission = Permission(ask=ask)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: permission.check("write_file", {}), range(4)))
    assert maximum == 1
