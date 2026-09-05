"""Plan mode: read-only investigation, then a plan the user approves."""

from corecoder import Agent, Config, cli
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.write import WriteFileTool
from tests.conftest import get_tool


def _write_call(call_id, path):
    return ToolCall(id=call_id, name="write_file",
                    arguments={"file_path": str(path), "content": "x\n"})


def _agent(tmp_path, permission):
    return Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[_write_call("c1", tmp_path / "a.txt")]),
            LLMResponse(content="here is the plan"),
        ]),
        tools=[WriteFileTool()],
        permission=permission,
    )


def _repl_with(monkeypatch, agent, inputs):
    """Drive the real REPL with a scripted list of inputs."""
    it = iter(inputs)
    monkeypatch.setattr(cli, "pt_prompt", lambda *a, **k: next(it))
    cli._repl(agent, Config.from_env())


def test_plan_mode_refuses_a_write_and_the_model_gets_the_reason(tmp_path):
    agent = _agent(tmp_path, Permission(allow_all=True))  # even --yes loses to plan mode
    agent.plan_mode = True

    assert agent.chat("go") == "here is the plan"  # the loop survived the refusal
    assert not (tmp_path / "a.txt").exists()
    result = agent.messages[2]
    assert result["role"] == "tool" and result["tool_call_id"] == "c1"
    assert "Plan mode is on" in result["content"]
    assert "approve" in result["content"]  # the model is told how execution resumes


def test_plan_mode_off_lets_the_same_write_through(tmp_path):
    agent = _agent(tmp_path, Permission(allow_all=True))

    assert agent.chat("go") == "here is the plan"
    assert (tmp_path / "a.txt").exists()
    assert agent.messages[2]["content"].startswith("Wrote")


def test_read_only_tools_still_run_in_plan_mode(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hello", encoding="utf-8")
    agent = Agent(
        llm=ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="c1", name="read_file", arguments={"file_path": str(f)})]),
            LLMResponse(content="read it"),
        ]),
        tools=[get_tool("read_file")],
    )
    agent.plan_mode = True

    assert agent.chat("go") == "read it"
    assert "hello" in agent.messages[2]["content"]


def test_system_prompt_gains_the_plan_section_only_while_on():
    agent = Agent(llm=ScriptedLLM([]))

    assert "# Plan mode" not in agent._full_messages()[0]["content"]
    agent.plan_mode = True
    system = agent._full_messages()[0]["content"]
    assert "# Plan mode" in system
    assert "numbered list" in system
    agent.plan_mode = False
    assert "# Plan mode" not in agent._full_messages()[0]["content"]


def test_plan_slash_command_toggles(monkeypatch):
    agent = Agent(llm=ScriptedLLM([]))

    _repl_with(monkeypatch, agent, ["/plan", "quit"])
    assert agent.plan_mode is True

    _repl_with(monkeypatch, agent, ["/plan", "quit"])
    assert agent.plan_mode is False


def test_approve_exits_plan_mode_and_the_agent_proceeds(monkeypatch):
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="executing the plan")]))

    _repl_with(monkeypatch, agent, ["/plan", "approve", "quit"])

    assert agent.plan_mode is False
    # the approval itself went to the model as an ordinary user message
    assert agent.messages[0] == {"role": "user", "content": "approve"}
    assert agent.messages[1]["content"] == "executing the plan"
