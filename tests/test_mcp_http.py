"""Exercise the HTTP transport against a real loopback server, without an LLM."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from corecoder import mcp
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPError, load_mcp_tools
from corecoder.mcp_http import HTTPMCPClient
from corecoder.permissions import Permission


@pytest.fixture
def endpoint():
    state = SimpleNamespace(requests=[], sessions=0, sse=False, stateful=True, mode="ok",
                            delete_status=200, release=threading.Event(), redirects=0,
                            protocol="2025-03-26", notify_status=202)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send(self, value, status=200, session=None, content_type="application/json"):
            body = value.encode("utf-8") if isinstance(value, str) else json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self):
            state.requests.append(("DELETE", dict(self.headers), {}))
            self.send({}, status=state.delete_status)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append((request["method"], dict(self.headers), request))
            if self.path == "/redirected":
                state.redirects += 1
            method = request["method"]
            session = None
            if method == "initialize":
                state.sessions += 1
                session = f"session-{state.sessions}" if state.stateful else None
                result = {"protocolVersion": state.protocol, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "remote", "version": "1"}}
            elif method == "notifications/initialized":
                self.send("", status=state.notify_status)
                return
            elif method == "tools/list":
                result = {"tools": [{"name": "echo", "description": "Echo remotely",
                                     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
            else:
                if state.mode == "redirect":
                    self.send_response(301)
                    self.send_header("Location", "/redirected")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if state.mode in {"expired", "server-error"}:
                    self.send({}, status=404 if state.mode == "expired" else 500)
                    return
                if state.mode == "timeout":
                    state.release.wait(3)
                    return
                if state.mode == "heartbeats":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        for _ in range(20):
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            if state.release.wait(0.01):
                                break
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    self.close_connection = True
                    return
                if state.mode == "bad-json":
                    self.send("not json")
                    return
                if state.mode == "bad-type":
                    self.send("hello", content_type="text/plain")
                    return
                if state.mode == "not-object":
                    self.send([])
                    return
                if state.mode == "rpc-error":
                    self.send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32602, "message": "bad arguments"}})
                    return
                result = {"content": [{"type": "text", "text": "echo: " + request["params"]["arguments"].get("text", "")}],
                          "isError": state.mode == "tool-error"}
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            if state.mode == "wrong-id" and method == "tools/call":
                reply["id"] += 100
            if not state.sse:
                self.send(reply, session=session)
                return
            # Keep the connection open after the reply: a client must return as
            # soon as it gets its response rather than waiting for EOF.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            data = ": keepalive\r\nevent: message\r\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/message\"}\r\n\r\n"
            data += "\r\n".join("data: " + line for line in json.dumps(reply, indent=2, ensure_ascii=False).splitlines()) + "\r\n\r\n"
            self.wfile.write(data.encode("utf-8"))
            self.wfile.flush()
            state.release.wait(3)
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", state
    finally:
        state.release.set()
        for client in mcp._live_clients:
            client.close()
        mcp._live_clients.clear()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def configured_tools(tmp_path, endpoint):
    url, _ = endpoint
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": url, "headers": {"X-Client-Key": "example"}}}}))
    return load_mcp_tools(path)


def test_http_handshake_and_agent_tool_round_trip(tmp_path, endpoint):
    tools = configured_tools(tmp_path, endpoint)
    assert [tool.name for tool in tools] == ["mcp__remote__echo"]
    assert tools[0].description == "Echo remotely"
    agent = Agent(llm=ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(id="call-1", name=tools[0].name, arguments={"text": "hello"})]),
        LLMResponse(content="done"),
    ]), tools=tools)
    assert agent.chat("go") == "done"
    assert agent.messages[2]["content"] == "echo: hello"
    _, state = endpoint
    assert [method for method, _, _ in state.requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    for method, headers, request in state.requests:
        assert headers["X-Client-Key"] == "example"
        assert headers["Accept"] == "application/json, text/event-stream"
        assert request["jsonrpc"] == "2.0"
        if method != "initialize":
            assert headers["Mcp-Session-Id"] == "session-1"
            assert headers["Mcp-Protocol-Version"] == "2025-03-26"
    assert Permission().check(tools[0].name, {}) is not None


def test_sse_multiline_unicode_and_notifications_without_waiting_for_eof(tmp_path, endpoint):
    endpoint[1].sse = True
    with ThreadPoolExecutor() as executor:
        tools = executor.submit(configured_tools, tmp_path, endpoint).result(timeout=2)
        result = executor.submit(tools[0].execute, text="merhaba 世界").result(timeout=2)
    assert result == "echo: merhaba 世界"


def test_parallel_http_calls_keep_their_responses(tmp_path, endpoint):
    tool = configured_tools(tmp_path, endpoint)[0]
    with ThreadPoolExecutor() as executor:
        results = list(executor.map(lambda text: tool.execute(text=text), ["one", "two", "three"]))
    assert results == ["echo: one", "echo: two", "echo: three"]


@pytest.mark.parametrize("mode, message", [
    ("rpc-error", "bad arguments"), ("tool-error", "echo:"),
    ("bad-json", "transport failed"), ("bad-type", "Content-Type"),
    ("not-object", "JSON object"), ("wrong-id", "matching reply"), ("server-error", "HTTP 500"),
])
def test_http_failures_are_mcp_errors(tmp_path, endpoint, mode, message):
    tool = configured_tools(tmp_path, endpoint)[0]
    endpoint[1].mode = mode
    with pytest.raises(MCPError, match=message):
        tool.execute(text="hello")


def test_expired_session_never_replays_a_tool_and_explicit_reconnect_recovers(tmp_path, endpoint):
    tool = configured_tools(tmp_path, endpoint)[0]
    state = endpoint[1]
    state.mode = "expired"
    with pytest.raises(MCPError, match="HTTP 404"):
        tool.execute(text="one")
    with pytest.raises(MCPError, match="HTTP 404"):
        tool.execute(text="two")
    assert sum(method == "tools/call" for method, _, _ in state.requests) == 1
    assert state.sessions == 1
    state.mode = "ok"
    tool._client.reconnect()
    assert state.sessions == 2
    assert tool.execute(text="again") == "echo: again"
    assert state.requests[-1][1]["Mcp-Session-Id"] == "session-2"


def test_http_call_read_timeout(tmp_path, endpoint):
    tool = configured_tools(tmp_path, endpoint)[0]
    endpoint[1].mode = "timeout"
    tool._client.call_timeout = 0.05
    with pytest.raises(MCPError, match="transport failed"):
        tool.execute(text="hello")
    assert sum(method == "tools/call" for method, _, _ in endpoint[1].requests) == 1


def test_sse_keepalives_cannot_extend_the_call_budget(tmp_path, endpoint):
    tool = configured_tools(tmp_path, endpoint)[0]
    endpoint[1].mode = "heartbeats"
    tool._client.call_timeout = 0.05
    with pytest.raises(MCPError, match="timed out"):
        tool.execute(text="hello")


@pytest.mark.parametrize("status", [200, 405])
def test_http_close_terminates_session_once(tmp_path, endpoint, status):
    tool = configured_tools(tmp_path, endpoint)[0]
    endpoint[1].delete_status = status
    tool._client.close()
    tool._client.close()
    deletes = [headers for method, headers, _ in endpoint[1].requests if method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0]["Mcp-Session-Id"] == "session-1"
    with pytest.raises(MCPError, match="closed"):
        tool.execute(text="hello")


def test_stateless_server_needs_no_session_header_or_delete(tmp_path, endpoint):
    endpoint[1].stateful = False
    tool = configured_tools(tmp_path, endpoint)[0]
    assert tool.execute(text="hello") == "echo: hello"
    tool._client.close()
    assert all("Mcp-Session-Id" not in headers for _, headers, _ in endpoint[1].requests)
    assert all(method != "DELETE" for method, _, _ in endpoint[1].requests)


def test_http_redirect_is_not_followed_with_configured_headers(tmp_path, endpoint):
    tool = configured_tools(tmp_path, endpoint)[0]
    endpoint[1].mode = "redirect"
    with pytest.raises(MCPError, match="HTTP 301"):
        tool.execute(text="hello")
    assert endpoint[1].redirects == 0


@pytest.mark.parametrize("url, headers", [("file:///tmp/socket", None), ("http://", None),
                                          ("https://example.test/mcp", "bad"),
                                          ("https://example.test/mcp", {"X-Token": 1})])
def test_invalid_http_config_is_skipped_with_a_warning(tmp_path, caplog, url, headers):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": url, "headers": headers}}}))
    with caplog.at_level(logging.WARNING):
        assert load_mcp_tools(path) == []
    assert len(caplog.records) == 1
    assert "remote" in caplog.text


def test_unreachable_http_server_reports_transport_error():
    # Port zero is reserved and cannot have a listener.
    with pytest.raises(MCPError, match="transport failed"):
        HTTPMCPClient("offline", "http://127.0.0.1:0/mcp")


@pytest.mark.parametrize("failure", ["version", "notification"])
def test_failed_initialization_terminates_its_partial_session(tmp_path, endpoint, failure):
    state = endpoint[1]
    if failure == "version":
        state.protocol = "2099-01-01"
    else:
        state.notify_status = 400
    assert configured_tools(tmp_path, endpoint) == []
    assert mcp._live_clients == []
    assert state.requests[-1][0] == "DELETE"
    assert state.requests[-1][1]["Mcp-Session-Id"] == "session-1"
