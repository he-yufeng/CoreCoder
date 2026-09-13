"""Shell command execution with safety checks.

Claude Code's BashTool is 1,143 lines. This is the distilled version:
- Output capture with truncation (head+tail preserved)
- Timeout support
- Dangerous command detection
- Working directory tracking (cd awareness)
"""

import os
import re
import shlex
import subprocess
import threading
from typing import ClassVar

from .base import Tool

# Track cwd across commands (Claude Code does this too). Thread-local, so that
# when the agent executes tools in parallel two bash calls never race on one
# shared global: each worker thread carries its own cwd. See article 05.
_local = threading.local()

# patterns that could wreck the filesystem or leak secrets
_DANGEROUS_PATTERNS = [
    # recursive delete aimed at root/home (force flag optional)
    (r"\brm\s+(-\w*)?-r\w*\s+(/|~|\$HOME)", "recursive delete on home/root"),
    # recursive (-r/-R) and force (-f) flags together, in any order or spacing
    (r"\brm\b(?=(?:.*\s)?-\w*[rR])(?=(?:.*\s)?-\w*f)", "force recursive delete"),
    # the same, written with long-form flags
    (r"\brm\b.*--recursive\b.*--force\b|\brm\b.*--force\b.*--recursive\b", "force recursive delete"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*of=/dev/", "raw disk write"),
    (r">\s*/dev/sd[a-z]", "overwrite block device"),
    (r"\bchmod\s+(-R\s+)?777\s+/", "chmod 777 on root"),
    (r":\(\)\s*\{.*:\|:.*\}", "fork bomb"),
    (r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh\b", "pipe curl to shell"),
    (r"\bwget\b.*\|\s*(sudo\s+)?(ba)?sh\b", "pipe wget to shell"),
]


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command. Returns stdout, stderr, and exit code. "
        "Use this for running tests, installing packages, git operations, etc."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 120)",
            },
        },
        "required": ["command"],
    }

    def execute(self, command: str, timeout: int = 120) -> str:
        # safety check
        warning = _check_dangerous(command)
        if warning:
            return f"⚠ Blocked: {warning}\nCommand: {command}\nIf intentional, modify the command to be more specific."

        # use this thread's own tracked working directory
        cwd = getattr(_local, "cwd", None) or os.getcwd()

        try:
            proc = subprocess.run(
                command,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
            )

            # track cd commands so next command runs in the right place
            if proc.returncode == 0:
                _update_cwd(command, cwd)
            out = proc.stdout
            if proc.stderr:
                out += f"\n[stderr]\n{proc.stderr}"
            if proc.returncode != 0:
                out += f"\n[exit code: {proc.returncode}]"
            # keep head + tail to preserve the most useful info
            if len(out) > 15_000:
                out = (
                    out[:6000]
                    + f"\n\n... truncated ({len(out)} chars total) ...\n\n"
                    + out[-3000:]
                )
            return out.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: timed out after {timeout}s"
        except Exception as e:  # noqa: BLE001
            # anything else from the OS (spawn failure etc.) also comes back as text
            return f"Error running command: {e}"


def _check_dangerous(cmd: str) -> str | None:
    """Return a warning string if the command looks destructive, else None."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, cmd):
            return reason
    return None


def _split_statements(command: str) -> list[str]:
    """Split a command on its unquoted `&&` and `;` statement boundaries.

    Goes through shlex so that a separator inside a string literal — `echo
    "a; cd b"` — stays quoted data instead of looking like a command break.
    """
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=";&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: keep the line as one statement
        return [command]
    statements, current = [], []
    for token in tokens:
        if token in (";", "&&"):
            statements.append(" ".join(current))
            current = []
        else:
            current.append(token)
    statements.append(" ".join(current))
    return statements


def _update_cwd(command: str, current_cwd: str):
    """Track directory changes from cd commands, per thread.

    Walks every cd in a `&&` / `;` chain, resolving relative targets against
    the directory the previous cd landed in (not the original cwd), so
    `cd a && cd b` ends in a/b, and so does `cd a; cd b`. A bare `cd` goes
    home. A cd inside a subshell (`(cd a)`) is skipped on purpose: it does not
    move the shell that runs the next command.
    """
    running = current_cwd
    changed = False
    for statement in _split_statements(command):
        words = statement.split()
        if not words or words[0] != "cd":
            continue
        # a bare `cd` goes home; `cd -` needs OLDPWD, which we do not track
        target = " ".join(words[1:]).strip().strip("'\"") if words[1:] else "~"
        if not target or target == "-":
            continue
        new_dir = os.path.normpath(os.path.join(running, os.path.expanduser(target)))
        if os.path.isdir(new_dir):
            running = new_dir
            changed = True
    if changed:
        _local.cwd = running
