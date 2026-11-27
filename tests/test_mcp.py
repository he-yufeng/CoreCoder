"""MCP stdio servers: handshake, tool registration, calls, dying servers.

The fake server is a stdlib-only Python script run via sys.executable, so
these tests need no shell and run the same on Windows.
"""

import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from corecoder import mcp
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.hooks import Hooks
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPError, load_mcp_tools
from corecoder.permissions import Permission
from tests.conftest import repl_with

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
        if req.get("method") == "notifications/cancelled" and os.environ.get("CANCEL_LOG"):
            with open(os.environ["CANCEL_LOG"], "a", encoding="utf-8") as log:
                log.write(json.dumps({**(req.get("params") or {}), "pid": os.getpid()}) + "\\n")
        continue  # notification, nothing to answer
    method, params = req.get("method"), req.get("params") or {}
    if method == "initialize":
        if os.environ.get("INITIALIZE_DELAY"):
            time.sleep(float(os.environ["INITIALIZE_DELAY"]))
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
            if os.environ.get("STALL_LOG"):
                with open(os.environ["STALL_LOG"], "a", encoding="utf-8") as log:
                    log.write(str(req["id"]) + "\\n")
            time.sleep(0.5)
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
        "command": sys.executable, "args": [str(server_script)],
        "env": {"CANCEL_LOG": str(tmp_path / "cancelled.jsonl"),
                "STALL_LOG": str(tmp_path / "stalled.jsonl")}}}}), encoding="utf-8")
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


def test_timeout_cancels_the_request_and_ignores_its_late_reply(mcp_config, monkeypatch):
    tools = load_mcp_tools(mcp_config)
    stall = next(t for t in tools if t.name == "mcp__fake__stall")
    echo = next(t for t in tools if t.name == "mcp__fake__echo")
    client = stall._client
    request_id = client._next_id + 1

    with monkeypatch.context() as timeout:
        timeout.setattr(client, "call_timeout", 0.2)
        with pytest.raises(MCPError, match="no answer"):
            stall.execute()
    time.sleep(0.6)
    assert echo.execute(text="after") == "echo: after"

    cancelled = [
        json.loads(line)
        for line in (mcp_config.parent / "cancelled.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["requestId"] == request_id
    assert "tools/call timed out" in cancelled[0]["reason"]
    assert client._pending == set()
    assert client._responses == {}


def test_reconnect_does_not_send_an_old_timeout_to_the_new_process(mcp_config, monkeypatch):
    old_reader_at_eof = threading.Event()
    release_old_reader = threading.Event()
    real_popen = mcp.subprocess.Popen

    class GatedStdout:
        def __init__(self, stream):
            self._stream = stream

        def __iter__(self):
            return self

        def __next__(self):
            try:
                return next(self._stream)
            except StopIteration:
                old_reader_at_eof.set()
                assert release_old_reader.wait(3)
                raise

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        proc.stdout = GatedStdout(proc.stdout)
        return proc

    monkeypatch.setattr(mcp.subprocess, "Popen", popen)
    tools = load_mcp_tools(mcp_config)
    stall = next(t for t in tools if t.name == "mcp__fake__stall")
    echo = next(t for t in tools if t.name == "mcp__fake__echo")
    client = stall._client
    client.call_timeout = 1
    stalled = mcp_config.parent / "stalled.jsonl"

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(stall.execute)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and (not stalled.exists() or not stalled.read_text().strip()):
                time.sleep(0.01)
            assert stalled.exists() and stalled.read_text().strip()

            client.reconnect()
            assert old_reader_at_eof.wait(1)
            new_pid = client._proc.pid
            assert echo.execute(text="new") == "echo: new"
            with pytest.raises(MCPError, match="no answer"):
                old.result(timeout=3)
    finally:
        release_old_reader.set()

    cancelled_path = mcp_config.parent / "cancelled.jsonl"
    cancelled = [json.loads(line) for line in cancelled_path.read_text().splitlines()] if cancelled_path.exists() else []
    assert all(record["pid"] != new_pid for record in cancelled)


def test_initialize_timeout_is_never_cancelled(server_script, tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "INIT_TIMEOUT", 0.05)
    cancelled = tmp_path / "cancelled.jsonl"
    with pytest.raises(MCPError, match="no answer"):
        mcp.MCPClient(
            "fake",
            sys.executable,
            [str(server_script)],
            {"CANCEL_LOG": str(cancelled), "INITIALIZE_DELAY": "0.2"},
        )
    assert not cancelled.exists()


def test_a_tool_level_error_surfaces_its_message(mcp_config):
    fail = next(t for t in load_mcp_tools(mcp_config) if t.name == "mcp__fake__fail")
    with pytest.raises(MCPError, match="bad input near 42"):
        fail.execute()


def test_missing_config_means_no_mcp(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_mcp_tools(tmp_path / "nope.json") == []
    assert caplog.records == []


def test_shipped_examples_match_the_loader_contract():
    # Every examples/*.json must stay loadable: mcpServers entries with a
    # string command, list args and dict env when present.
    examples = list((Path(__file__).resolve().parent.parent / "examples").glob("*.json"))
    assert examples, "no example configs to pin"
    for path in examples:
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers")
        assert isinstance(servers, dict) and servers, f"{path.name}: mcpServers missing"
        for name, spec in servers.items():
            assert isinstance(name, str) and name, f"{path.name}: blank server name"
            assert isinstance(spec.get("command"), str), f"{path.name}: {name} needs a command"
            assert isinstance(spec.get("args", []), list), f"{path.name}: {name} args not a list"
            assert isinstance(spec.get("env", {}), dict), f"{path.name}: {name} env not a dict"


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


def test_mcp_command_lists_each_server_and_its_state(mcp_config, monkeypatch, capsys):
    agent = _agent([], load_mcp_tools(mcp_config))

    repl_with(monkeypatch, agent, ["/mcp", "quit"])

    out = capsys.readouterr().out
    assert "fake" in out and "alive" in out and "4 tools" in out


def test_mcp_command_marks_a_dead_server_with_its_error(mcp_config, monkeypatch, capsys):
    tools = load_mcp_tools(mcp_config)
    crash = next(t for t in tools if t.name == "mcp__fake__crash")
    with pytest.raises(MCPError, match="exited"):
        crash.execute()

    repl_with(monkeypatch, _agent([], tools), ["/mcp", "quit"])

    out = capsys.readouterr().out
    assert "dead" in out and "exited" in out and "/mcp reconnect fake" in out


def test_mcp_reconnect_revives_a_dead_server(mcp_config, monkeypatch, capsys):
    tools = load_mcp_tools(mcp_config)
    echo = next(t for t in tools if t.name == "mcp__fake__echo")
    crash = next(t for t in tools if t.name == "mcp__fake__crash")
    with pytest.raises(MCPError, match="exited"):
        crash.execute()
    with pytest.raises(MCPError, match="exited"):
        echo.execute(text="while dead")

    repl_with(monkeypatch, _agent([], tools), ["/mcp reconnect fake", "quit"])

    assert "Reconnected" in capsys.readouterr().out
    # the tool object the agent grabbed before the crash works again untouched
    assert echo.execute(text="back") == "echo: back"


def test_mcp_reconnect_failure_keeps_the_server_dead(mcp_config, monkeypatch, capsys):
    tools = load_mcp_tools(mcp_config)
    crash = next(t for t in tools if t.name == "mcp__fake__crash")
    with pytest.raises(MCPError, match="exited"):
        crash.execute()
    client = mcp._live_clients[0]
    client._command = "not-a-real-binary-xyz"  # the server can no longer be spawned

    repl_with(monkeypatch, _agent([], tools), ["/mcp reconnect fake", "quit"])

    assert "Reconnect failed" in capsys.readouterr().out
    assert client.error is not None


def test_mcp_command_with_no_servers_or_bad_arguments(monkeypatch, capsys):
    agent = _agent([], [])

    repl_with(monkeypatch, agent, ["/mcp", "/mcp reconnect ghost", "/mcp frobnicate", "quit"])

    out = capsys.readouterr().out
    assert "No MCP servers running" in out
    assert "No MCP server named 'ghost'" in out
    assert "Usage: /mcp" in out
    assert agent.messages == []  # none of it reached the model
