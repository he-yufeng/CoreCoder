from corecoder import session as session_module
from corecoder.session import load_session, save_session


def test_default_session_ids_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    first_id = save_session([{"role": "user", "content": "first"}], "model-a")
    second_id = save_session([{"role": "user", "content": "second"}], "model-b")

    assert first_id != second_id
    assert load_session(first_id) == (
        [{"role": "user", "content": "first"}],
        "model-a",
    )
    assert load_session(second_id) == (
        [{"role": "user", "content": "second"}],
        "model-b",
    )


def test_session_id_path_traversal_is_neutralized(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = save_session([{"role": "user", "content": "x"}], "m", "../../etc/passwd")

    assert sid == "passwd"
    assert (tmp_path / "passwd.json").exists()
    # the same traversal string round-trips through the parent-dir boundary check
    assert load_session("../../etc/passwd") == ([{"role": "user", "content": "x"}], "m")


def test_session_id_absolute_path_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = save_session([{"role": "user", "content": "x"}], "m", "/etc/shadow")

    assert sid == "shadow"
    assert (tmp_path / "shadow.json").exists()


def test_session_id_windows_backslash_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = save_session([{"role": "user", "content": "x"}], "m", r"..\..\secret")

    assert sid == "secret"


def test_session_id_length_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = save_session([{"role": "user", "content": "x"}], "m", "a" * 500)

    assert len(sid) <= 100
    assert (tmp_path / f"{sid}.json").exists()


def test_corrupt_session_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    (tmp_path / "broken.json").write_text("{ not valid json", encoding="utf-8")

    assert load_session("broken") is None


def test_session_roundtrips_unicode(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    msgs = [{"role": "user", "content": "请帮我修复这个 bug"}]
    sid = save_session(msgs, "model-zh")

    raw = (tmp_path / f"{sid}.json").read_bytes()
    assert "请帮我修复这个 bug".encode() in raw
    assert load_session(sid) == (msgs, "model-zh")


def test_invalid_utf8_session_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    raw = b'{"id": "bad", "model": "m", "messages": [{"role": "user", "content": "\xe4\xb8'
    (tmp_path / "bad.json").write_bytes(raw)

    assert load_session("bad") is None


def test_invalid_utf8_session_is_skipped_in_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = save_session([{"role": "user", "content": "still here"}], "m")
    (tmp_path / "zz_corrupt.json").write_bytes(b'{"id": "\xff\xfe')

    ids = [s["id"] for s in session_module.list_sessions()]
    assert sid in ids
    assert len(ids) == 1
