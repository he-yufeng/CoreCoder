import sys

import pytest

from corecoder import cli, session


@pytest.fixture
def isolated_sessions(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSIONS_DIR", root)

    def unexpected_config():
        pytest.fail("listing sessions must not load provider configuration")

    monkeypatch.setattr(cli.Config, "from_env", unexpected_config)
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
