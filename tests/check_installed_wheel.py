"""Run after installing the built wheel, not an editable checkout."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath


def main():
    wheel = Path(sys.argv[1]).resolve()
    checkout = Path(__file__).resolve().parent.parent
    import corecoder

    package = Path(corecoder.__file__).resolve().parent
    assert not package.is_relative_to(checkout / "corecoder"), "checkout shadows the installed wheel"
    assert corecoder.__version__ == metadata.version("corecoder")
    with zipfile.ZipFile(wheel) as archive:
        sources = [name for name in archive.namelist() if name.startswith("corecoder/") and name.endswith(".py")]
        expected = {path.relative_to(checkout).as_posix() for path in (checkout / "corecoder").rglob("*.py")}
        assert set(sources) == expected and len(sources) == len(expected), "wheel source inventory differs"
        for name in sources:
            relative = PurePosixPath(name)
            assert ".." not in relative.parts
            assert (checkout / relative).read_bytes() == archive.read(name), name
            assert (package.parent / relative).read_bytes() == archive.read(name), name
    with tempfile.TemporaryDirectory(prefix="corecoder-wheel-") as work:
        cli = shutil.which("corecoder")
        assert cli, "installed console entry point is missing"
        help_result = subprocess.run([cli, "--help"], cwd=work, capture_output=True, text=True, check=True)
        assert "corecoder" in help_result.stdout.lower()
        env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CORECODER_API_KEY", "PYTHONPATH"):
            env.pop(key, None)
        env.update(HOME=work, USERPROFILE=work)
        sessions = Path(work) / ".corecoder" / "sessions"
        sessions.mkdir(parents=True)
        original = sessions / "wheel-session.json"
        prefix = [
            {"role": "user", "content": "saved before the restart"},
            {"role": "assistant", "content": "first answer"},
        ]
        original.write_text(json.dumps({
            "id": "wheel-session",
            "model": "fixture-model",
            "saved_at": "2026-01-01 12:00:00",
            "messages": [*prefix, {"role": "user", "content": "try another approach"}],
        }), encoding="utf-8")
        before = original.read_bytes()
        (sessions / "corrupt-session.json").write_text('{"model": "m", "messages": null}', encoding="utf-8")
        catalog = subprocess.run([cli, "--list-sessions"], cwd=work, env=env,
                                 capture_output=True, text=True, check=True)
        assert "wheel-session" in catalog.stdout and "saved before the restart" in " ".join(catalog.stdout.split())
        assert "corrupt-session" not in catalog.stdout
        inspected = subprocess.run([cli, "--inspect-session", "wheel-session"], cwd=work, env=env,
                                   capture_output=True, text=True, check=True)
        assert "Session: wheel-session" in inspected.stdout and "Model: fixture-model" in inspected.stdout
        assert "Messages: 3" in inspected.stdout and "Retained turns: 2" in inspected.stdout
        assert prefix[0]["content"] not in inspected.stdout
        forked = subprocess.run([cli, "--fork-session", "wheel-session", "--before-turn", "2"], cwd=work, env=env,
                                 capture_output=True, text=True, check=True, timeout=20)
        assert forked.stdout.startswith("Forked session: ") and not forked.stderr
        fork_id = forked.stdout.strip().removeprefix("Forked session: ")
        assert fork_id != original.stem and original.read_bytes() == before
        child = json.loads((sessions / f"{fork_id}.json").read_text(encoding="utf-8"))
        assert child["id"] == fork_id and child["model"] == "fixture-model" and child["messages"] == prefix
        fork_inspected = subprocess.run([cli, "--inspect-session", fork_id], cwd=work, env=env,
                                        capture_output=True, text=True, check=True, timeout=20)
        assert "Messages: 2" in fork_inspected.stdout and "Retained turns: 1" in fork_inspected.stdout
        assert prefix[0]["content"] not in fork_inspected.stdout
        archives = {path.name: path.read_bytes() for path in sessions.iterdir()}
        compared = subprocess.run([cli, "--compare-sessions", original.stem, fork_id], cwd=work, env=env,
                                  capture_output=True, text=True, check=True, timeout=20)
        assert "Shared stored messages: 2" in compared.stdout
        assert "Remaining messages: 1 left, 0 right" in compared.stdout
        assert prefix[0]["content"] not in compared.stdout and not compared.stderr
        assert {path.name: path.read_bytes() for path in sessions.iterdir()} == archives
        version = subprocess.run([cli, "--version"], cwd=work, env=env, capture_output=True, text=True, check=True)
        assert version.stdout.strip() == f"corecoder {corecoder.__version__}"
        invalid = subprocess.run([cli, "--resume", "corrupt-session"], cwd=work, env=env,
                                 capture_output=True, text=True, check=False)
        assert invalid.returncode == 1 and "invalid session file" in invalid.stderr.lower(), invalid
        # The package is already imported from site-packages; this path is only for test fixtures.
        sys.path.insert(0, str(checkout))
        import pytest

        result = int(pytest.main([
            "-q", "--import-mode=importlib", str(checkout / "tests/test_mcp_resources.py"),
            str(checkout / "tests/test_session.py") + "::test_failed_save_preserves_the_last_complete_session",
            str(checkout / "tests/test_session.py") + "::test_successful_save_replaces_the_complete_session",
            "--basetemp=" + str(Path(work) / "pytest"), "-o", "cache_dir=" + str(Path(work) / "cache"),
        ]))
        loaded = {name: Path(module.__file__).resolve() for name, module in list(sys.modules.items())
                  if (name == "corecoder" or name.startswith("corecoder.")) and getattr(module, "__file__", None)}
        assert all(path.is_relative_to(package) for path in loaded.values()), loaded
    print(json.dumps({"version": corecoder.__version__, "wheelSha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                      "packageRoot": str(package), "matchingPythonFiles": len(sources),
                      "consoleExit": help_result.returncode, "sessionCatalogExit": catalog.returncode,
                      "sessionInspectExit": inspected.returncode, "invalidSessionExit": invalid.returncode,
                      "sessionForkExit": forked.returncode, "forkInspectExit": fork_inspected.returncode,
                      "sessionCompareExit": compared.returncode,
                      "resourceTestsExit": result}, indent=2))
    raise SystemExit(result)


if __name__ == "__main__":
    main()
