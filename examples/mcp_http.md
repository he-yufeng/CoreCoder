# Connect a remote MCP tool server

Use the server's **Streamable HTTP endpoint**, usually a path such as `/mcp`. A legacy SSE subscription URL is not interchangeable with it. CoreCoder supports the tools slice of MCP 2025-06-18 and 2025-03-26, without adding an SDK dependency.

Put this in `~/.corecoder/mcp.json`, replacing the endpoint and token with your own:

```json
{
  "mcpServers": {
    "remote": {
      "url": "https://your-mcp-server.example/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

`headers` is optional for servers that do not require authentication. Values are literal strings: `${TOKEN}` is not expanded. Keep this local config out of version control. Use HTTPS outside a trusted local network; URL credentials, fragments and redirects are not supported. Connections are direct, without proxy discovery or an OAuth login flow.

Start CoreCoder normally. It initializes the connection and registers the server's tools as `mcp__remote__<tool>`. Those tools go through the ordinary consent gate, plan mode and hooks. A URL config can coexist with local `command` entries in the same `mcpServers` object.

In the REPL:

```text
/mcp
/mcp reconnect remote
```

The first command lists server health. The second starts a fresh session, retaining the tool objects already registered with the agent. A timed-out or failed call is never replayed automatically, even after a session expires: a lost response does not prove that the tool did nothing. Check the remote operation before asking for it again.

JSON and SSE responses are accepted, with session and negotiated protocol headers managed by the client. Each response has a 4 MiB byte limit, including SSE progress events. Startup requests use fifteen-second timeouts; tool calls use sixty seconds, with the remaining budget checked between body reads. This is not a hard deadline around OS DNS lookup or status/header parsing. Closing or reconnecting attempts a session `DELETE`; refusal or network failure does not prevent local cleanup.

For a local protocol-only check without a model or API key:

```bash
python -m pytest tests/test_mcp_http.py -q
```

The tests run loopback HTTP servers and exercise the real config loader, JSON/SSE parsing, concurrent calls, session headers and reconnect. They do not contact the placeholder endpoint above or claim a live model or external-service check.
