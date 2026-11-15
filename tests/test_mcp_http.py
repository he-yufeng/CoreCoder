"""HTTP MCP uses the normal tool loader, consent gate and manual reconnect."""

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from corecoder import mcp
from corecoder.cli import _mcp
from corecoder.permissions import Permission


@pytest.fixture(params=["json", "sse"])
def server(request):
    calls, wire_headers = [], []
    settings = {"version": "2025-06-18", "session": "session-one", "mode": "normal"}
    response_type = request.param

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError):
                self.close_connection = True  # the client may finish before the SSE connection does

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((request, dict(self.headers)))
            wire_headers.append(self.headers)
            method = request["method"]
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            mode = settings["mode"] if method == "tools/call" else "normal"
            if method == "initialize":
                result = {"protocolVersion": settings["version"], "capabilities": {"tools": {}}}
            elif method == "tools/list":
                result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}]}
            if mode == "late_error":
                entered, release = settings["gate"]
                entered.set()
                release.wait(5)
            reply = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            if mode == "rpc_error":
                reply = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32602, "message": "bad text"}}
            elif mode == "wrong_id":
                reply["id"] += 100
            if "reply_override" in settings:
                reply.update(settings["reply_override"])
            body = json.dumps(reply, ensure_ascii=False).encode()
            if mode == "framed":
                ending = settings["ending"]
                body = b"\xef\xbb\xbf" + ending.join(
                    b"data: " + line for line in json.dumps(reply, ensure_ascii=False, indent=2).encode().split(b"\n")
                ) + ending * 2
            elif response_type == "sse":
                body = (b': keepalive\n\nevent: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                        + b"data: " + body + b"\n\n")
            if mode == "oversize":
                body = b" " * (4 * 1024 * 1024) + body
            if mode == "invalid_json":
                body = b"data: {\n\n" if response_type == "sse" else b"{"
            self.send_response(503 if mode == "late_error" else 404 if mode == "expired" else 302 if mode == "redirect" else 200)
            self.send_header("Content-Type", "application/json" if response_type == "json" else "text/event-stream")
            if mode == "redirect":
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/other")
            if mode == "drip":
                prefix = b": keepalive\n\n" if response_type == "sse" else b" "
                body = prefix * 20 + body
            if settings.get("chunked"):
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body) + (mode == "truncated")))
            if mode == "truncated":
                self.send_header("Connection", "close")
                self.close_connection = True
            if method == "initialize" and settings["session"] is not None:
                self.send_header("Mcp-Session-Id", settings["session"])
            self.end_headers()
            try:
                if mode == "drip":
                    for _ in range(20):
                        self.wfile.write(prefix)
                        self.wfile.flush()
                        time.sleep(0.03)
                    body = body[len(prefix) * 20:]
                if settings.get("chunked"):
                    size = 1 if mode == "framed" else 65536
                    for offset in range(0, len(body), size):
                        chunk = body[offset:offset + size]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_DELETE(self):
            calls.append(({"method": "DELETE"}, dict(self.headers)))
            self.send_response(settings.get("delete_status", 200))
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    existing = len(mcp._live_clients)
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/mcp", calls, wire_headers, settings
    finally:
        if "gate" in settings:
            settings["gate"][1].set()
        for client in mcp._live_clients[existing:]:
            client.close()
        del mcp._live_clients[existing:]
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def load(server, tmp_path, headers=None):
    url, _, _, _ = server
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": url, "headers": headers or {}}}}))
    return mcp.load_mcp_tools(path)


def test_url_server_registers_normal_mcp_tools(server, tmp_path):
    _, calls, _, _ = server
    tools = load(server, tmp_path, {"Authorization": "Bearer test-only"})

    assert [tool.name for tool in tools] == ["mcp__remote__echo"]
    assert isinstance(tools[0], mcp.MCPTool)
    assert tools[0].execute(text="你好") == "你好"
    assert [request["method"] for request, _ in calls] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"
    ]
    assert all(headers["Authorization"] == "Bearer test-only" for _, headers in calls)
    for _, headers in calls[1:]:
        assert headers["Mcp-Session-Id"] == "session-one"
        assert headers["MCP-Protocol-Version"] == "2025-06-18"
    assert Permission().check(tools[0].name, {}) is not None


def test_negotiated_protocol_and_stateless_server(server, tmp_path):
    _, calls, _, settings = server
    settings.update(version="2025-03-26", session=None)
    tool, = load(server, tmp_path)
    assert tool.execute(text="ok") == "ok"
    assert all("Mcp-Session-Id" not in headers for _, headers in calls)
    assert all(headers["MCP-Protocol-Version"] == "2025-03-26" for _, headers in calls[1:])


def test_protocol_headers_cannot_be_shadowed_by_custom_header_case(server, tmp_path):
    _, _, headers, _ = server
    tool, = load(server, tmp_path, {"accept": "text/plain", "mcp-session-id": "old", "mcp-protocol-version": "old"})
    assert tool.execute(text="ok") == "ok"
    assert headers[0].get_all("Mcp-Session-Id") is None
    for item in headers:
        assert item.get_all("Accept") == ["application/json, text/event-stream"]
    for item in headers[1:]:
        assert item.get_all("Mcp-Session-Id") == ["session-one"]
        assert item.get_all("MCP-Protocol-Version") == ["2025-06-18"]


@pytest.mark.parametrize("version", ["2099-01-01", None])
def test_unsupported_protocol_is_skipped(server, tmp_path, version, caplog):
    server[3]["version"] = version
    assert load(server, tmp_path) == []
    assert "protocol version" in caplog.text


@pytest.mark.parametrize("bad_url", ["http://[", "/mcp", "http://127.0.0.1:70000/mcp", None])
def test_bad_url_does_not_break_other_servers(server, tmp_path, bad_url):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"bad": {"url": bad_url}, "remote": {"url": server[0]}}}))
    assert [tool.name for tool in mcp.load_mcp_tools(path)] == ["mcp__remote__echo"]


@pytest.mark.parametrize("mode", ["rpc_error", "invalid_json", "wrong_id", "expired"])
def test_failed_calls_are_not_replayed(server, tmp_path, mode):
    _, calls, _, settings = server
    tool, = load(server, tmp_path)
    settings["mode"] = mode
    with pytest.raises(mcp.MCPError):
        tool.execute(text="once")
    assert [request["method"] for request, _ in calls].count("tools/call") == 1
    settings["mode"] = "normal"
    if mode == "rpc_error":
        assert tool._client.error is None
    else:
        assert tool._client.error is not None
        tool._client.reconnect()
    assert tool.execute(text="healthy") == "healthy"


def test_response_body_has_a_byte_limit(server, tmp_path):
    tool, = load(server, tmp_path)
    server[3]["mode"] = "oversize"
    with pytest.raises(mcp.MCPError, match="response.*limit"):
        tool.execute(text="large")
    assert tool._client.error is not None


def test_trickling_body_does_not_extend_call_budget(server, tmp_path):
    tool, = load(server, tmp_path, {"Connection": "close"})
    tool._client.call_timeout = 0.15
    server[3]["mode"] = "drip"
    started = time.monotonic()
    with pytest.raises(mcp.MCPError):
        tool.execute(text="slow")
    assert time.monotonic() - started < 1


def test_old_failure_cannot_poison_reconnected_client(server, tmp_path, capsys):
    _, calls, _, settings = server
    tool, = load(server, tmp_path)
    entered, release = threading.Event(), threading.Event()
    settings.update(mode="late_error", gate=(entered, release))
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(tool.execute, text="old")
        assert entered.wait(5)
        settings.update(mode="normal", session="session-two")
        _mcp("reconnect remote")
        release.set()
        with pytest.raises(mcp.MCPError, match="HTTP 503"):
            old.result(timeout=5)
    assert "Reconnected" in capsys.readouterr().out
    assert tool._client.error is None
    assert tool.execute(text="new") == "new"
    initializations = [headers for request, headers in calls if request["method"] == "initialize"]
    assert len(initializations) == 2
    assert all("Mcp-Session-Id" not in headers for headers in initializations)
    assert calls[-1][1]["Mcp-Session-Id"] == "session-two"


def test_parallel_calls_keep_their_own_replies(server, tmp_path):
    tool, = load(server, tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda value: tool.execute(text=value), ["one", "two", "three", "four"])) == [
            "one", "two", "three", "four"
        ]


@pytest.mark.parametrize("server", ["sse"], indirect=True)
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_sse_utf8_bom_multiline_and_split_boundaries(server, tmp_path, ending):
    tool, = load(server, tmp_path)
    server[3].update(mode="framed", ending=ending, chunked=True)
    text = "你好" + chr(0x2028) + "world"
    assert tool.execute(text=text) == text


@pytest.mark.parametrize("override", [{"result": None}, {"result": []}, {"jsonrpc": "1.0"}, {"id": True}])
def test_malformed_handshake_is_skipped(server, tmp_path, override):
    server[3]["reply_override"] = override
    assert load(server, tmp_path) == []
    assert [request["method"] for request, _ in server[1]] == ["initialize"]


@pytest.mark.parametrize("server", ["json"], indirect=True)
def test_truncated_json_body_is_not_a_success(server, tmp_path):
    tool, = load(server, tmp_path)
    server[3]["mode"] = "truncated"
    with pytest.raises(mcp.MCPError, match="truncated"):
        tool.execute(text="partial")


def test_chunked_response_is_subject_to_same_byte_limit(server, tmp_path):
    tool, = load(server, tmp_path)
    server[3].update(mode="oversize", chunked=True)
    with pytest.raises(mcp.MCPError, match="response.*limit"):
        tool.execute(text="large")


def test_redirect_does_not_forward_credentials_or_repeat_request(server, tmp_path):
    tool, = load(server, tmp_path, {"Authorization": "Bearer test-only"})
    server[3]["mode"] = "redirect"
    with pytest.raises(mcp.MCPError, match="HTTP 302"):
        tool.execute(text="once")
    assert sum(request["method"] == "tools/call" for request, _ in server[1]) == 1


@pytest.mark.parametrize("kind", ["scheme", "userinfo", "fragment"])
def test_unsupported_endpoint_is_skipped(server, tmp_path, kind):
    url = server[0]
    url = url.replace("http:", "ftp:") if kind == "scheme" else (
        url.replace("http://", "http://user:password@") if kind == "userinfo" else url + "#fragment"
    )
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"bad": {"url": url}}}))
    assert mcp.load_mcp_tools(path) == []


@pytest.mark.parametrize("session", ["", "has space"])
def test_invalid_session_id_is_not_forwarded(server, tmp_path, session):
    server[3]["session"] = session
    assert load(server, tmp_path) == []
    assert [request["method"] for request, _ in server[1]] == ["initialize"]


@pytest.mark.parametrize("status", [200, 405])
def test_close_attempts_session_termination_once(server, tmp_path, status):
    tool, = load(server, tmp_path, {"Authorization": "Bearer test-only"})
    server[3]["delete_status"] = status
    tool._client.close()
    tool._client.close()
    deletes = [headers for request, headers in server[1] if request["method"] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0]["Mcp-Session-Id"] == "session-one"
    assert deletes[0]["MCP-Protocol-Version"] == "2025-06-18"
    assert deletes[0]["Authorization"] == "Bearer test-only"
    assert tool._client.error is not None


def test_stdio_and_http_servers_share_the_config_loader(server, tmp_path):
    script = '''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        result = {}
    elif msg["method"] == "tools/list":
        result = {"tools": [{"name": "echo"}]}
    else:
        result = {"content": [{"type": "text", "text": msg["params"]["arguments"]["text"]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
'''
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {
        "local": {"command": sys.executable, "args": ["-u", "-c", script]},
        "remote": {"url": server[0]},
    }}))
    tools = {tool.name: tool for tool in mcp.load_mcp_tools(path)}
    assert set(tools) == {"mcp__local__echo", "mcp__remote__echo"}
    assert tools["mcp__local__echo"].execute(text="local") == "local"
    assert tools["mcp__remote__echo"].execute(text="remote") == "remote"
