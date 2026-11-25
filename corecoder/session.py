"""Session persistence - save and resume conversations.

Claude Code maintains session state via QueryEngine (1295 lines).
CoreCoder distills this to: JSON dump of messages + model config.
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

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
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    session_id = _normalize_session_id(session_id)

    data = {
        "id": session_id,
        "model": model,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "messages": messages,
    }

    path = _session_path(session_id)
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
        staged.replace(path)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return session_id


@dataclass(frozen=True)
class SavedSession:
    session_id: str
    model: str
    saved_at: str
    messages: list[dict]


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


def list_sessions() -> list[dict]:
    """List available sessions, newest first."""
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
        sessions.append({
            "id": f.stem,
            "model": saved.model,
            "saved_at": saved.saved_at,
            "preview": preview,
        })

    return sessions[:20]  # cap at 20
