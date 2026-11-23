"""Slash-command dispatch in the REPL: each command must act and stay silent-proof."""

import corecoder.session as session_module
from corecoder import Agent, checkpoints
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse
from tests.conftest import repl_with


def _agent(*responses):
    return Agent(llm=ScriptedLLM(list(responses)))


def test_reset_clears_the_conversation(monkeypatch, capsys):
    agent = _agent(LLMResponse(content="answer"))
    agent.chat("hi")
    assert agent.messages

    repl_with(monkeypatch, agent, ["/reset", "quit"])

    assert agent.messages == []
    assert "Conversation reset." in capsys.readouterr().out


def test_tokens_prints_totals(monkeypatch, capsys):
    agent = _agent()

    repl_with(monkeypatch, agent, ["/tokens", "quit"])

    assert "Tokens: 0 prompt + 0 completion = 0 total" in capsys.readouterr().out


def test_model_without_argument_prints_current(monkeypatch, capsys):
    agent = _agent()

    repl_with(monkeypatch, agent, ["/model", "quit"])

    assert "Current model:" in capsys.readouterr().out


def test_model_with_argument_switches_llm_and_config(monkeypatch, capsys):
    agent = _agent()

    config = repl_with(monkeypatch, agent, ["/model kimi-k2.5", "quit"])

    assert agent.llm.model == "kimi-k2.5"
    assert config.model == "kimi-k2.5"
    assert "Switched to" in capsys.readouterr().out


def test_compact_on_a_short_conversation_reports_nothing(monkeypatch, capsys):
    agent = _agent()

    repl_with(monkeypatch, agent, ["/compact", "quit"])

    assert "Nothing to compress" in capsys.readouterr().out


def test_sessions_with_none_saved_reports_none(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    agent = _agent()

    repl_with(monkeypatch, agent, ["/sessions", "quit"])

    assert "No saved sessions." in capsys.readouterr().out


def test_diff_with_no_edits_reports_none(monkeypatch, capsys):
    monkeypatch.setattr("corecoder.tools.edit._changed_files", set())
    agent = _agent()

    repl_with(monkeypatch, agent, ["/diff", "quit"])

    assert "No files modified this session." in capsys.readouterr().out


def test_diff_lists_modified_files(monkeypatch, capsys):
    monkeypatch.setattr("corecoder.tools.edit._changed_files", {"src/a.py"})
    agent = _agent()

    repl_with(monkeypatch, agent, ["/diff", "quit"])

    out = capsys.readouterr().out
    assert "Files modified this session (1):" in out
    assert "src/a.py" in out


def test_undo_with_empty_stack_reports_nothing(monkeypatch, capsys):
    agent = _agent()

    repl_with(monkeypatch, agent, ["/undo", "quit"])

    assert "Nothing to undo." in capsys.readouterr().out


def test_undo_restores_the_recorded_file(monkeypatch, capsys, tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before", encoding="utf-8")
    checkpoints.record(target)
    target.write_text("after", encoding="utf-8")
    agent = _agent()

    repl_with(monkeypatch, agent, ["/undo", "quit"])

    assert target.read_text(encoding="utf-8") == "before"
    assert "Restored" in capsys.readouterr().out


def test_unknown_slash_command_never_reaches_the_model(monkeypatch, capsys):
    agent = _agent()

    repl_with(monkeypatch, agent, ["/frobnicate", "quit"])

    assert "Unknown command: /frobnicate" in capsys.readouterr().out
    assert agent.messages == []
