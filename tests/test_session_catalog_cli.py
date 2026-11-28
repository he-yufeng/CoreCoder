import json
import sys
from types import SimpleNamespace

import pytest

from corecoder import cli, session
from corecoder.context import ContextManager


@pytest.fixture
def isolated_sessions(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    monkeypatch.setattr(session, "SESSIONS_DIR", root)

    def unexpected_init(*args, **kwargs):
        pytest.fail("offline session commands must not boot a run")

    monkeypatch.setattr(cli.Config, "from_env", unexpected_init)
    for name in ("LLM", "LiteLLM", "load_mcp_tools", "load_hooks"):
        monkeypatch.setattr(cli, name, unexpected_init)
    return root


@pytest.mark.parametrize("left_tail,right_tail,shared,difference", [
    ([], [], 1, None),
    ([{"role": "assistant", "content": "private left"}], [], 1, None),
    ([], [{"role": "assistant", "content": "private right"}], 1, None),
    ([{"role": "assistant", "content": "private left"}],
     [{"role": "assistant", "content": "private right"}], 1, 2),
    ([{"role": "tool", "content": {"enabled": True}}],
     [{"role": "tool", "content": {"enabled": 1}}], 1, 2),
])
def test_compare_sessions_reports_only_the_stored_prefix(
    isolated_sessions, monkeypatch, capsys, left_tail, right_tail, shared, difference,
):
    left = [{"role": "user", "content": {"note": "private start", "step": 1}}, *left_tail]
    right = [{"content": {"step": 1, "note": "private start"}, "role": "user"}, *right_tail]
    session.save_session(left, "private-model-a", "left")
    session.save_session(right, "private-model-b", "right")
    before = {p.name: p.read_bytes() for p in isolated_sessions.iterdir()}
    monkeypatch.setattr(sys, "argv", ["corecoder", "--compare-sessions", "left", "right"])

    cli.main()

    result = capsys.readouterr()
    assert result.err == ""
    assert f"Shared stored messages: {shared}" in result.out
    assert f"Remaining messages: {len(left) - shared} left, {len(right) - shared} right" in result.out
    assert "Left: left" in result.out and "Right: right" in result.out
    assert "private" not in result.out
    if difference is None:
        assert "First different stored message:" not in result.out
    else:
        assert f"First different stored message: {difference}" in result.out
    assert {p.name: p.read_bytes() for p in isolated_sessions.iterdir()} == before


@pytest.mark.parametrize("args,reason", [
    (["missing", "valid"], "not found"),
    (["valid", "corrupt"], "invalid session file"),
])
def test_compare_sessions_reports_bad_inputs_without_partial_output(
    isolated_sessions, monkeypatch, capsys, args, reason,
):
    session.save_session([], "m", "valid")
    (isolated_sessions / "corrupt.json").write_text('{"model": "private", "messages": null}', encoding="utf-8")
    before = {p.name: p.read_bytes() for p in isolated_sessions.iterdir()}
    monkeypatch.setattr(sys, "argv", ["corecoder", "--compare-sessions", *args])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 1
    result = capsys.readouterr()
    assert result.out == "" and reason in result.err and "private" not in result.err
    assert {p.name: p.read_bytes() for p in isolated_sessions.iterdir()} == before


@pytest.mark.parametrize("other", [
    ["--list-sessions"], ["--inspect-session", "one"], ["--resume", "one"],
    ["--fork-session", "one", "--before-turn", "1"], ["--before-turn", "1"], ["-p", ""], ["--demo"],
])
def test_compare_sessions_cannot_start_another_mode(other, isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--compare-sessions", "left", "right", *other])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not isolated_sessions.exists()


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


@pytest.mark.parametrize("query", ["OLDER-SNAPSHOT", "QWEN-LEGACY", "翻上次"])
def test_list_sessions_searches_before_the_recent_limit(isolated_sessions, monkeypatch, capsys, query):
    session.save_session([{"role": "user", "content": "翻上次的结果"}], "qwen-legacy", "older-snapshot")
    for i in range(22):
        session.save_session([], "latest-model", f"recent-{i:02}")
    before = {p.name: p.read_bytes() for p in isolated_sessions.iterdir()}
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions", query])

    cli.main()

    output = capsys.readouterr()
    assert output.err == ""
    assert "older-snapshot" in output.out
    assert "recent-" not in output.out
    assert {p.name: p.read_bytes() for p in isolated_sessions.iterdir()} == before


def test_list_sessions_does_not_search_hidden_message_content(isolated_sessions, monkeypatch, capsys):
    session.save_session([
        {"role": "user", "content": "A" * 80 + "hidden-value"},
        {"role": "assistant", "content": "hidden-value"},
    ], "model", "saved")
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions", "hidden-value"])

    cli.main()

    output = capsys.readouterr()
    assert "No saved sessions match." in output.out
    assert "hidden-value" not in output.out + output.err


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


def test_json_session_catalog_has_only_visible_fields(isolated_sessions, monkeypatch, capsys):
    session.save_session([
        {"role": "user", "content": "检查发布"},
        {"role": "assistant", "content": "not in the catalog"},
    ], "local-model", "keep")
    session.save_session([], "other-model", "skip")
    before = {p.name: p.read_bytes() for p in isolated_sessions.iterdir()}
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions", "发布", "--json"])

    cli.main()

    result = capsys.readouterr()
    assert result.err == ""
    [row] = json.loads(result.out)
    assert set(row) == {"id", "model", "saved_at", "preview"}
    assert row["id"] == "keep" and row["model"] == "local-model"
    assert row["preview"] == "检查发布"
    assert "not in the catalog" not in result.out
    assert {p.name: p.read_bytes() for p in isolated_sessions.iterdir()} == before


def test_json_session_catalog_returns_empty_array(isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--list-sessions", "--json"])

    cli.main()

    assert json.loads(capsys.readouterr().out) == []
    assert not isolated_sessions.exists()


def test_session_catalog_json_cannot_start_an_agent(isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--json"])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "--json requires --list-sessions" in capsys.readouterr().err


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
    assert "Retained turns: 1" in result.out
    assert "private" not in result.out and "call_fixture" not in result.out
    assert session.load_session("restored") == (history, "saved-model")
    assert path.read_bytes() == before


def test_fork_keeps_complete_prefix_without_starting_a_run(isolated_sessions, monkeypatch, capsys):
    prefix = [
        {"role": "user", "content": "private starting point"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for i in (1, 2)
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "first note"},
        {"role": "tool", "tool_call_id": "call_2", "content": "second note"},
        {"role": "assistant", "content": "private answer"},
    ]
    original = [*prefix, {"role": "user", "content": "try the first approach"}]
    sid = session.save_session(original, "saved-model", "original")
    source = isolated_sessions / f"{sid}.json"
    before = source.read_bytes()
    monkeypatch.setattr(sys, "argv", ["corecoder", "--fork-session", sid, "--before-turn", "2"])

    cli.main()

    result = capsys.readouterr()
    assert result.err == ""
    child, = (path for path in isolated_sessions.iterdir() if path != source)
    assert result.out.strip() == f"Forked session: {child.stem}"
    assert child.suffix == ".json"
    assert session.load_session(child.stem) == (prefix, "saved-model")
    assert source.read_bytes() == before


@pytest.mark.parametrize("collapse", [False, True])
def test_fork_skips_generated_summary_for_numbering(isolated_sessions, monkeypatch, capsys, collapse):
    history = [
        message for turn in ("old", "keep", "drop") for message in (
            {"role": "user", "content": f"{turn} notes"},
            {"role": "assistant", "content": f"{turn} answer"},
        )
    ]
    context = ContextManager()
    if collapse:
        context._hard_collapse(history, llm=None)
    else:
        assert context._summarize_old(history, llm=None, keep_recent=4)
    sid = session.save_session(history, "saved-model", "original")
    source = isolated_sessions / f"{sid}.json"
    before = source.read_bytes()
    monkeypatch.setattr(sys, "argv", ["corecoder", "--inspect-session", sid])

    cli.main()

    assert "Retained turns: 2" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["corecoder", "--fork-session", sid, "--before-turn", "2"])
    cli.main()

    child, = (path.stem for path in isolated_sessions.glob("*.json") if path != source)
    assert session.load_session(child) == (history[:4], "saved-model")
    assert source.read_bytes() == before


@pytest.mark.parametrize("first_content", [
    "[Context compressed - conversation summary]\nExplain this documentation sample.",
    [{"type": "text", "text": "first question"}],
])
def test_fork_before_first_user_turn_can_start_empty(isolated_sessions, monkeypatch, capsys, first_content):
    sid = session.save_session([
        {"role": "user", "content": first_content},
        {"role": "assistant", "content": "This is a normal answer."},
    ], "saved-model")
    monkeypatch.setattr(sys, "argv", ["corecoder", "--fork-session", sid, "--before-turn", "1"])

    cli.main()

    child = capsys.readouterr().out.strip().removeprefix("Forked session: ")
    assert child != sid
    assert session.load_session(child) == ([], "saved-model")


@pytest.mark.parametrize("argv", [
    ["--fork-session", "id"],
    ["--before-turn", "1"],
    ["--fork-session", "id", "--before-turn", "0"],
    ["--fork-session", "id", "--before-turn", "two"],
    *[["--fork-session", "id", "--before-turn", "1", *option] for option in (
        ["--list-sessions"], ["--inspect-session", "id"], ["--resume", "id"], ["-p", ""], ["--demo"],
    )],
])
def test_fork_rejects_conflicting_or_incomplete_arguments(isolated_sessions, monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["corecoder", *argv])

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err
    assert not isolated_sessions.exists()


@pytest.mark.parametrize("history,turn,count", [
    ([], "1", 0),
    ([{"role": "user", "content": "only prompt"}], "2", 1),
    ([
        {"role": "user", "content": "[Hard context reset]\n"},
        {"role": "assistant", "content": "Context restored. Continuing from where we left off."},
    ], "1", 0),
])
def test_fork_rejects_turns_not_in_the_archive(isolated_sessions, monkeypatch, capsys, history, turn, count):
    sid = session.save_session(history, "saved-model", "original")
    source = isolated_sessions / f"{sid}.json"
    before = source.read_bytes()
    monkeypatch.setattr(sys, "argv", ["corecoder", "--fork-session", sid, "--before-turn", turn])

    with pytest.raises(SystemExit) as error:
        cli.main()

    result = capsys.readouterr()
    assert error.value.code == 1
    assert f"Retained turns: {count}" in result.err and not result.out
    assert list(isolated_sessions.iterdir()) == [source]
    assert source.read_bytes() == before


@pytest.mark.parametrize("failure", ["collision", "fsync", "link"])
def test_failed_fork_leaves_existing_archives_unchanged(isolated_sessions, monkeypatch, capsys, failure):
    sid = session.save_session([{"role": "user", "content": "first"}], "saved-model", "original")
    session.save_session([{"role": "user", "content": "not the fork"}], "other-model", "occupied")
    before = {path.name: path.read_bytes() for path in isolated_sessions.iterdir()}

    def fail(*args):
        raise OSError("fixture write refused")

    if failure == "collision":
        monkeypatch.setattr(session, "_new_session_id", lambda: "occupied")
    else:
        monkeypatch.setattr(session.os, failure, fail)
    monkeypatch.setattr(sys, "argv", ["corecoder", "--fork-session", sid, "--before-turn", "1"])

    with pytest.raises(SystemExit) as error:
        cli.main()

    result = capsys.readouterr()
    assert error.value.code == 1
    assert "Could not create a new session file" in result.err and not result.out
    assert {path.name: path.read_bytes() for path in isolated_sessions.iterdir()} == before


def test_fork_reports_created_session_even_if_staging_cleanup_fails(isolated_sessions, monkeypatch, capsys, caplog):
    sid = session.save_session([{"role": "user", "content": "first"}], "saved-model", "original")
    unlink = type(isolated_sessions).unlink

    def fail_staging_cleanup(path, *args, **kwargs):
        if path.parent == isolated_sessions.resolve() and path.suffix == ".tmp":
            raise PermissionError("fixture cleanup refused")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(isolated_sessions), "unlink", fail_staging_cleanup)
    monkeypatch.setattr(sys, "argv", ["corecoder", "--fork-session", sid, "--before-turn", "1"])

    cli.main()

    child = capsys.readouterr().out.strip().removeprefix("Forked session: ")
    assert session.load_session(child) == ([], "saved-model")
    assert "Could not remove temporary session file" in caplog.text


@pytest.mark.parametrize("prefix", [
    ("[Context compressed - conversation summary]", "Got it, I have the context from our earlier conversation."),
    ("[Hard context reset]", "Context restored. Continuing from where we left off."),
])
def test_export_keeps_only_retained_visible_text(prefix, isolated_sessions, monkeypatch, capsys):
    marker, ack = prefix
    history = [
        {"role": "user", "content": f"{marker}\nprivate summary"},
        {"role": "assistant", "content": ack},
        {"role": "system", "content": "private system message"},
        {"role": "user", "content": "你好\n\n`next step`"},
        {"role": "assistant", "content": "Visible answer.",
         "tool_calls": [{"function": {"arguments": "private arguments"}}], "reasoning_content": "private reasoning"},
        {"role": "tool", "content": "private tool response"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "private call"}]},
        {"role": "user", "content": {"note": "private structured data"}},
        {"role": "user", "content": f"{marker}\nQuoted in dialogue"},
    ]
    isolated_sessions.mkdir()
    (isolated_sessions / "fixture.json").write_text(
        json.dumps({"model": "fixture-model", "messages": history}, ensure_ascii=False), encoding="utf-8",
    )
    before = {p.name: p.read_bytes() for p in isolated_sessions.iterdir()}
    monkeypatch.setattr(sys, "argv", ["corecoder", "--export-session", "fixture"])

    for _ in range(2):
        cli.main()
        result = capsys.readouterr()
        assert result.err == ""
        assert result.out == (
            "# Session fixture\n\n## User\n\n你好\n\n`next step`\n\n"
            "## Assistant\n\nVisible answer.\n\n"
            f"## User\n\n{marker}\nQuoted in dialogue\n"
        )
    assert {p.name: p.read_bytes() for p in isolated_sessions.iterdir()} == before


def test_export_empty_dialogue_has_no_hidden_content(isolated_sessions, monkeypatch, capsys):
    session.save_session([{"role": "user", "content": "  "}, {"role": "tool", "content": "hidden"}], "m", "empty")
    monkeypatch.setattr(sys, "argv", ["corecoder", "--export-session", "empty"])

    cli.main()

    result = capsys.readouterr()
    assert result.err == ""
    assert result.out == "# Session empty\n\nNo retained dialogue.\n"


@pytest.mark.parametrize("other", [["--resume", "id"], ["-p", ""], ["--demo"], ["--list-sessions"], ["--json"]])
def test_export_session_rejects_conflicting_modes(other, isolated_sessions, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["corecoder", "--export-session", "fixture", *other])
    with pytest.raises(SystemExit) as error:
        cli.main()
    result = capsys.readouterr()
    assert error.value.code == 2 and not result.out
    diagnostic = "requires --list-sessions" if other == ["--json"] else "--export-session"
    assert diagnostic in result.err
    assert not isolated_sessions.exists()


@pytest.mark.parametrize("mode", ["--inspect-session", "--resume", "--fork-session", "--export-session"])
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
    args = ["corecoder", mode, "fixture"]
    if mode == "--fork-session":
        args.extend(["--before-turn", "1"])
    monkeypatch.setattr(sys, "argv", args)

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
