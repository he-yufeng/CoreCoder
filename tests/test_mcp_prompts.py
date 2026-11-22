# ruff: noqa: F811
"""Prompt discovery and rendering exercise the real transports and agent dispatch."""

import json

import pytest

from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.mcp import MCPError, load_mcp_tools
from corecoder.permissions import Permission
from tests.test_mcp_resources import resource_config  # noqa: F401


def _prompt_tool(config, name="mcp__refs__prompts"):
    tools = load_mcp_tools(config)
    selected = [tool for tool in tools if tool.name == name]
    assert len(selected) == 1, f"prompt bridge missing from {[tool.name for tool in tools]}"
    return selected[0]


def test_a_prompt_only_server_does_not_need_tools_list(resource_config):
    tools = load_mcp_tools(resource_config("prompts-only"))

    assert [tool.name for tool in tools] == ["mcp__refs__prompts"]
    result = json.loads(tools[0].execute(action="list"))
    assert result["prompts"][0]["name"] == "code_review"
    assert result["_meta"]["method"] == "prompts/list"


def test_invalid_prompt_capability_is_not_treated_as_support(resource_config, caplog):
    tools = load_mcp_tools(resource_config("invalid-prompts"))

    assert tools == []
    assert "prompts capability must be an object" in caplog.text


def test_prompt_pagination_preserves_opaque_cursors(resource_config):
    tool = _prompt_tool(resource_config("prompts"))
    first = json.loads(tool.execute(action="list"))
    second = json.loads(tool.execute(action="list", cursor=first["nextCursor"]))
    empty = json.loads(tool.execute(action="list", cursor=""))

    assert second["prompts"][0]["name"] == "release_notes"
    assert second["_meta"]["params"] == {"cursor": " prompt:2 "}
    assert "nextCursor" not in second
    assert empty["_meta"]["params"] == {"cursor": ""}


def test_get_passes_name_and_arguments_and_returns_server_messages(resource_config):
    tool = _prompt_tool(resource_config("prompts"))

    result = json.loads(tool.execute(action="get", name="code_review", arguments={"code": "print('hello')"}))

    assert result["_meta"] == {
        "method": "prompts/get",
        "params": {"name": "code_review", "arguments": {"code": "print('hello')"}},
    }
    assert result["messages"] == [{"role": "user", "content": {"type": "text", "text": "Review:\nprint('hello')"}}]


def test_the_agent_receives_prompt_results_as_normal_tool_results(resource_config):
    tool = _prompt_tool(resource_config("prompts"))
    agent = Agent(
        llm=ScriptedLLM(
            [
                LLMResponse(
                    tool_calls=[
                        ToolCall(
                            id="prompt",
                            name=tool.name,
                            arguments={"action": "get", "name": "code_review", "arguments": {"code": "pass"}},
                        )
                    ]
                ),
                LLMResponse(content="Prompt loaded"),
            ]
        ),
        tools=[tool],
        permission=Permission(allow_all=True),
    )

    assert agent.chat("Load the review prompt") == "Prompt loaded"
    result = next(message["content"] for message in agent.messages if message["role"] == "tool")
    assert json.loads(result)["messages"][0]["content"]["text"] == "Review:\npass"


def test_prompt_names_do_not_replace_existing_remote_tools(resource_config):
    tools = load_mcp_tools(resource_config("collision-prompts"))

    assert {tool.name for tool in tools} == {
        "mcp__refs__prompts",
        "mcp__refs__prompts_",
        "mcp__refs__prompts__",
        "mcp__refs__resources",
    }
    remote = next(tool for tool in tools if tool.name == "mcp__refs__prompts")
    bridge = next(tool for tool in tools if tool.name == "mcp__refs__prompts__")
    assert remote.execute() == "remote tool: prompts"
    assert json.loads(bridge.execute(action="list"))["prompts"]


@pytest.mark.parametrize(
    "name,message",
    [
        ("bad_content", "text"),
        ("broken", "messages"),
        ("oversize", "32"),
        ("missing", "Prompt not found"),
    ],
)
def test_unsupported_or_failed_prompt_results_are_explicit_errors(resource_config, name, message):
    tool = _prompt_tool(resource_config("prompts"))

    with pytest.raises(MCPError, match=message):
        tool.execute(action="get", name=name)


@pytest.mark.parametrize(
    "arguments,field",
    [
        ({"action": "get"}, "name"),
        ({"action": "get", "name": ""}, "name"),
        ({"action": "get", "name": True}, "name"),
        ({"action": "get", "name": "code_review", "cursor": "x"}, "cursor"),
        ({"action": "get", "name": "code_review", "arguments": []}, "arguments"),
        ({"action": "list", "name": "code_review"}, "name"),
        ({"action": "list", "arguments": {}}, "arguments"),
        ({"action": "list", "cursor": 3}, "cursor"),
        ({"action": "delete"}, "action"),
    ],
)
def test_prompt_arguments_are_checked_before_dispatch(resource_config, arguments, field):
    tool = _prompt_tool(resource_config("prompts"))

    with pytest.raises(MCPError, match=field):
        tool.execute(**arguments)


def test_existing_prompt_bridge_survives_a_reconnect(resource_config):
    tool = _prompt_tool(resource_config("prompts"))
    before = json.loads(tool.execute(action="get", name="code_review", arguments={"code": "pass"}))

    tool._client.reconnect()

    assert json.loads(tool.execute(action="get", name="code_review", arguments={"code": "pass"})) == before
