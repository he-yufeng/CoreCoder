import errno
import json
import signal

import pytest

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


def test_failed_save_preserves_the_last_complete_session(tmp_path, monkeypatch):
    resource = pytest.importorskip("resource")
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    old_messages = [{"role": "user", "content": "previous conversation"}]
    sid = save_session(old_messages, "model-a", "keep")
    saved = tmp_path / f"{sid}.json"
    before = saved.read_bytes()

    limits = resource.getrlimit(resource.RLIMIT_FSIZE)
    if limits[1] != resource.RLIM_INFINITY and limits[1] < 512:
        pytest.skip("hard file size limit is below the test threshold")
    handler = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (512, limits[1]))
        with pytest.raises(OSError) as failed:
            save_session([{"role": "user", "content": "x" * 4096}], "model-b", sid)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, limits)
        signal.signal(signal.SIGXFSZ, handler)

    assert failed.value.errno == errno.EFBIG
    assert saved.read_bytes() == before
    assert load_session(sid) == (old_messages, "model-a")
    assert list(tmp_path.iterdir()) == [saved]


def test_successful_save_replaces_the_complete_session(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    sid = save_session([{"role": "user", "content": "before"}], "model-a", "keep")
    messages = [{"role": "user", "content": "第二次保存"}]
    assert save_session(messages, "model-b", sid) == sid
    assert load_session(sid) == (messages, "model-b")
    assert list(tmp_path.iterdir()) == [tmp_path / "keep.json"]


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


@pytest.mark.parametrize("record", ["[]", "null", "42", '\"hello\"'])
def test_non_object_session_file_returns_none(tmp_path, monkeypatch, record):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    fixture = tmp_path / "not_a_session.json"
    fixture.write_text(record, encoding="utf-8")

    assert load_session("not_a_session") is None
    assert fixture.read_text(encoding="utf-8") == record


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


@pytest.mark.parametrize("bad_fields", [
    {"messages": None},
    {"messages": [None]},
    {"messages": [{"content": "missing role"}]},
    {"model": 10},
    {"model": "  "},
])
def test_malformed_record_does_not_hide_valid_sessions(tmp_path, monkeypatch, bad_fields):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    sid = save_session([{"role": "user", "content": "keep me"}], "m", "keep")
    broken = tmp_path / "zz_broken.json"
    original = json.dumps({"messages": [], "model": "m", **bad_fields})
    broken.write_text(original, encoding="utf-8")

    assert load_session("zz_broken") is None
    assert [s["id"] for s in session_module.list_sessions()] == [sid]
    assert broken.read_text(encoding="utf-8") == original


def test_catalog_uses_resumable_filename_not_old_embedded_id(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    (tmp_path / "renamed.json").write_text(json.dumps({
        "id": "old-name", "model": "m", "messages": [],
    }), encoding="utf-8")

    entry, = session_module.list_sessions()
    assert entry["id"] == "renamed"
    assert load_session(entry["id"]) == ([], "m")
