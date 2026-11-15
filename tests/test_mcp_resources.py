"""Resource discovery and reads exercise the real transports and agent dispatch."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from corecoder import mcp
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.hooks import Hooks
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPError, load_mcp_tools
from corecoder.permissions import Permission
from tests.mcp_resource_fixture import ResourceServer


@pytest.fixture(params=["stdio", "http"])
def resource_config(request, tmp_path):
    servers = []

    def make(mode="mixed"):
        if request.param == "stdio":
            script = Path(__file__).with_name("mcp_resource_fixture.py")
            spec = {"command": sys.executable, "args": ["-I", "-u", str(script), mode]}
        else:
            dispatcher = ResourceServer(mode)

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_POST(self):
                    message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    response = dispatcher.respond(message)
                    if response is None:
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    data = json.dumps(response).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    if message["method"] == "initialize":
                        self.send_header("Mcp-Session-Id", "resource-test-session")
                    self.end_headers()
                    self.wfile.write(data)

                def do_DELETE(self):
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append((server, thread))
            spec = {"url": f"http://127.0.0.1:{server.server_port}/mcp"}
        config = tmp_path / f"{mode}.json"
        config.write_text(json.dumps({"mcpServers": {"refs": spec}}), encoding="utf-8")
        return config

    yield make
    for client in mcp._live_clients:
        client.close()
    mcp._live_clients.clear()
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _resource_tool(config, name="mcp__refs__resources"):
    tools = load_mcp_tools(config)
    selected = [tool for tool in tools if tool.name == name]
    assert len(selected) == 1, f"resource bridge missing from {[tool.name for tool in tools]}"
    return selected[0]


def test_a_resource_only_server_does_not_need_tools_list(resource_config):
    tools = load_mcp_tools(resource_config("resources-only"))

    assert [tool.name for tool in tools] == ["mcp__refs__resources"]
    result = json.loads(tools[0].execute(action="list"))
    assert result["resources"][0]["uri"] == "docs://guide"
    assert result["_meta"]["method"] == "resources/list"


def test_invalid_resource_capability_is_not_treated_as_support(resource_config, caplog):
    tools = load_mcp_tools(resource_config("invalid-resources"))

    assert tools == []
    assert "resources capability must be an object" in caplog.text


def test_resource_and_template_pagination_preserves_opaque_cursors(resource_config):
    tool = _resource_tool(resource_config())
    first = json.loads(tool.execute(action="list"))
    second = json.loads(tool.execute(action="list", cursor=first["nextCursor"]))
    empty = json.loads(tool.execute(action="list", cursor=""))
    templates = json.loads(tool.execute(action="templates"))
    later = json.loads(tool.execute(action="templates", cursor=templates["nextCursor"]))

    assert second["resources"][0]["uri"] == "docs://more"
    assert second["_meta"]["params"] == {"cursor": " page:2 "}
    assert "nextCursor" not in second
    assert empty["_meta"]["params"] == {"cursor": ""}
    assert templates["resourceTemplates"][0]["uriTemplate"] == "docs://guide/{chapter}"
    assert later["_meta"] == {"method": "resources/templates/list", "params": {"cursor": " template:2 "}}


@pytest.mark.parametrize("uri", ["docs://guide", "file:///not-a-client-file", "https://must-not-fetch.invalid/reference"])
def test_read_passes_the_uri_to_the_server_without_dereferencing_it(resource_config, uri):
    tool = _resource_tool(resource_config())

    result = json.loads(tool.execute(action="read", uri=uri))

    assert result["_meta"] == {"method": "resources/read", "params": {"uri": uri}}
    assert result["contents"][0] == {"uri": uri, "mimeType": "text/plain", "text": "server-owned: " + uri}
    assert result["contents"][1]["text"] == "项目上下文"


def test_the_agent_receives_resources_as_normal_tool_results(resource_config):
    tool = _resource_tool(resource_config())
    agent = Agent(llm=ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(id="refs", name=tool.name, arguments={"action": "list"})]),
        LLMResponse(tool_calls=[ToolCall(id="read", name=tool.name, arguments={"action": "read", "uri": "docs://guide"})]),
        LLMResponse(content="Reference loaded"),
    ]), tools=[tool], permission=Permission(allow_all=True))

    assert agent.chat("Read the project guide") == "Reference loaded"
    results = [json.loads(message["content"]) for message in agent.messages if message["role"] == "tool"]
    assert results[0]["resources"][0]["uri"] == "docs://guide"
    assert results[1]["contents"][0]["text"] == "server-owned: docs://guide"


@pytest.mark.parametrize("boundary", ["permission", "plan", "hook"])
def test_resource_reads_keep_the_existing_consent_plan_and_hook_boundaries(resource_config, boundary):
    tool = _resource_tool(resource_config())
    permission = Permission() if boundary == "permission" else Permission(allow_all=True)
    command = f'"{sys.executable}" -I -c "import sys; sys.stderr.write(\'resources blocked\'); sys.exit(2)"'
    hooks = Hooks(pre=[{"matcher": tool.name, "command": command}], post=[]) if boundary == "hook" else None
    agent = Agent(llm=ScriptedLLM([
        LLMResponse(tool_calls=[ToolCall(id="read", name=tool.name, arguments={"action": "read", "uri": "docs://guide"})]),
        LLMResponse(content="Not read"),
    ]), tools=[tool], permission=permission, hooks=hooks)
    agent.plan_mode = boundary == "plan"

    assert agent.chat("Read the project guide") == "Not read"
    result = next(message["content"] for message in agent.messages if message["role"] == "tool")
    assert "server-owned" not in result
    assert {"permission": "Permission denied", "plan": "Plan mode is on", "hook": "resources blocked"}[boundary] in result
    assert tool.name not in Permission.READ_ONLY


def test_resource_names_do_not_replace_existing_remote_tools(resource_config):
    tools = load_mcp_tools(resource_config("collision"))

    assert {tool.name for tool in tools} == {"mcp__refs__echo", "mcp__refs__resources", "mcp__refs__resources_", "mcp__refs__resources__"}
    remote = next(tool for tool in tools if tool.name == "mcp__refs__resources")
    bridge = next(tool for tool in tools if tool.name == "mcp__refs__resources__")
    assert remote.execute() == "remote tool: resources"
    assert json.loads(bridge.execute(action="list"))["resources"]


@pytest.mark.parametrize("uri,message", [("docs://blob", "binary"), ("docs://broken", "contents"), ("docs://oversize", "32"), ("docs://missing", "Resource not found")])
def test_unsupported_or_failed_reads_are_explicit_errors(resource_config, uri, message):
    tool = _resource_tool(resource_config())

    with pytest.raises(MCPError, match=message):
        tool.execute(action="read", uri=uri)


@pytest.mark.parametrize("arguments,field", [
    ({"action": "read"}, "uri"),
    ({"action": "read", "uri": ""}, "uri"),
    ({"action": "read", "uri": True}, "uri"),
    ({"action": "list", "cursor": 3}, "cursor"),
    ({"action": "delete", "uri": "docs://guide"}, "action"),
])
def test_resource_arguments_are_checked_before_dispatch(resource_config, arguments, field):
    tool = _resource_tool(resource_config())

    with pytest.raises(MCPError, match=field):
        tool.execute(**arguments)


def test_existing_resource_bridge_survives_a_reconnect(resource_config):
    tool = _resource_tool(resource_config())
    client = tool._client
    before = json.loads(tool.execute(action="read", uri="docs://guide"))

    client.reconnect()

    assert json.loads(tool.execute(action="read", uri="docs://guide")) == before
