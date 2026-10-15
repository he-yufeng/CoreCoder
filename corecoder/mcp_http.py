"""The tools-only MCP client over Streamable HTTP, without automatic call replay."""

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .mcp import INIT_TIMEOUT, PROTOCOL_VERSION, MCPClient, MCPError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward configured credentials to a redirect target.


class HTTPMCPClient(MCPClient):
    """POST JSON-RPC requests, accepting either JSON or SSE responses.

    Reuse the stdio client's tool wrappers and explicit reconnect. A lost
    response is never retried: a remote tool may already have had side effects.
    """

    def __init__(self, name: str, url: str, headers: dict | None = None):
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
            raise MCPError("MCP url must use http or https")
        if headers is not None and (not isinstance(headers, dict) or
                                    not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())):
            raise MCPError("MCP headers must map strings to strings")
        self._url = url
        self._headers = dict(headers or {})
        self._opener = build_opener(_NoRedirect)
        super().__init__(name, "")

    def _start(self):
        self._session_id = None
        self._protocol_version = None
        self._dead = None
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _http_headers(self):
        headers = {**self._headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    def _post(self, msg: dict, timeout: float):
        request = Request(self._url, data=json.dumps(msg).encode("utf-8"), headers=self._http_headers(), method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                if "id" not in msg and response.status == 202:
                    return {}
                if msg["method"] == "initialize":
                    self._session_id = response.headers.get("Mcp-Session-Id")
                deadline = time.monotonic() + timeout
                for reply in _messages(response, deadline):
                    if time.monotonic() > deadline:
                        raise MCPError("MCP HTTP response timed out")
                    if not isinstance(reply, dict):
                        raise MCPError("MCP HTTP response must be a JSON object")
                    if reply.get("id") == msg.get("id") and "id" in msg and ("result" in reply or "error" in reply):
                        if "error" in reply:
                            raise MCPError(f"MCP server {self.name!r} rejected {msg['method']}: {reply['error']}")
                        return reply.get("result", {})
                raise MCPError("MCP HTTP response ended without a matching reply")
        except HTTPError as e:
            self._dead = MCPError(f"MCP server {self.name!r}: HTTP {e.code}; use /mcp reconnect {self.name}")
            e.close()
            raise self._dead from e
        except (URLError, OSError, ValueError) as e:
            self._dead = MCPError(f"MCP server {self.name!r}: HTTP transport failed ({type(e).__name__})")
            raise self._dead from e

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        with self._cond:
            if self._dead is not None:
                raise self._dead
            self._next_id += 1
            req_id = self._next_id
        result = self._post({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}, timeout)
        if method == "initialize" and result.get("protocolVersion") not in {PROTOCOL_VERSION, "2025-03-26"}:
            raise MCPError("MCP server negotiated an unsupported protocol version")
        return result

    def _notify(self, method: str):
        self._post({"jsonrpc": "2.0", "method": method, "params": {}}, INIT_TIMEOUT)

    def close(self):
        session, self._session_id = self._session_id, None
        self._dead = MCPError(f"MCP server {self.name!r} closed")
        if session:
            headers = {**self._http_headers(), "Mcp-Session-Id": session}
            try:
                with self._opener.open(Request(self._url, headers=headers, method="DELETE"), timeout=INIT_TIMEOUT):
                    pass
            except (URLError, OSError):
                pass  # Termination is best effort; a server may return 405.


def _messages(response, deadline):
    if response.headers.get_content_type() == "application/json":
        yield json.load(response)
    elif response.headers.get_content_type() == "text/event-stream":
        data = []
        for raw in response:
            if time.monotonic() > deadline:
                raise MCPError("MCP HTTP response timed out")
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line and data:
                yield json.loads("\n".join(data))
                data = []
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
    else:
        raise MCPError("MCP HTTP response has an unsupported Content-Type")
