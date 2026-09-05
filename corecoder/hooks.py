"""User shell hooks around tool calls, distilled from Claude Code's hooks.

Definitions live in ~/.corecoder/hooks.json:

    {"PreToolUse":  [{"matcher": "bash", "command": "..."}],
     "PostToolUse": [{"matcher": "*",    "command": "..."}]}

Each hook is a shell command fed the tool call as JSON on stdin. A pre hook
vetoes the call with exit code 2; its stderr goes back to the model as the
reason. Post hooks only observe. A hook that errors or hangs is skipped with
a warning: hooks assist the loop, they never get to kill it.
"""

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

HOOKS_FILE = Path.home() / ".corecoder" / "hooks.json"
TIMEOUT = 10  # seconds; a hung hook must not hang the agent


class Hooks:
    """Pre/post shell commands matched against tool names."""

    def __init__(self, pre: list[dict], post: list[dict]):
        self.pre = pre
        self.post = post

    def __bool__(self):
        return bool(self.pre or self.post)

    def run_pre(self, tool_name: str, tool_input: dict) -> str | None:
        """Fire matching PreToolUse hooks. A string return blocks the call and
        is what the model gets as the tool result; None means carry on."""
        payload = {"tool_name": tool_name, "tool_input": tool_input}
        for hook in self.pre:
            out = _fire(hook, payload)
            if out is not None and out.returncode == 2:
                reason = out.stderr.strip() or "no reason given"
                return "Blocked by hook: " + reason
        return None

    def run_post(self, tool_name: str, tool_input: dict, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        payload = {"tool_name": tool_name, "tool_input": tool_input, "tool_response": result}
        for hook in self.post:
            _fire(hook, payload)


def load_hooks(path: Path = HOOKS_FILE) -> Hooks:
    """Read hooks.json. A missing file means no hooks; a broken one gets one
    clear warning and is otherwise ignored."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Hooks([], [])
    except (json.JSONDecodeError, OSError) as e:
        log.warning("ignoring %s: %s", path, e)
        return Hooks([], [])
    pre = [h for h in data.get("PreToolUse", []) if h.get("command")]
    post = [h for h in data.get("PostToolUse", []) if h.get("command")]
    return Hooks(pre, post)


def _fire(hook: dict, payload: dict):
    """Run one hook if its matcher applies (exact tool name or "*"), payload
    as JSON on stdin. None means it didn't match or failed and was skipped."""
    matcher = hook.get("matcher", "")
    if matcher not in ("", "*", payload["tool_name"]):
        return None
    try:
        proc = subprocess.run(
            hook["command"], shell=True, check=False, input=json.dumps(payload),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("hook skipped (%s): %s", e.__class__.__name__, hook["command"])
        return None
    if proc.returncode not in (0, 2):
        log.warning("hook exited %d, skipped: %s", proc.returncode, hook["command"])
        return None
    return proc
