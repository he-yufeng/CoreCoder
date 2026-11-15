"""Run after installing the built wheel, not an editable checkout."""

import hashlib
import json
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
        # The package is already imported from site-packages; this path is only for test fixtures.
        sys.path.insert(0, str(checkout))
        import pytest

        result = int(pytest.main([
            "-q", "--import-mode=importlib", str(checkout / "tests/test_mcp_resources.py"),
            "--basetemp=" + str(Path(work) / "pytest"), "-o", "cache_dir=" + str(Path(work) / "cache"),
        ]))
        loaded = {name: Path(module.__file__).resolve() for name, module in list(sys.modules.items())
                  if (name == "corecoder" or name.startswith("corecoder.")) and getattr(module, "__file__", None)}
        assert all(path.is_relative_to(package) for path in loaded.values()), loaded
    print(json.dumps({"version": corecoder.__version__, "wheelSha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                      "packageRoot": str(package), "matchingPythonFiles": len(sources),
                      "consoleExit": help_result.returncode, "resourceTestsExit": result}, indent=2))
    raise SystemExit(result)


if __name__ == "__main__":
    main()
