from pathlib import Path


def resolve_workspace_path(file_path: str, workspace: str | None = None) -> Path:
    root = Path(workspace or ".").resolve()
    target = Path(file_path).expanduser().resolve()

    try:
        target.relative_to(root)
    except ValueError:
        raise PermissionError(
            f"Path outside workspace is blocked: {target}. "
            f"Allowed workspace: {root}"
        )

    return target




