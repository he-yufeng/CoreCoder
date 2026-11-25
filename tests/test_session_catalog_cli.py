import json
import sys
from types import SimpleNamespace

import pytest

from corecoder import cli, session


@pytest.fixture
def isolated_sessions(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSIONS_DIR", root)

    def unexpected_init(*args, **kwargs):
        pytest.fail("offline session commands must not initialize a provider or MCP")

    monkeypatch.setattr(cli.Config, "from_env", unexpected_init)
    monkeypatch.setattr(cli, "LLM", unexpected_init)
    monkeypatch.setattr(cli, "load_mcp_tools", unexpected_init)
    return root


def test_list_sessions_offline(isolated_sessions, monkeypatch, capsys):
    sid = session.save_session([
        {"role": "user", "content": "[note] 看上次的结果"},
        {"role": "assistant", "content": "not shown in a preview"},
    ], "research-model")
    path = isolated_sessions / f"{sid}.json"
    before = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions"])

    cli.main()

    output = capsys.readouterr().out
    assert sid in output
    assert "research-model" in output
    assert "[note] 看上次的结果" in " ".join(output.split())
    assert "not shown" not in output
    assert path.read_bytes() == before


def test_list_sessions_skips_non_object_record(isolated_sessions, monkeypatch, capsys):
    sid = session.save_session([{"role": "user", "content": "keep this"}], "m")
    unreadable = isolated_sessions / "zz_not_a_session.json"
    unreadable.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions"])

    cli.main()

    output = capsys.readouterr().out
    assert sid in output
    assert "zz_not_a_session" not in output
    assert unreadable.read_text(encoding="utf-8") == "[]"


def test_list_sessions_empty_is_read_only(isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions"])

    cli.main()

    assert "No saved sessions." in capsys.readouterr().out
    assert not isolated_sessions.exists()


@pytest.mark.parametrize("other", [["--resume", "old-id"], ["--prompt", "hello"], ["--demo"]])
def test_list_sessions_cannot_start_another_mode(other, isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions", *other])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "--list-sessions" in stderr
    assert "not allowed with" in stderr or "cannot be combined" in stderr
    assert not isolated_sessions.exists()


def test_inspect_session_only_prints_metadata(isolated_sessions, monkeypatch, capsys):
    isolated_sessions.mkdir()
    path = isolated_sessions / "restored.json"
    history = [
        {"role": "user", "content": "private question from yesterday"},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_fixture", "type": "function", "function": {"name": "read_file", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call_fixture", "content": "private tool response"},
    ]
    path.write_text(json.dumps({
        "model": "saved-model", "saved_at": "2026-11-25 09:00:00", "messages": history,
    }), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(sys, "argv", ["corecoder", "--inspect-session", "restored"])

    cli.main()

    result = capsys.readouterr()
    assert result.err == ""
    assert "Session: restored" in result.out
    assert "Model: saved-model" in result.out
    assert "Saved: 2026-11-25 09:00:00" in result.out
    assert "Messages: 3" in result.out
    assert "private" not in result.out and "call_fixture" not in result.out
    assert session.load_session("restored") == (history, "saved-model")
    assert path.read_bytes() == before


@pytest.mark.parametrize("mode", ["--inspect-session", "--resume"])
@pytest.mark.parametrize("case,diagnostic", [
    ("missing", "not found"),
    ("unreadable", "could not read"),
    ("bad_json", "invalid session file"),
    ("bad_structure", "invalid session file"),
])
def test_offline_session_failure_precedes_configuration(mode, case, diagnostic, isolated_sessions, monkeypatch, capsys):
    isolated_sessions.mkdir()
    target = isolated_sessions / "fixture.json"
    if case == "bad_json":
        target.write_text("{", encoding="utf-8")
    elif case == "bad_structure":
        target.write_text('{"model": "m", "messages": null}', encoding="utf-8")
    elif case == "unreadable":
        target.write_text('{"model": "m", "messages": []}', encoding="utf-8")
        read_text = type(target).read_text

        def refuse_one(path, *args, **kwargs):
            if path == target.resolve():
                raise PermissionError("fixture denied")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(type(target), "read_text", refuse_one)
    monkeypatch.setattr(sys, "argv", ["corecoder", mode, "fixture"])

    with pytest.raises(SystemExit) as error:
        cli.main()

    result = capsys.readouterr()
    assert error.value.code == 1
    assert diagnostic in result.err.lower()
    assert "fixture" in result.err
    assert not result.out


@pytest.mark.parametrize("other", [["--resume", "old"], ["-p", "hello"], ["--demo"], ["--list-sessions"]])
def test_inspect_session_cannot_start_another_mode(other, isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--inspect-session", "id", *other])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "--inspect-session" in capsys.readouterr().err
    assert not isolated_sessions.exists()


@pytest.mark.parametrize("prompt,override,model", [(None, [], "saved-model"), ("next step", ["--model", "chosen"], "chosen")])
def test_valid_resume_installs_history_and_selects_model_before_construction(
    prompt, override, model, isolated_sessions, monkeypatch,
):
    history = [{"role": "user", "content": "already here"}]
    sid = session.save_session(history, "saved-model")
    created = []
    handled = []
    args = ["corecoder", "--resume", sid, *override]
    if prompt:
        args.extend(["-p", prompt])
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(cli.Config, "from_env", lambda: cli.Config(model="configured", api_key="fixture"))
    monkeypatch.setattr(cli, "load_mcp_tools", list)
    monkeypatch.setattr(cli, "load_hooks", list)
    monkeypatch.setattr(cli, "DEFAULT_STORE", isolated_sessions / "permissions.json")

    def make_llm(**kwargs):
        created.append(kwargs["model"])
        return SimpleNamespace(model=kwargs["model"])

    monkeypatch.setattr(cli, "LLM", make_llm)
    monkeypatch.setattr(cli, "_repl", lambda agent, cfg: handled.append((agent.messages, cfg.model, None)))
    monkeypatch.setattr(cli, "_run_once", lambda agent, message: handled.append((agent.messages, agent.llm.model, message)))

    cli.main()

    assert created == [model]
    assert handled == [(history, model, prompt)]
