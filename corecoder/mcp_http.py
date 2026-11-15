"""MCP over Streamable HTTP; calls are never replayed automatically."""

import http.client
import json
import time
from urllib.parse import urlsplit

from .mcp import INIT_TIMEOUT, PROTOCOL_VERSION, MCPClient, MCPError, log

_RESPONSE_LIMIT = 4 * 1024 * 1024
_RESERVED_HEADERS = {"accept", "content-type", "mcp-session-id", "mcp-protocol-version"}


class _Response(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        self._socket = sock
        super().__init__(sock, *args, **kwargs)


class HTTPMCPClient(MCPClient):
    def __init__(self, name: str, url: str, headers: dict | None = None):
        if not isinstance(url, str):
            raise MCPError("MCP url must be a string")
        try:
            self._url = urlsplit(url)
            if (self._url.scheme not in {"http", "https"} or not self._url.hostname
                    or self._url.username is not None or self._url.fragment):
                raise ValueError
            _ = self._url.port  # the property validates the port range
        except ValueError as e:
            raise MCPError("MCP url must be an absolute http or https URL with a valid port") from e
        if headers is not None and (not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        )):
            raise MCPError("MCP headers must map strings to strings")
        self._headers = {k: v for k, v in (headers or {}).items() if k.lower() not in _RESERVED_HEADERS}
        super().__init__(name, "")

    def _start(self):
        with self._cond:
            self._generation = object()
            self._session_id = None
            self._protocol_version = None
            self._dead = None
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._cond:
            if self._dead is not None:
                raise self._dead
            generation = self._generation
            self._next_id += 1
            req_id = self._next_id
        reply = self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, timeout, generation)
        if "error" in reply:
            raise MCPError(f"MCP server {self.name!r} rejected {method}: {reply['error']}")
        result = reply.get("result", {})
        if method == "initialize":
            version = result.get("protocolVersion")
            if version not in {PROTOCOL_VERSION, "2025-03-26"}:
                raise MCPError("MCP server negotiated an unsupported protocol version")
            with self._cond:
                if generation is self._generation:
                    self._protocol_version = version
        return result

    def _notify(self, method: str):
        with self._cond:
            generation = self._generation
        self._post({"jsonrpc": "2.0", "method": method, "params": {}}, INIT_TIMEOUT, generation)

    def _http_headers(self):
        headers = {**self._headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _connection(self, timeout):
        connection = http.client.HTTPSConnection if self._url.scheme == "https" else http.client.HTTPConnection
        path = self._url.path or "/"
        if self._url.query:
            path += "?" + self._url.query
        conn = connection(self._url.hostname, self._url.port, timeout=timeout)
        conn.response_class = _Response
        return conn, path

    def _post(self, msg: dict, timeout: float, generation):
        with self._cond:
            if generation is not self._generation or self._dead is not None:
                raise MCPError(f"MCP server {self.name!r} is no longer connected")
            headers = self._http_headers()
        conn, path = self._connection(timeout)
        deadline = time.monotonic() + timeout
        try:
            conn.request("POST", path, body=json.dumps(msg).encode("utf-8"), headers=headers)
            with conn.getresponse() as response:
                if "id" not in msg and response.status == 202:
                    return {}
                if response.status != 200:
                    raise MCPError(f"MCP server {self.name!r}: HTTP {response.status}; use /mcp reconnect {self.name}")
                for reply in _messages(response, deadline):
                    if not isinstance(reply, dict):
                        raise MCPError("MCP HTTP response must be a JSON object")
                    if ("id" in msg and not isinstance(reply.get("id"), bool) and reply.get("id") == msg["id"]
                            and ("result" in reply or "error" in reply)):
                        if (reply.get("jsonrpc") != "2.0" or ("result" in reply) == ("error" in reply)
                                or not isinstance(reply.get("result", reply.get("error")), dict)):
                            raise MCPError("MCP HTTP response has an invalid JSON-RPC result or error")
                        with self._cond:
                            if generation is not self._generation:
                                raise MCPError("MCP session changed while the request was in flight")
                            if msg["method"] == "initialize":
                                session = response.getheader("Mcp-Session-Id")
                                if session is not None and (not session or not all("!" <= ch <= "~" for ch in session)):
                                    raise MCPError("MCP HTTP response has an invalid session ID")
                                self._session_id = session
                        return reply
                raise MCPError("MCP HTTP response ended without a matching reply")
        except (MCPError, OSError, ValueError, http.client.HTTPException) as e:
            failure = e if isinstance(e, MCPError) else MCPError(f"MCP HTTP transport failed ({type(e).__name__})")
            with self._cond:
                if generation is self._generation:
                    self._dead = failure
            if failure is e:
                raise
            raise failure from e
        finally:
            conn.close()

    def close(self):
        with self._cond:
            session, headers = self._session_id, self._http_headers()
            self._generation = object()
            self._session_id = None
            self._protocol_version = None
            self._dead = MCPError(f"MCP server {self.name!r} closed")
        if session is None:
            return
        conn, path = self._connection(INIT_TIMEOUT)
        try:
            conn.request("DELETE", path, headers=headers)
            with conn.getresponse():
                pass  # best effort; servers may refuse termination with 405
        except (OSError, ValueError, http.client.HTTPException):
            log.debug("MCP server %r session termination failed", self.name)
        finally:
            conn.close()


def _chunks(response, deadline):
    total = 0
    if response.length is not None and response.length > _RESPONSE_LIMIT:
        raise MCPError("MCP HTTP response exceeds the 4 MiB limit")
    while not response.isclosed():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError("MCP HTTP response timed out")
        response._socket.settimeout(remaining)
        chunk = response.read1(min(65536, _RESPONSE_LIMIT - total + 1))
        if not chunk:
            if response.length:
                raise MCPError("MCP HTTP response body was truncated")
            break
        total += len(chunk)
        if total > _RESPONSE_LIMIT:
            raise MCPError("MCP HTTP response exceeds the 4 MiB limit")
        yield chunk


def _lines(chunks):
    pending, previous_cr, first = b"", False, True
    for chunk in chunks:
        if previous_cr:
            chunk = chunk.removeprefix(b"\n")
        if not chunk:
            previous_cr = False
            continue
        chunk = pending + chunk
        previous_cr = chunk.endswith(b"\r")
        lines = chunk.splitlines(keepends=True)
        pending = lines.pop() if not lines[-1].endswith((b"\r", b"\n")) else b""
        for line in lines:
            if first:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first = False
            yield line.rstrip(b"\r\n")
    if pending:
        yield pending.removeprefix(b"\xef\xbb\xbf") if first else pending


def _messages(response, deadline):
    content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
    chunks = _chunks(response, deadline)
    if content_type == "application/json":
        yield json.loads(b"".join(chunks))
    elif content_type == "text/event-stream":
        data = []
        for line in _lines(chunks):
            if not line and data:
                yield json.loads(b"\n".join(data))
                data = []
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
    else:
        raise MCPError("MCP HTTP response has an unsupported Content-Type")
