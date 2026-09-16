"""Optional web search via the You.com Search API.

The tool registers only when YDC_API_KEY is in the environment — the same
opt-in shape as load_mcp_tools(): no key means no tool, and the default
eight are untouched for everyone else. Like MCP tools, it stays out of the
read-only set, so the consent gate asks before the first call (a query
leaves the machine, which is worth a yes).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import ClassVar

from .base import Tool

API_URL = "https://api.you.com/api/search"
TIMEOUT = 15  # seconds for one search call
MAX_RESULTS = 10


class YouWebSearchTool(Tool):
    """Search the web with You.com. Present only when YDC_API_KEY is set."""

    name = "you_web_search"
    description = (
        "Search the web for current information: docs, releases, changelogs, error "
        "messages — anything not already in this repo. Returns results with title, "
        "URL and snippet. Requires YDC_API_KEY; if a call fails with a key error, "
        "tell the user how to get one at https://you.com/platform/api-keys."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query; a few keywords beats a full sentence",
            },
            "count": {
                "type": "integer",
                "description": f"Number of results to return (default 5, max {MAX_RESULTS})",
            },
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str):
        self.api_key = api_key

    def execute(self, query: str, count: int = 5) -> str:
        count = max(1, min(int(count), MAX_RESULTS))
        req = urllib.request.Request(
            f"{API_URL}?{urllib.parse.urlencode({'q': query, 'numResults': count})}",
            headers={"X-API-Key": self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            hint = " — check YDC_API_KEY" if e.code in (401, 403) else ""
            return f"Error: You.com search failed with HTTP {e.code}{hint}"
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            # boundary: the agent gets an error string, not a traceback
            return f"Error: You.com search unavailable: {e}"

        lines = []
        for i, r in enumerate((data.get("results") or [])[:count], 1):
            title = r.get("title") or "(untitled)"
            url = r.get("url") or r.get("link") or ""
            snippet = r.get("description") or " ".join(r.get("snippets") or [])
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}".rstrip())
        return "\n".join(lines) if lines else "No results found."


def web_search_tools() -> list[Tool]:
    """Return the You.com search tool when YDC_API_KEY is set, else []."""
    key = os.environ.get("YDC_API_KEY", "").strip()
    return [YouWebSearchTool(key)] if key else []
