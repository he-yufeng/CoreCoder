"""Session persistence - save and resume conversations.

Claude Code maintains session state via QueryEngine (1295 lines).
CoreCoder distills this to: JSON dump of messages + model config.
"""

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from .context import ContextManager

SESSIONS_DIR = Path.home() / ".corecoder" / "sessions"
_SAFE_SESSION_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SESSION_ID_LEN = 100  # keep filenames comfortably under the OS limit


def _normalize_session_id(session_id: str | None) -> str:
    if not session_id:
        return _new_session_id()

    name = session_id.strip().replace("\\", "/").split("/")[-1]
    name = _SAFE_SESSION_RE.sub("-", name).strip(".-_")
    if len(name) > _MAX_SESSION_ID_LEN:
        name = name[:_MAX_SESSION_ID_LEN].strip(".-_")
    return name or _new_session_id()


def _new_session_id() -> str:
    return f"session_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _session_path(session_id: str) -> Path:
    path = (SESSIONS_DIR / f"{_normalize_session_id(session_id)}.json").resolve()
    root = SESSIONS_DIR.resolve()
    if root != path.parent:
        raise ValueError("Invalid session id")
    return path


def save_session(messages: list[dict], model: str, session_id: str | None = None) -> str:
    """Save conversation to disk. Returns the session ID."""
    return _write_session(messages, model, _normalize_session_id(session_id), create_only=False)


def _write_session(messages: list[dict], model: str, session_id: str, *, create_only: bool) -> str:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "id": session_id,
        "model": model,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": messages,
    }

    path = SESSIONS_DIR.resolve() / f"{session_id}.json" if create_only else _session_path(session_id)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    staged = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".session-", suffix=".tmp", delete=False,
        ) as handle:
            staged = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            os.link(staged, path)  # publish the complete file without replacing an existing name
        else:
            staged.replace(path)
            staged = None
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning("Could not remove temporary session file")
    return session_id


@dataclass(frozen=True)
class SavedSession:
    session_id: str
    model: str
    saved_at: str
    messages: list[dict]

    def retained_turn_starts(self) -> list[int]:
        start = 2 if ContextManager.has_summary_prefix(self.messages) else 0
        return [i for i in range(start, len(self.messages)) if self.messages[i]["role"] == "user"]


def fork_session(saved: SavedSession, before_turn: int) -> str:
    starts = saved.retained_turn_starts()
    if not 1 <= before_turn <= len(starts):
        raise ValueError(f"Turn {before_turn} is out of range. Retained turns: {len(starts)}.")
    prefix = saved.messages[:starts[before_turn - 1]]
    return _write_session(prefix, saved.model, _new_session_id(), create_only=True)


def common_message_prefix(left: list[dict], right: list[dict]) -> int:
    for index, (left_message, right_message) in enumerate(zip(left, right)):
        if json.dumps(left_message, sort_keys=True) != json.dumps(right_message, sort_keys=True):
            return index
    return min(len(left), len(right))


class SessionFormatError(ValueError):
    """A readable file does not contain a session."""


def read_session(session_id: str) -> SavedSession:
    """Read a session without changing it; raise OSError or ValueError on failure."""
    path = _session_path(session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SessionFormatError("Expected a session object")

    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise SessionFormatError("Expected a model name")

    messages = data.get("messages")
    if not isinstance(messages, list) or any(
        not isinstance(message, dict) or not isinstance(message.get("role"), str) or not message["role"].strip()
        for message in messages
    ):
        raise SessionFormatError("Expected messages with roles")

    saved_at = data.get("saved_at")
    return SavedSession(path.stem, model, saved_at if isinstance(saved_at, str) and saved_at else "?", messages)


def load_session(session_id: str) -> tuple[list[dict], str] | None:
    """Load a saved session. Returns (messages, model) or None."""
    try:
        saved = read_session(session_id)
    except (ValueError, OSError):
        return None
    return saved.messages, saved.model


def list_sessions(query: str = "") -> list[dict]:
    """List up to 20 sessions matching ID, model, or displayed preview."""
    query = query.strip().casefold()
    sessions = []
    for f in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        if f.stem != _normalize_session_id(f.stem):
            continue
        try:
            saved = read_session(f.stem)
        except (ValueError, OSError):
            continue

        preview = ""
        for message in saved.messages:
            content = message.get("content")
            if message["role"] == "user" and isinstance(content, str) and content:
                preview = content[:80]
                break
        if query and not any(query in field.casefold() for field in (f.stem, saved.model, preview)):
            continue
        sessions.append({
            "id": f.stem,
            "model": saved.model,
            "saved_at": saved.saved_at,
            "preview": preview,
        })
        if len(sessions) == 20:
            break

    return sessions
