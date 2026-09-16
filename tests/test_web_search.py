"""Tests for the optional You.com web search tool."""

import json
import urllib.error
from email.message import Message

from corecoder.tools.web_search import MAX_RESULTS, YouWebSearchTool, web_search_tools


def test_tool_absent_without_key(monkeypatch):
    monkeypatch.delenv("YDC_API_KEY", raising=False)
    assert web_search_tools() == []


def test_tool_present_with_key(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "test-key")
    tools = web_search_tools()
    assert len(tools) == 1
    assert tools[0].name == "you_web_search"


def test_schema_shape(monkeypatch):
    monkeypatch.setenv("YDC_API_KEY", "test-key")
    s = web_search_tools()[0].schema()
    assert s["type"] == "function"
    assert s["function"]["name"] == "you_web_search"
    assert s["function"]["parameters"]["required"] == ["query"]


def _fake_response(data):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(data).encode()

    return Resp()


def test_execute_formats_results(monkeypatch):
    tool = YouWebSearchTool("test-key")
    monkeypatch.setattr(
        "corecoder.tools.web_search.urllib.request.urlopen",
        lambda req, timeout: _fake_response(
            {"results": [{"title": "FastAPI docs", "url": "https://fastapi.tiangolo.com", "snippets": ["websockets guide"]}]}
        ),
    )
    r = tool.execute(query="fastapi websockets")
    assert "FastAPI docs" in r
    assert "https://fastapi.tiangolo.com" in r
    assert "websockets guide" in r


def test_execute_empty_results(monkeypatch):
    tool = YouWebSearchTool("test-key")
    monkeypatch.setattr(
        "corecoder.tools.web_search.urllib.request.urlopen",
        lambda req, timeout: _fake_response({"results": []}),
    )
    assert tool.execute(query="nothing") == "No results found."


def test_execute_http_error_suggests_key(monkeypatch):
    tool = YouWebSearchTool("test-key")

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", Message(), None)

    monkeypatch.setattr("corecoder.tools.web_search.urllib.request.urlopen", boom)
    r = tool.execute(query="x")
    assert "HTTP 401" in r
    assert "YDC_API_KEY" in r


def test_execute_network_error_returns_error_string(monkeypatch):
    tool = YouWebSearchTool("test-key")

    def boom(req, timeout):
        raise OSError("no network")

    monkeypatch.setattr("corecoder.tools.web_search.urllib.request.urlopen", boom)
    assert tool.execute(query="x").startswith("Error: ")


def test_count_is_clamped(monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen["url"] = req.full_url
        return _fake_response({"results": [{"title": "t", "url": "u", "snippets": ["s"]}] * 50})

    monkeypatch.setattr("corecoder.tools.web_search.urllib.request.urlopen", fake)
    tool = YouWebSearchTool("test-key")
    tool.execute(query="q", count=999)
    assert f"numResults={MAX_RESULTS}" in seen["url"]
    tool.execute(query="q", count=-3)
    assert "numResults=1" in seen["url"]


def test_not_read_only_for_consent_gate(monkeypatch):
    """Like MCP tools, the search tool sits behind the consent gate."""
    from corecoder.permissions import Permission

    monkeypatch.setenv("YDC_API_KEY", "test-key")
    assert "you_web_search" not in Permission.READ_ONLY
