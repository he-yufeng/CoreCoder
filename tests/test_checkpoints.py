"""Undo checkpoints for edit_file / write_file."""

from __future__ import annotations

import base64
import json

from corecoder import checkpoints
from corecoder.tools.edit import EditFileTool
from corecoder.tools.write import WriteFileTool


def _simulate_restart():
    # drop the in-memory stack and force a reload from disk
    checkpoints._stack.clear()
    checkpoints._loaded = False


def test_edit_then_undo_restores_previous_bytes(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    assert EditFileTool().execute(str(f), "v1", "v2").startswith("Edited")
    assert f.read_text() == "v2\n"

    assert checkpoints.undo() == f"Restored {f}."
    assert f.read_text() == "v1\n"


def test_undo_removes_file_created_by_write(tmp_path):
    f = tmp_path / "new.py"
    assert not f.exists()
    WriteFileTool().execute(str(f), "print(1)\n")
    assert f.exists()

    assert checkpoints.undo() == f"Removed {f} (created this session)."
    assert not f.exists()


def test_undo_pops_one_mutation_at_a_time(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    EditFileTool().execute(str(f), "v1", "v2")
    EditFileTool().execute(str(f), "v2", "v3")

    assert checkpoints.pending() == 2
    checkpoints.undo()
    assert f.read_text() == "v2\n"
    checkpoints.undo()
    assert f.read_text() == "v1\n"


def test_failed_edit_leaves_no_checkpoint(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    # old_string absent -> error before any write
    result = EditFileTool().execute(str(f), "missing", "v2")
    assert result.startswith("Error:")
    assert checkpoints.pending() == 0
    assert checkpoints.undo() == "Nothing to undo."


def test_undo_recreates_a_deleted_parent_tree(tmp_path):
    # bash side effects are untracked: if the parent dir is removed between
    # the checkpoint and /undo, restore recreates it instead of dying.
    f = tmp_path / "deep" / "nested" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("v1\n", encoding="utf-8")
    EditFileTool().execute(str(f), "v1", "v2")
    import shutil

    shutil.rmtree(tmp_path / "deep")
    assert not f.parent.exists()

    msg = checkpoints.undo()
    assert msg == f"Restored {f} (recreated missing parent directories)."
    assert f.read_text() == "v1\n"


def test_undo_survives_a_restart(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    EditFileTool().execute(str(f), "v1", "v2")

    _simulate_restart()
    assert checkpoints.pending() == 1
    assert checkpoints.undo() == f"Restored {f}."
    assert f.read_text() == "v1\n"

    _simulate_restart()  # the popped entry stays popped
    assert checkpoints.pending() == 0
    assert checkpoints.undo() == "Nothing to undo."


def test_restart_remembers_a_file_created_by_write(tmp_path):
    f = tmp_path / "new.py"
    WriteFileTool().execute(str(f), "print(1)\n")

    _simulate_restart()
    assert checkpoints.undo() == f"Removed {f} (created this session)."
    assert not f.exists()


def test_corrupt_stack_file_means_nothing_to_undo(tmp_path):
    checkpoints.CHECKPOINTS_FILE.write_text("{ nope", encoding="utf-8")

    _simulate_restart()
    assert checkpoints.pending() == 0
    assert checkpoints.undo() == "Nothing to undo."


def test_undo_keeps_later_manual_edits(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    EditFileTool().execute(str(f), "v1", "v2")
    f.write_text("keep my manual work\n", encoding="utf-8")

    result = checkpoints.undo()

    assert "changed after the edit" in result
    assert f.read_text() == "keep my manual work\n"
    assert checkpoints.pending() == 1


def test_undo_keeps_a_file_replaced_after_creation(tmp_path):
    f = tmp_path / "new.py"
    WriteFileTool().execute(str(f), "agent\n")
    f.write_text("manual\n", encoding="utf-8")

    result = checkpoints.undo()

    assert "changed after the edit" in result
    assert f.read_text() == "manual\n"
    assert checkpoints.pending() == 1


def test_conflicted_undo_keeps_the_checkpoint_until_resolved(tmp_path):
    first = tmp_path / "first.py"
    latest = tmp_path / "latest.py"
    first.write_text("old\n", encoding="utf-8")
    EditFileTool().execute(str(first), "old", "agent")
    WriteFileTool().execute(str(latest), "agent\n")
    latest.write_text("manual\n", encoding="utf-8")

    assert "changed after the edit" in checkpoints.undo()
    assert checkpoints.pending() == 2
    latest.unlink()
    assert checkpoints.undo() == f"Removed {latest} (created this session)."
    assert checkpoints.undo() == f"Restored {first}."
    assert first.read_text() == "old\n"
    assert checkpoints.pending() == 0


def test_legacy_checkpoint_still_restores(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("v1\n", encoding="utf-8")
    checkpoints.CHECKPOINTS_FILE.write_text(
        json.dumps([{"path": str(f), "prior": base64.b64encode(b"v1\n").decode()}]),
        encoding="utf-8",
    )
    f.write_text("v2\n", encoding="utf-8")

    _simulate_restart()
    assert checkpoints.undo() == f"Restored {f}."
    assert f.read_text() == "v1\n"
