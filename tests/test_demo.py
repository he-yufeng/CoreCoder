"""Tests for the offline scripted demo and ScriptedLLM."""

import pytest

from corecoder.llm import LLMResponse, ScriptedLLM, ToolCall
from corecoder.demo import run_demo, _script


def test_scripted_llm_plays_turns_in_order():
    llm = ScriptedLLM([
        LLMResponse(content="first"),
        LLMResponse(content="second", tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "true"})]),
    ])

    r1 = llm.chat(messages=[])
    r2 = llm.chat(messages=[])

    assert r1.content == "first"
    assert r2.content == "second"
    assert r2.tool_calls[0].name == "bash"

    with pytest.raises(RuntimeError):
        llm.chat(messages=[])


def test_demo_runs_the_full_loop(tmp_path, monkeypatch):
    monkeypatch.setattr("corecoder.demo.tempfile.mkdtemp", lambda prefix: str(tmp_path))

    assert run_demo() == 0

    fib = (tmp_path / "fib.py").read_text()
    assert "def fib(n):" in fib
    assert (tmp_path / "test_fib.py").exists()


def test_script_points_at_the_demo_workdir(tmp_path):
    turns = _script(tmp_path)
    paths = [tc.arguments.get("file_path", "") for turn in turns for tc in turn.tool_calls]
    assert all(path.startswith(str(tmp_path)) for path in paths if path)
