"""MCP stdio servers: handshake, tool registration, calls, dying servers.

The fake server is a stdlib-only Python script run via sys.executable, so
these tests need no shell and run the same on Windows.
"""

import json
import logging
import sys

import pytest

from corecoder import mcp
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.hooks import Hooks
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPError, load_mcp_tools
from corecoder.permissions import Permission

FAKE_SERVER = """
import json, os, sys, time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8", newline="\\n")

TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "crash", "description": "Take the server down mid-call",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stall", "description": "Answer too slowly",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "fail", "description": "Report a tool-level error",
     "inputSchema": {"type": "object", "properties": {}}},
]

for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in req:
        continue  # notification, nothing to answer
    method, params = req.get("method"), req.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "0.1"}}
    elif method == "tools/list":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}) + "\\n")
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "crash":
            os._exit(1)
        if name == "stall":
            time.sleep(30)
        content = [{"type": "text", "text": "echo: " + args["text"] if name == "echo" else "bad input near 42"}]
        result = {"content": content, "isError": name == "fail"}
    else:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "no such method"}}) + "\\n")
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""


@pytest.fixture
def server_script(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return script


@pytest.fixture
def mcp_config(tmp_path, server_script):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"fake": {
        "command": sys.executable, "args": [str(server_script)]}}}), encoding="utf-8")
    return cfg


@pytest.fixture(autouse=True)
def _close_clients():
    yield
    for client in mcp._live_clients:
        client.close()
    mcp._live_clients.clear()


def _echo_call(call_id="c1", text="hi"):
    return ToolCall(id=call_id, name="mcp__fake__echo", arguments={"text": text})


def _agent(script, tools, **kwargs):
    return Agent(llm=ScriptedLLM(script), tools=tools, **kwargs)


def test_handshake_registers_each_remote_tool(mcp_config):
    tools = load_mcp_tools(mcp_config)
    assert {t.name for t in tools} == {
        "mcp__fake__echo", "mcp__fake__crash", "mcp__fake__stall", "mcp__fake__fail"}
    echo = next(t for t in tools if t.name == "mcp__fake__echo")
    assert echo.description == "Echo text back"
    assert echo.schema()["function"]["parameters"]["properties"]["text"] == {"type": "string"}


def test_call_round_trip_returns_text_content(mcp_config):
    agent = _agent(
        [LLMResponse(tool_calls=[_echo_call()]), LLMResponse(content="done")],
        load_mcp_tools(mcp_config),
    )

    assert agent.chat("go") == "done"
    result = agent.messages[2]
    assert result["role"] == "tool" and result["content"] == "echo: hi"


def test_parallel_calls_to_one_server_dont_cross_wires(mcp_config):
    agent = _agent(
        [LLMResponse(tool_calls=[_echo_call("c1", "one"), _echo_call("c2", "two")]),
         LLMResponse(content="done")],
        load_mcp_tools(mcp_config),
    )

    assert agent.chat("go") == "done"
    results = {m["tool_call_id"]: m["content"] for m in agent.messages if m["role"] == "tool"}
    assert results == {"c1": "echo: one", "c2": "echo: two"}


def test_server_crash_mid_call_fails_without_killing_the_loop(mcp_config):
    agent = _agent(
        [LLMResponse(tool_calls=[ToolCall(id="c1", name="mcp__fake__crash", arguments={})]),
         LLMResponse(tool_calls=[_echo_call("c2")]),
         LLMResponse(content="still alive")],
        load_mcp_tools(mcp_config),
    )

    assert agent.chat("go") == "still alive"
    crash_result = agent.messages[2]
    assert "Error executing mcp__fake__crash" in crash_result["content"]
    assert "exited" in crash_result["content"]
    # the server stays dead: the next call on it fails clean too
    assert "exited" in agent.messages[4]["content"]


def test_a_slow_server_times_out_the_call(mcp_config):
    stall = next(t for t in load_mcp_tools(mcp_config) if t.name == "mcp__fake__stall")
    stall._client.call_timeout = 0.2
    with pytest.raises(MCPError, match="no answer"):
        stall.execute()


def test_a_tool_level_error_surfaces_its_message(mcp_config):
    fail = next(t for t in load_mcp_tools(mcp_config) if t.name == "mcp__fake__fail")
    with pytest.raises(MCPError, match="bad input near 42"):
        fail.execute()


def test_missing_config_means_no_mcp(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_mcp_tools(tmp_path / "nope.json") == []
    assert caplog.records == []


def test_broken_config_is_ignored_with_one_warning(tmp_path, caplog):
    bad = tmp_path / "mcp.json"
    bad.write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert load_mcp_tools(bad) == []
    assert len(caplog.records) == 1
    assert "ignoring" in caplog.records[0].getMessage()


def test_an_unstartable_server_is_skipped_with_one_warning(tmp_path, caplog):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"ghost": {"command": "not-a-real-binary-xyz"}}}))
    with caplog.at_level(logging.WARNING):
        assert load_mcp_tools(cfg) == []
    assert len(caplog.records) == 1
    assert "ghost" in caplog.records[0].getMessage()


def test_mcp_tools_sit_behind_the_consent_gate():
    # not in READ_ONLY, so with nobody to ask the call is refused, never run
    assert Permission().check("mcp__fake__echo", {}) is not None


def test_hooks_match_mcp_tool_names(mcp_config):
    blocker = f'"{sys.executable}" -c "import sys; sys.stderr.write(\'mcp frozen\'); sys.exit(2)"'
    agent = _agent(
        [LLMResponse(tool_calls=[_echo_call()]), LLMResponse(content="done")],
        load_mcp_tools(mcp_config),
        hooks=Hooks(pre=[{"matcher": "mcp__fake__echo", "command": blocker}], post=[]),
    )

    assert agent.chat("go") == "done"
    assert "mcp frozen" in agent.messages[2]["content"]
