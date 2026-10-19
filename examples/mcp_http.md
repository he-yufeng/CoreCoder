# MCP example: a remote server over Streamable HTTP

For an existing MCP Streamable HTTP endpoint, add a URL entry to
`~/.corecoder/mcp.json`. It can live alongside your existing stdio servers:

```json
{
  "mcpServers": {
    "remote": {
      "url": "https://your-server.example/mcp",
      "headers": {"Authorization": "Bearer YOUR_TOKEN"}
    }
  }
}
```

Replace the endpoint and token with your server's values. Omit `headers` when
the endpoint does not need authentication. A URL entry connects to an already
running server; it does not launch a subprocess. No new dependency is required.

At startup, CoreCoder initializes the connection and lists the remote tools.
They appear as `mcp__remote__<tool>` and keep the same consent prompts, hooks
and plan-mode restrictions as stdio tools. `/mcp` shows their connection state.

## Sessions and failures

The client accepts JSON and SSE responses to POST requests, including
multi-line SSE data and interleaved server notifications. It carries the
server's session ID and negotiated protocol version on subsequent requests.
The supported versions are `2025-06-18` and `2025-03-26`.

If the session expires (HTTP 404) or the connection fails, the tool call
reports an error. CoreCoder never retries a tool call automatically: a lost
response does not prove that the server did not execute it. Use
`/mcp reconnect remote` to initialize a fresh session, then decide whether to
run the tool again. Existing tool wrappers continue to use the reconnected
client. Shutdown sends DELETE for a stateful session; servers that do not
support DELETE may respond with 405.

Redirects are not followed, so configured headers are not forwarded to another
endpoint. Configure the final MCP URL directly. Protocol headers are managed
by the client, independently of custom headers.

## Deliberate scope

This is the tools-only Streamable HTTP transport, following the
[MCP transport specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
It does not implement the legacy 2024 HTTP+SSE transport, OAuth discovery,
resources, prompts, unsolicited GET streams, or automatic stream resumption.
Static authentication headers are supported.

## Verify without credentials

From a development checkout:

```bash
python -m pytest tests/test_mcp_http.py tests/test_mcp.py -q
```

The HTTP tests start a real server bound only to `127.0.0.1`. They exercise
JSON and SSE tool calls, concurrent requests, protocol/session headers,
timeouts, redirect refusal, session expiration, explicit reconnect, and
shutdown. They use a scripted LLM and synthetic tool data, so no provider key
or external MCP service is needed.
