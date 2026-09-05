"""Pre/PostToolUse shell hooks: loading, matching, blocking, failing open."""

import logging

from corecoder import Agent
from corecoder.demo import ScriptedLLM
from corecoder.hooks import Hooks, load_hooks
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.agent import AgentTool
from corecoder.tools.write import WriteFileTool


def _write_call(call_id, path):
    return ToolCall(id=call_id, name="write_file",
                    arguments={"file_path": str(path), "content": "x\n"})


def _agent(tmp_path, hooks, permission=None):
    return Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[_write_call("c1", tmp_path / "a.txt")]),
            LLMResponse(content="done"),
        ]),
        tools=[WriteFileTool()],
        permission=permission,
        hooks=hooks,
    )


def test_pre_hook_blocks_with_reason_and_the_tool_never_runs(tmp_path):
    blocker = tmp_path / "blocker.sh"
    blocker.write_text("#!/bin/sh\necho 'writes are frozen today' >&2\nexit 2\n")
    asked = []
    agent = _agent(
        tmp_path,
        Hooks(pre=[{"matcher": "*", "command": f"sh {blocker}"}], post=[]),
        permission=Permission(ask=lambda n, a: asked.append(n) or "once"),
    )

    assert agent.chat("go") == "done"          # the loop survived the veto
    assert not (tmp_path / "a.txt").exists()   # the tool never executed
    result = agent.messages[2]
    assert result["role"] == "tool" and result["tool_call_id"] == "c1"
    assert "writes are frozen today" in result["content"]  # the model gets the reason
    assert asked == []                         # hooks gate before consent is asked


def test_pre_hook_passing_lets_the_call_through(tmp_path):
    agent = _agent(tmp_path, Hooks(pre=[{"matcher": "", "command": "true"}], post=[]))

    assert agent.chat("go") == "done"
    assert (tmp_path / "a.txt").exists()


def test_post_hook_observes_the_finished_call(tmp_path):
    marker = tmp_path / "seen.jsonl"
    agent = _agent(tmp_path, Hooks(pre=[], post=[{"matcher": "*", "command": f"cat >> {marker}"}]))

    assert agent.chat("go") == "done"
    seen = marker.read_text()
    assert '"tool_name": "write_file"' in seen  # the call JSON arrived on stdin
    assert '"tool_response"' in seen            # post hooks see the result too


def test_matcher_scopes_a_hook_to_one_tool(tmp_path):
    # a bash-only veto must not touch a write_file call
    cmd = "echo blocked >&2; exit 2"
    agent = _agent(tmp_path, Hooks(pre=[{"matcher": "bash", "command": cmd}], post=[]))

    assert agent.chat("go") == "done"
    assert (tmp_path / "a.txt").exists()


def test_missing_hooks_file_means_no_hooks(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        hooks = load_hooks(tmp_path / "nope.json")
    assert not hooks
    assert caplog.records == []


def test_broken_hooks_file_is_ignored_with_one_warning(tmp_path, caplog):
    bad = tmp_path / "hooks.json"
    bad.write_text("{not json")
    with caplog.at_level(logging.WARNING):
        hooks = load_hooks(bad)
    assert not hooks
    assert len(caplog.records) == 1
    assert "ignoring" in caplog.records[0].getMessage()


def test_failing_hook_is_skipped_with_a_warning(tmp_path, caplog):
    agent = _agent(tmp_path, Hooks(pre=[{"matcher": "*", "command": "exit 1"}], post=[]))
    with caplog.at_level(logging.WARNING):
        assert agent.chat("go") == "done"
    assert (tmp_path / "a.txt").exists()  # fail open
    assert any("exited 1" in r.getMessage() for r in caplog.records)


def test_slow_hook_times_out_and_is_skipped(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr("corecoder.hooks.TIMEOUT", 0.3)
    agent = _agent(tmp_path, Hooks(pre=[{"matcher": "*", "command": "sleep 5"}], post=[]))
    with caplog.at_level(logging.WARNING):
        assert agent.chat("go") == "done"
    assert (tmp_path / "a.txt").exists()
    assert any("TimeoutExpired" in r.getMessage() for r in caplog.records)


def test_hooks_gate_each_call_of_a_parallel_batch(tmp_path):
    calls = [
        _write_call("c1", tmp_path / "a.txt"),
        _write_call("c2", tmp_path / "b.txt"),
    ]
    pre_log = tmp_path / "pre.jsonl"
    post_log = tmp_path / "post.jsonl"
    agent = Agent(
        llm=ScriptedLLM([LLMResponse(tool_calls=calls), LLMResponse(content="done")]),
        tools=[WriteFileTool()],
        hooks=Hooks(
            pre=[{"matcher": "*", "command": f"cat >> {pre_log}"}],
            post=[{"matcher": "*", "command": f"cat >> {post_log}"}],
        ),
    )

    assert agent.chat("go") == "done"
    assert (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()
    assert pre_log.read_text().count('"tool_name"') == 2
    assert post_log.read_text().count('"tool_response"') == 2


def test_sub_agent_inherits_the_hooks(tmp_path):
    target = tmp_path / "sub.txt"
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="c1", name="agent", arguments={"task": "write the file"})]),
            LLMResponse(tool_calls=[_write_call("c2", target)]),  # the sub-agent's move
            LLMResponse(content="could not write"),               # the sub-agent's reply
            LLMResponse(content="parent done"),
        ]),
        tools=[AgentTool(), WriteFileTool()],
        hooks=Hooks(pre=[{"matcher": "write_file", "command": "sh -c 'echo no >&2; exit 2'"}], post=[]),
    )

    assert agent.chat("go") == "parent done"
    assert not target.exists()  # the veto followed the work into the sub-agent
