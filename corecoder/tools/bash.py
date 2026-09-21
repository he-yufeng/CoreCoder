"""Shell command execution with safety checks.

Claude Code's BashTool is 1,143 lines. This is the distilled version:
- Output capture with truncation (head+tail preserved)
- Timeout support
- Dangerous command detection
- Working directory tracking (cd awareness)
"""

import functools
import os
import re
import shutil
import subprocess
import threading
from typing import ClassVar

from .base import Tool


@functools.lru_cache(maxsize=1)
def _find_bash() -> str | None:
    """Locate Git Bash on Windows so POSIX commands and shell hooks run."""
    if os.name != "nt":
        return None
    for p in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if os.path.isfile(p):
            return p
    git = shutil.which("git")
    if git and os.path.isfile(b := os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe")):
        return b
    return b if (b := shutil.which("bash")) and "system32" not in b.lower() else None

# Track cwd across commands (Claude Code does this too). Thread-local, so that
# when the agent executes tools in parallel two bash calls never race on one
# shared global: each worker thread carries its own cwd. See article 05.
_local = threading.local()


def get_tracked_cwd() -> str | None:
    """The cwd tracked for this thread, if any."""
    return getattr(_local, "cwd", None)


def set_tracked_cwd(path: str | None) -> None:
    """Set the tracked cwd for this thread (session handoff at pool edges)."""
    if path is None:
        if hasattr(_local, "cwd"):
            del _local.cwd
    else:
        _local.cwd = path

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
        cwd = get_tracked_cwd() or os.getcwd()

        bash_exe = _find_bash()
        cmd: list[str] | str = [bash_exe, "-c", command] if bash_exe else command
        try:
            proc = subprocess.run(
                cmd,
                shell=not bash_exe,
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


def _split_words(statement: str) -> list[str]:
    """Split one statement into words on unquoted whitespace, stripping the
    quotes. Backslashes are kept verbatim (Windows paths must not lose them,
    so this deliberately does not use shlex's posix escape processing)."""
    words: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in statement:
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        elif ch in ("'", '"'):
            quote = ch
        elif ch.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _split_statements(command: str) -> list[list[str]]:
    """Split a shell command into per-statement word lists on `&&` and `;`,
    honoring quotes. Words come back dequoted, so `cd "my dir"` arrives as
    ["cd", "my dir"] and a separator inside quotes never splits."""
    statements: list[str] = []
    quote: str | None = None
    start = 0
    i = 0
    while i < len(command):
        ch = command[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        else:
            is_and = command[i : i + 2] == "&&"
            if ch == ";" or is_and:
                statement = command[start:i]
                if statement.strip():
                    statements.append(statement)
                i += 2 if is_and else 1
                start = i
                continue
        i += 1
    tail = command[start:]
    if tail.strip():
        statements.append(tail)
    return [_split_words(statement) for statement in statements]


def _update_cwd(command: str, current_cwd: str):
    """Track directory changes from cd commands, per thread."""
    # walk each cd in a && or ; chain, resolving relative targets against the dir the
    # previous cd landed in (not the original cwd) so `cd a && cd b` ends in a/b.
    # a parenthesized group is a subshell — `( cd a )` never changes this shell's
    # cwd, so scrub those before scanning; a bare `cd` goes home.
    scrubbed = re.sub(r"\([^()]*\)", " ", command)
    running = current_cwd
    changed = False
    for words in _split_statements(scrubbed):
        if words and words[0] == "cd":
            target = words[1] if len(words) > 1 else "~"
            new_dir = os.path.normpath(os.path.join(running, os.path.expanduser(target)))
            if os.path.isdir(new_dir):
                running = new_dir
                changed = True
    if changed:
        _local.cwd = running
