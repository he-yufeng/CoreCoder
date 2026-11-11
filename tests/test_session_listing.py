from corecoder import session as session_module


def test_non_object_json_file_is_skipped_in_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = session_module.save_session([{"role": "user", "content": "kept"}], "m")
    (tmp_path / "stray.json").write_text("[1, 2, 3]", encoding="utf-8")

    ids = [s["id"] for s in session_module.list_sessions()]
    assert ids == [sid]


def test_null_messages_are_skipped_in_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)

    sid = session_module.save_session([{"role": "user", "content": "kept"}], "m")
    (tmp_path / "empty.json").write_text(
        '{"id": "empty", "model": "m", "messages": null}', encoding="utf-8"
    )

    ids = [s["id"] for s in session_module.list_sessions()]
    assert ids == [sid]
