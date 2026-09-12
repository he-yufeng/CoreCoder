"""Pairwise tests for the three safety layers.

The three mechanisms - consent (Permission), plan mode (read-only), and hooks
(pre can veto / post observes only) - each have their own unit tests, but the
*combinations* between them were untested. This file pins the interactions the
project must never get wrong, since it is explicitly "the place people copy
from" (see roadmap issue #27, P1 "safety-layer combination matrix"):

  * plan mode outranks --yes, so a mutating call is refused even when consent
    would have auto-approved it, and consent is never even consulted;
  * a pre-hook veto reaches the model before consent, so consent is skipped;
  * a passing pre-hook still falls through to consent, which is asked once;
  * in a parallel round, plan mode refuses the mutating call while a read-only
    call still executes;
  * a read-only tool passes even under plan mode with a consent that would deny.
"""

import os
import tempfile

from corecoder import LLM, Agent
from corecoder.hooks import Hooks
from corecoder.permissions import Permission
from tests.conftest import get_tool


class _Call:
    """A minimal stand-in for an LLM tool_call: just a name and arguments."""

    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments or {}


class RecordingPermission(Permission):
    """Permission whose ask callback records every call it receives."""

    def __init__(self, verdict="once", allow_all=False):
        super().__init__(ask=self._ask, allow_all=allow_all)
        self.verdict = verdict
        self.asked = []

    def _ask(self, name, arguments):
        self.asked.append(name)
        return self.verdict


def _agent(permission=None, hooks=None, tools=None):
    return Agent(
        llm=LLM.__new__(LLM),
        tools=tools or [],
        permission=permission,
        hooks=hooks,
    )


def test_plan_mode_outranks_yes():
    """--yes (allow_all) must not override plan mode: a mutating call is refused
    and consent is never consulted."""
    perm = RecordingPermission(allow_all=True)
    agent = _agent(permission=perm)
    agent.plan_mode = True

    result = agent._permit(_Call("bash", {"command": "rm -rf /"}))

    assert result is not None
    assert "Plan mode is on" in result
    assert perm.asked == []  # plan mode short-circuits before consent


def test_pre_hook_veto_precedes_consent():
    """A pre-hook that vetoes a mutating call blocks it and must NOT consult
    consent (consent shouldn't even be asked)."""
    perm = RecordingPermission()
    hooks = Hooks(pre=[{"matcher": "bash", "command": "exit 2"}], post=[])
    agent = _agent(permission=perm, hooks=hooks)

    result = agent._pre_hooks(_Call("bash", {"command": "echo hi"}))

    assert result is not None
    assert result.startswith("Blocked by hook")
    assert perm.asked == []  # the hook veto bypassed consent


def test_passing_pre_hook_still_consults_consent():
    """A pre-hook that allows the call falls through to consent, which is asked
    exactly once."""
    perm = RecordingPermission()
    hooks = Hooks(pre=[{"matcher": "bash", "command": "exit 0"}], post=[])
    agent = _agent(permission=perm, hooks=hooks)

    decided = agent._pre_hooks(_Call("bash", {"command": "echo hi"}))
    assert decided is None  # the hook let it through

    agent._permit(_Call("bash", {"command": "echo hi"}))
    assert perm.asked == ["bash"]


def test_parallel_one_blocked_one_allowed():
    """In a parallel round, plan mode refuses the mutating call while the
    read-only call still executes and returns its content."""
    perm = RecordingPermission()
    agent = _agent(permission=perm, tools=[get_tool("read_file")])
    agent.plan_mode = True

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("hello safety matrix")
        path = f.name
    try:
        read_result, bash_result = agent._exec_tools_parallel(
            [
                _Call("read_file", {"file_path": path}),
                _Call("bash", {"command": "echo nope"}),
            ]
        )
        assert "hello safety matrix" in read_result  # read-only ran
        assert "Plan mode is on" in bash_result  # mutate refused
    finally:
        os.unlink(path)


def test_read_only_bypasses_plan_and_consent():
    """A read-only tool passes even under plan mode with a consent that would
    otherwise deny it."""
    perm = RecordingPermission(verdict="deny")
    agent = _agent(permission=perm)
    agent.plan_mode = True

    result = agent._permit(_Call("read_file", {"file_path": "/etc/hosts"}))

    assert result is None  # read-only is allowed despite plan mode + deny
