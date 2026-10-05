# Web research through a stdio MCP bridge

CoreCoder's MCP client speaks stdio. This example uses
[mcp-remote](https://github.com/geelen/mcp-remote) to bridge that existing
transport to the Streamable HTTP endpoint of
[Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp).
It adds `web_search` and `web_fetch` tools for looking up documentation and
reading source pages. The endpoint is free for light use, with anonymous rate
limits and no Parallel API key or sign-in required.

## Setup

Install CoreCoder as described in the README, and install Node.js 22 or newer
with `npx` on your PATH. Cache the pinned bridge before launching CoreCoder:

```bash
npm exec --yes --package=mcp-remote@0.14.3 -- node -e 'console.log("bridge cached")'
```

The first download can exceed CoreCoder's 15-second MCP startup timeout.
The example pins the tested bridge version and uses HTTP only, without an SSE
fallback. It sends `User-Agent: corecoder/0.7.0` on the bridge's HTTP requests.
`--enable-proxy` honors existing `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY`
settings when your network requires a proxy.

If you have no `~/.corecoder/mcp.json` yet, run from this checkout:

```bash
mkdir -p ~/.corecoder
cp examples/parallel-search-mcp.json ~/.corecoder/mcp.json
```

If that file already exists, add only the `parallel-search` entry from
[parallel-search-mcp.json](parallel-search-mcp.json) inside its `mcpServers`
object, keeping your other servers. Restart CoreCoder after editing the file.
This is a user configuration; CoreCoder does not load examples automatically.
Remove that entry and restart to disable it.

## Use

Keep your usual model configuration. A model connection is still needed;
the keyless search endpoint does not supply the model itself. In the REPL:

```bash
corecoder
```

Ask: "Use Parallel search to find Python's asyncio TaskGroup documentation,
then fetch the official page and explain how it handles a failing child task.
Include the source URL."

The tools are registered as `mcp__parallel-search__web_search` and
`mcp__parallel-search__web_fetch`. CoreCoder asks for consent before MCP calls,
and plan mode refuses them, just like other MCP tools. For a one-shot script,
`--yes` approves **every** tool call, including shell and file changes:

```bash
corecoder --yes -p "Use Parallel search to find Python's asyncio TaskGroup documentation, fetch the official page, and explain how it handles a failing child task. Include the source URL."
```

Search queries and fetched URLs go to Parallel. Search results become tool
observations in the model conversation. Existing hooks still apply, and calls
keep CoreCoder's 60-second timeout and ordinary tool-error handling. Network
failures or anonymous rate limits can return an error to the agent. No model
provider settings, existing MCP entries, or defaults need to change.
