"""Shared pytest fixtures and helpers."""

from corecoder.tools import ALL_TOOLS


def get_tool(name: str):
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None
