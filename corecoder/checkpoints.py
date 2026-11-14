"""Session-scoped undo for file mutations.

edit_file and write_file record a checkpoint before touching a file; /undo
pops the latest one and restores the previous bytes (or removes the file if
it did not exist). The stack persists to ~/.corecoder/checkpoints.json so
/undo still reaches back into earlier sessions after a restart. Bash side
effects are not tracked, only the two file-writing tools.
"""

import base64
import json
from dataclasses import dataclass
from pathlib import Path

CHECKPOINTS_FILE = Path.home() / ".corecoder" / "checkpoints.json"

@dataclass
class _Checkpoint:
    path: str
    prior: bytes | None
    after: bytes | None = None


_stack: list[_Checkpoint] = []
_loaded = False


def _encode(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode("ascii") if data is not None else None


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        entries = json.loads(CHECKPOINTS_FILE.read_text(encoding="utf-8"))
        for e in entries:
            prior = e["prior"]
            after = e.get("after")
            _stack.append(_Checkpoint(
                e["path"],
                base64.b64decode(prior) if prior is not None else None,
                base64.b64decode(after) if after is not None else None,
            ))
    except (OSError, ValueError, KeyError, TypeError):
        # a missing or corrupt stack just means nothing to undo
        pass


def _save() -> None:
    entries = [
        {"path": c.path, "prior": _encode(c.prior), "after": _encode(c.after)}
        for c in _stack
    ]
    try:
        CHECKPOINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHECKPOINTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries), encoding="utf-8")
        tmp.replace(CHECKPOINTS_FILE)
    except OSError:
        # undo is a safety net; losing its persistence must not break the edit
        pass


def record(path: Path) -> None:
    """Capture the pre-mutation state of path. Call right before writing."""
    _ensure_loaded()
    _stack.append(_Checkpoint(str(path), path.read_bytes() if path.exists() else None))
    _save()


def wrote(path: Path, content: bytes) -> None:
    """Record the bytes produced by the mutation that followed record()."""
    _ensure_loaded()
    if _stack and _stack[-1].path == str(path) and _stack[-1].after is None:
        _stack[-1].after = content
        _save()


def undo() -> str:
    """Restore the most recent checkpoint."""
    _ensure_loaded()
    if not _stack:
        return "Nothing to undo."
    checkpoint = _stack[-1]
    path_str, prior = checkpoint.path, checkpoint.prior
    p = Path(path_str)
    current = p.read_bytes() if p.exists() else None
    if checkpoint.after is not None and p.exists() and current != checkpoint.after:
        return (
            f"Skipped {path_str}: the file changed after the edit. "
            "The checkpoint was kept; review the file before undoing it."
        )
    _stack.pop()
    if prior is None:
        p.unlink(missing_ok=True)
        msg = f"Removed {path_str} (created this session)."
    else:
        # the parent tree may be gone by now: bash side effects are untracked,
        # so recreate it instead of dying on FileNotFoundError
        recreated = not p.parent.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(prior)
        msg = f"Restored {path_str}." if not recreated else (
            f"Restored {path_str} (recreated missing parent directories)."
        )
    _save()
    return msg


def pending() -> int:
    _ensure_loaded()
    return len(_stack)


def clear() -> None:
    global _loaded
    _stack.clear()
    _loaded = True  # the stack is known-empty; don't reload the file we just removed
    try:
        CHECKPOINTS_FILE.unlink(missing_ok=True)
    except OSError:
        pass
