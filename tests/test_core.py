"""Tests for core modules: config, context, session, imports."""

import os
import re
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pytest
from openai import BadRequestError

from corecoder import ALL_TOOLS, LLM, Agent, Config, __version__
from corecoder import session as session_module
from corecoder.context import ContextManager, estimate_tokens
from corecoder.session import list_sessions, load_session, save_session
from tests.conftest import get_tool


def test_version():
    # regex instead of tomllib: the latter only exists on 3.11+ and CI runs 3.10
    m = re.search(r'(?m)^version = "([^"]+)"', Path("pyproject.toml").read_text())
    assert m is not None
    assert __version__ == m.group(1)


def test_readme_line_counts_are_current():
    # The LoC numbers are the brand of this repo. If the engine or the
    # package grows, the README has to move with it, and this test is the
    # alarm: update the badge and the prose in README.md and README_CN.md.
    root = Path(__file__).resolve().parent.parent
    engine_files = [
        root / "corecoder" / name
        for name in ("agent.py", "llm.py", "context.py", "session.py")
    ]
    engine_files += sorted((root / "corecoder" / "tools").glob("*.py"))
    package_files = sorted((root / "corecoder").rglob("*.py"))

    def net_lines(path: Path) -> int:
        return sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    engine = sum(net_lines(f) for f in engine_files)
    physical = sum(
        len(f.read_text(encoding="utf-8").splitlines()) for f in package_files
    )
    package_net = sum(net_lines(f) for f in package_files)

    readme = (root / "README.md").read_text(encoding="utf-8")
    assert f"engine-{engine}_LoC" in readme
    assert f"{len(package_files)} files" in readme
    assert f"{physical:,} physical lines" in readme
    assert f"{package_net:,} net" in readme


def test_public_api_exports():
    """Users should be able to import key classes from the top-level package."""
    assert Agent is not None
    assert LLM is not None
    assert Config is not None
    assert len(ALL_TOOLS) == 8


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CORECODER_MODEL", "test-model")
    c = Config.from_env()
    assert c.model == "test-model"


def test_config_defaults(monkeypatch):
    # clear relevant env vars without leaking the change into other tests
    monkeypatch.delenv("CORECODER_MODEL", raising=False)
    monkeypatch.delenv("CORECODER_MAX_TOKENS", raising=False)

    c = Config.from_env()
    assert c.model == "gpt-5.5"
    assert c.max_tokens == 4096
    assert c.temperature == 0.0


# --- Context ---

def test_estimate_tokens():
    msgs = [{"role": "user", "content": "hello world"}]
    t = estimate_tokens(msgs)
    assert t > 0
    assert t < 100


def test_estimate_tokens_tiered_by_content():
    from corecoder.context import _approx_tokens

    prose = "The quick brown fox jumps over the lazy dog. " * 10  # 460 prose chars
    cjk = "你好世界，这是一段中文。" * 20  # 260 hanzi-ish chars
    code = 'def f(x):\n    return {"k": [1, 2, 3], "s": "{}"}\n' * 10  # symbol-dense

    # CJK reads near 1.5 chars/token, roughly double the old flat rate
    assert _approx_tokens(cjk) == pytest.approx(len(cjk) / 1.5, rel=0.1)
    # prose sits under the old 3-chars rate, symbol soup sits above it
    assert _approx_tokens(prose) == pytest.approx(len(prose) / 3.4, rel=0.1)
    assert _approx_tokens(code) == pytest.approx(len(code) / 2.8, rel=0.1)
    # and every tier still beats the "half the real size" failure the old
    # estimator had on CJK: a 260-char Chinese chat must not read as 86 tokens
    assert _approx_tokens(cjk) > 150


def test_context_snip():
    ctx = ContextManager(max_tokens=3000)
    msgs = [
        {"role": "tool", "tool_call_id": "t1", "content": "x\n" * 1000},
    ]
    before = estimate_tokens(msgs)
    ctx._snip_tool_outputs(msgs)
    after = estimate_tokens(msgs)
    assert after < before


def test_context_compress():
    ctx = ContextManager(max_tokens=2000)
    msgs = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"msg {i} " + "a" * 200})
        msgs.append({"role": "tool", "tool_call_id": f"t{i}", "content": "b" * 2000})
    before = estimate_tokens(msgs)
    ctx.maybe_compress(msgs, None)
    after = estimate_tokens(msgs)
    assert after < before
    assert len(msgs) < 40  # should be compressed


def test_safe_split_never_orphans_a_tool_message():
    """The kept tail must not begin with a 'tool' message - it would be severed
    from the assistant tool_calls that produced it, which the API rejects."""
    ctx = ContextManager(max_tokens=1000)
    messages = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "tool", "tool_call_id": "c2", "content": "result2"},
    ]
    split = ctx._safe_split(messages, keep_recent=1)
    assert messages[split].get("role") != "tool"


def test_compress_never_leaves_an_orphan_tool_reply():
    """After summarisation every tool reply must still follow its tool_calls."""
    ctx = ContextManager(max_tokens=2000)
    msgs = []
    for i in range(20):
        msgs.append({"role": "user", "content": f"msg {i} " + "a" * 200})
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"c{i}"}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "b" * 800})
    ctx.maybe_compress(msgs, None)
    for i, m in enumerate(msgs):
        if m.get("role") == "tool":
            prev = msgs[i - 1]
            assert prev.get("role") == "tool" or prev.get("tool_calls"), f"orphan tool at {i}"


# --- Session ---

def test_session_save_load(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    msgs = [{"role": "user", "content": "test message"}]
    save_session(msgs, "test-model", "pytest_test_session")
    loaded = load_session("pytest_test_session")
    assert loaded is not None
    assert loaded[0] == msgs
    assert loaded[1] == "test-model"


def test_session_name_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    msgs = [{"role": "user", "content": "test message"}]
    sid = save_session(msgs, "test-model", "../Research Notes!")

    assert sid == "Research-Notes"
    assert (tmp_path / "Research-Notes.json").exists()
    assert load_session("../Research Notes!") is not None


def test_session_not_found():
    assert load_session("nonexistent_session_id") is None


def test_list_sessions():
    sessions = list_sessions()
    assert isinstance(sessions, list)


# --- Cost estimation ---

def test_cost_estimation_known_model():
    from corecoder.llm import LLM
    llm = LLM.__new__(LLM)
    llm.model = "gpt-5.4"
    llm.total_prompt_tokens = 1_000_000
    llm.total_completion_tokens = 500_000
    cost = llm.estimated_cost
    assert cost is not None
    assert cost == 2.5 + 7.5  # $2.5/M in + $15/M out * 0.5M

def test_cost_estimation_unknown_model():
    from corecoder.llm import LLM
    llm = LLM.__new__(LLM)
    llm.model = "some-custom-model"
    llm.total_prompt_tokens = 1000
    llm.total_completion_tokens = 500
    assert llm.estimated_cost is None


# --- Changed files tracking ---

def test_edit_tracks_changed_files(tmp_path):
    from corecoder.tools.edit import _changed_files
    _changed_files.clear()
    edit = get_tool("edit_file")
    path = tmp_path / "sample.py"
    path.write_text("aaa\nbbb\n")
    edit.execute(file_path=str(path), old_string="aaa", new_string="zzz")
    assert any(str(path) in p for p in _changed_files)
    _changed_files.clear()


def test_write_tracks_changed_files(tmp_path):
    from corecoder.tools.edit import _changed_files
    _changed_files.clear()
    write = get_tool("write_file")
    path = tmp_path / "tracked.txt"
    write.execute(file_path=str(path), content="tracked\n")
    assert any(path.name in p for p in _changed_files)
    _changed_files.clear()


# --- Agent tool execution ---

def test_parallel_bash_calls_inherit_and_merge_session_cwd(tmp_path):
    """Pool workers must start from the session cwd, not the launch dir, and a
    cd inside the batch moves the session cwd afterwards."""
    from corecoder.tools.bash import get_tracked_cwd, set_tracked_cwd

    def norm_dir(s: str) -> str:
        # pwd prints the shell's own form: git-bash gives /c/Users/... where
        # Python gives C:\Users\...; compare both in one canonical shape
        s = s.strip().replace("\\", "/").rstrip("/").lower()
        if os.name == "nt" and s.startswith("/tmp"):
            import tempfile

            s = tempfile.gettempdir().replace("\\", "/").rstrip("/").lower() + s[4:]
        if len(s) >= 3 and s[0] == "/" and s[2] == "/" and s[1].isalpha():
            s = s[1] + ":/" + s[3:]
        return s

    bash = get_tool("bash")
    agent = Agent(llm=LLM.__new__(LLM), tools=[bash])
    target = tmp_path / "proj"
    target.mkdir()
    (tmp_path / "marker.txt").write_text("x")

    class _TC:
        def __init__(self, i, cmd):
            self.name, self.id, self.arguments = "bash", str(i), {"command": cmd}

    prev = get_tracked_cwd()
    set_tracked_cwd(str(tmp_path))
    try:
        results = agent._exec_tools_parallel([_TC(1, "pwd"), _TC(2, "ls marker.txt")])
        assert norm_dir(str(tmp_path)) in norm_dir(results[0])
        assert "marker.txt" in results[1]

        # a cd in the batch lands on the session afterwards; siblings in the
        # same batch still start from the pre-batch cwd (parallel, not serial)
        results = agent._exec_tools_parallel([_TC(3, f"cd {target.as_posix()}"), _TC(4, "pwd")])
        assert get_tracked_cwd() == str(target)
        assert norm_dir(str(tmp_path)) in norm_dir(results[1])
    finally:
        set_tracked_cwd(prev)


def test_parallel_edits_to_one_file_both_land(tmp_path):
    """Two edit_file calls on one file in one batch must not lose either."""
    edit = get_tool("edit_file")
    agent = Agent(llm=LLM.__new__(LLM), tools=[edit])
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\nthree\n")

    class _TC:
        def __init__(self, i, old, new):
            self.name, self.id = "edit_file", str(i)
            self.arguments = {"file_path": str(f), "old_string": old, "new_string": new}

    results = agent._exec_tools_parallel([_TC(1, "one", "1"), _TC(2, "three", "3")])
    assert all(r.startswith("Edited") for r in results)
    assert f.read_text() == "1\ntwo\n3\n"


def test_sub_agent_cwd_does_not_leak_into_parent(tmp_path, monkeypatch):
    """A sub-agent's own cd must not move the parent's tracked cwd."""
    from corecoder.agent import Agent as CoreAgent
    from corecoder.tools.agent import AgentTool
    from corecoder.tools.bash import get_tracked_cwd, set_tracked_cwd

    parent = Agent(llm=LLM.__new__(LLM), tools=[])
    tool = AgentTool()
    tool._parent_agent = parent
    target = tmp_path / "sub"
    target.mkdir()

    def fake_chat(self, text, **kwargs):
        set_tracked_cwd(str(target))  # the sub-agent cd's somewhere else
        return "done"

    monkeypatch.setattr(CoreAgent, "chat", fake_chat)
    prev = get_tracked_cwd()
    set_tracked_cwd(str(tmp_path))
    try:
        assert tool.execute(task="x") == "[Sub-agent completed]\ndone"
        assert get_tracked_cwd() == str(tmp_path)
    finally:
        set_tracked_cwd(prev)


def test_reset_clears_todo_list():
    """/reset must drop the dead conversation's checklist from the system prompt."""
    from corecoder.tools.todo import TodoWriteTool

    todo = TodoWriteTool()
    agent = Agent(llm=LLM.__new__(LLM), tools=[todo])
    todo.execute(tasks=[{"content": "fix parser bug", "status": "in_progress"}])
    assert "fix parser bug" in agent._full_messages()[0]["content"]
    agent.reset()
    assert todo._tasks == []
    assert "# Current task list" not in agent._full_messages()[0]["content"]


def test_agent_tool_scope_is_per_instance():
    """An Agent restricted to a subset of tools must not resolve tools outside it."""
    only_read = [get_tool("read_file")]
    agent = Agent(llm=LLM.__new__(LLM), tools=only_read)
    assert set(agent._tool_by_name) == {"read_file"}

    class _TC:
        name = "bash"  # a real, registered tool - but not in this agent's set
        id = "x"
        arguments: ClassVar[dict] = {"command": "echo hi"}

    assert "unknown tool 'bash'" in agent._exec_tool(_TC())


def test_exec_tool_distinguishes_bad_args_from_internal_error():
    """A TypeError raised inside a tool must not be reported as bad arguments."""
    from corecoder.tools.base import Tool

    class _Boom(Tool):
        name = "boom"
        description = "raises TypeError internally"
        parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}

        def execute(self):
            raise TypeError("internal explosion")

    agent = Agent(llm=LLM.__new__(LLM), tools=[_Boom()])

    class _BadArgs:
        name, id, arguments = "boom", "1", {"unexpected": 1}

    class _Good:
        name, id, arguments = "boom", "2", {}

    assert "bad arguments" in agent._exec_tool(_BadArgs())
    assert "Error executing boom" in agent._exec_tool(_Good())
    assert "bad arguments" not in agent._exec_tool(_Good())


def test_interrupt_backfills_missing_tool_replies():
    """A half-finished tool round must be repaired so history stays valid."""
    agent = Agent(llm=LLM.__new__(LLM), tools=[])
    agent.messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "done"},
    ]

    class _TC:
        def __init__(self, i):
            self.id = i

    agent._answer_pending_tool_calls([_TC("a"), _TC("b")])
    replies = [m for m in agent.messages if m.get("role") == "tool"]
    ids = [m["tool_call_id"] for m in replies]
    assert sorted(ids) == ["a", "b"]
    assert ids.count("a") == 1  # the already-answered call wasn't duplicated


# --- Task list injection ---

def test_todo_list_is_injected_into_system_context():
    """After a todo_write call, the next request must carry the list in the system message."""
    from corecoder.tools.todo import TodoWriteTool
    todo = TodoWriteTool()
    agent = Agent(llm=LLM.__new__(LLM), tools=[todo])

    todo.execute(tasks=[
        {"content": "fix the bug", "status": "in_progress"},
        {"content": "add a test", "status": "pending"},
    ])
    system = agent._full_messages()[0]["content"]
    assert "# Current task list" in system
    assert "1. [in_progress] fix the bug" in system
    assert "2. [pending] add a test" in system


def test_todo_injection_tracks_updates():
    """The injection is rebuilt every round: updates show, an empty list injects nothing."""
    from corecoder.tools.todo import TodoWriteTool
    todo = TodoWriteTool()
    agent = Agent(llm=LLM.__new__(LLM), tools=[todo])

    todo.execute(tasks=[{"content": "only task", "status": "in_progress"}])
    todo.execute(tasks=[{"content": "only task", "status": "done"}])
    system = agent._full_messages()[0]["content"]
    assert "[done] only task" in system
    assert "[in_progress] only task" not in system

    todo.execute(tasks=[])
    assert "# Current task list" not in agent._full_messages()[0]["content"]


def test_agent_without_todo_tool_injects_nothing():
    agent = Agent(llm=LLM.__new__(LLM), tools=[get_tool("read_file")])
    assert "# Current task list" not in agent._full_messages()[0]["content"]


# ---------------------------------------------------------------------------
# LLM.chat() provider-dialect fallback (400 param adaptation)
# ---------------------------------------------------------------------------


class TestLLMParamFallback:
    """Newer OpenAI models reject max_tokens / non-default temperature with a
    400 naming the parameter; chat() adapts that one param and retries."""

    @staticmethod
    def _make_llm():
        llm = LLM(model="gpt-5", api_key="sk-test", max_tokens=1024, temperature=0.7)
        llm.client = mock.Mock()
        return llm

    @staticmethod
    def _bad_request(msg):
        try:
            import httpx
        except ModuleNotFoundError:
            # openai>=3.14 moved its HTTP layer from httpx to httpx2; the
            # SDK's error types take a Response from whichever is installed.
            import httpx2 as httpx

        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        return BadRequestError(msg, response=httpx.Response(400, request=req), body=None)

    @staticmethod
    def _stream():
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", tool_calls=None))],
            )
        ]

    def _chat_with_failures(self, llm, failures):
        create = llm.client.chat.completions.create
        create.side_effect = [*failures, self._stream()]
        result = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "ok"
        return create

    def test_max_tokens_translated_when_rejected(self):
        llm = self._make_llm()
        create = self._chat_with_failures(
            llm,
            [self._bad_request("Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.")],
        )
        retry_kwargs = create.call_args_list[-1][1]
        assert retry_kwargs["max_completion_tokens"] == 1024
        assert "max_tokens" not in retry_kwargs

    def test_temperature_dropped_when_rejected(self):
        llm = self._make_llm()
        create = self._chat_with_failures(
            llm,
            [self._bad_request("Unsupported value: 'temperature' does not support 0.7 with this model.")],
        )
        retry_kwargs = create.call_args_list[-1][1]
        assert "temperature" not in retry_kwargs
        assert retry_kwargs["max_tokens"] == 1024

    def test_stream_options_still_dropped_on_first_400(self):
        llm = self._make_llm()
        create = self._chat_with_failures(llm, [self._bad_request("Bad request")])
        retry_kwargs = create.call_args_list[-1][1]
        assert "stream_options" not in retry_kwargs
        assert retry_kwargs["max_tokens"] == 1024

    def test_unrecognized_400_reraises(self):
        llm = self._make_llm()
        with pytest.raises(BadRequestError):
            self._chat_with_failures(
                llm,
                [self._bad_request("Unsupported parameter: 'response_format' here"),
                 self._bad_request("Unsupported parameter: 'response_format' here")],
            )

    def test_rejected_max_completion_tokens_does_not_loop(self):
        """The max_tokens error message names max_completion_tokens, and the
        substring must not re-trigger translation once translated."""
        llm = self._make_llm()
        create = self._chat_with_failures(
            llm,
            [self._bad_request("Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."),
             self._bad_request("Unsupported parameter: 'max_completion_tokens' is unknown here")],
        )
        # second failure surfaces because neither retry looped nor re-adapted
        retry_kwargs = create.call_args_list[-1][1]
        assert "max_completion_tokens" in retry_kwargs


# ---------------------------------------------------------------------------
# LLM.chat() reasoning passthrough and mid-stream retry
# ---------------------------------------------------------------------------


class TestReasoningPassthrough:
    """Thinking models stream reasoning_content next to content. It is shown
    through on_reasoning but never mixed into the reply or the history."""

    @staticmethod
    def _make_llm():
        llm = LLM(model="deepseek-reasoner", api_key="sk-test")
        llm.client = mock.Mock()
        return llm

    @staticmethod
    def _stream():
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None, tool_calls=None, reasoning_content="think "))],
            ),
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content="answer", tool_calls=None, reasoning_content=None))],
            ),
            SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None, tool_calls=None, reasoning_content="hard"))],
            ),
        ]

    def test_reasoning_forwarded_and_content_clean(self):
        llm = self._make_llm()
        llm.client.chat.completions.create.return_value = self._stream()
        reasoning_parts = []
        result = llm.chat(
            messages=[{"role": "user", "content": "hi"}],
            on_reasoning=reasoning_parts.append,
        )
        assert reasoning_parts == ["think ", "hard"]
        assert result.content == "answer"
        # reasoning must not leak into the history message
        assert "think" not in str(result.message)
        assert llm.total_prompt_tokens == 5
        assert llm.total_completion_tokens == 3

    def test_reasoning_without_callback_is_dropped(self):
        llm = self._make_llm()
        llm.client.chat.completions.create.return_value = self._stream()
        result = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "answer"

    def test_openrouter_reasoning_field_also_forwarded(self):
        # aggregators normalize the chain-of-thought onto delta.reasoning
        from types import SimpleNamespace

        llm = self._make_llm()
        llm.client.chat.completions.create.return_value = [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content="answer", tool_calls=None, reasoning="ponder"))],
            ),
        ]
        reasoning_parts = []
        result = llm.chat(
            messages=[{"role": "user", "content": "hi"}],
            on_reasoning=reasoning_parts.append,
        )
        assert reasoning_parts == ["ponder"]
        assert result.content == "answer"


class _DyingStream:
    """Yields one chunk, then raises like a dropped connection mid-stream."""

    def __init__(self, exc):
        self._exc = exc
        self._yielded = False

    def __iter__(self):
        return self

    def __next__(self):
        from types import SimpleNamespace

        if not self._yielded:
            self._yielded = True
            return SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content="part", tool_calls=None, reasoning_content=None))],
            )
        raise self._exc


class TestMidStreamRetry:
    """A stream that dies partway is re-issued wholesale; create-time
    transients stay inside _call_with_retry and never double up."""

    @staticmethod
    def _make_llm():
        llm = LLM(model="gpt-4o", api_key="sk-test")
        llm.client = mock.Mock()
        return llm

    @staticmethod
    def _good_stream():
        from types import SimpleNamespace

        return iter([SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="full", tool_calls=None, reasoning_content=None))],
        )])

    @staticmethod
    def _connection_error():
        try:
            import httpx
        except ModuleNotFoundError:
            import httpx2 as httpx

        from openai import APIConnectionError

        return APIConnectionError(
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))

    @staticmethod
    def _server_error():
        try:
            import httpx
        except ModuleNotFoundError:
            import httpx2 as httpx

        from openai import InternalServerError

        req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        return InternalServerError(
            "boom", response=httpx.Response(500, request=req), body=None)

    def test_connection_drop_mid_stream_retries(self):
        llm = self._make_llm()
        create = llm.client.chat.completions.create
        create.side_effect = [
            _DyingStream(self._connection_error()),
            self._good_stream(),
        ]
        with mock.patch("corecoder.llm.time.sleep"):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "full"
        assert create.call_count == 2
        # usage comes only from the successful attempt, never double counted
        assert llm.total_prompt_tokens == 7
        assert llm.total_completion_tokens == 2

    def test_server_error_mid_stream_retries(self):
        llm = self._make_llm()
        create = llm.client.chat.completions.create
        create.side_effect = [
            _DyingStream(self._server_error()),
            self._good_stream(),
        ]
        with mock.patch("corecoder.llm.time.sleep"):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "full"
        assert create.call_count == 2

    def test_client_error_mid_stream_not_retried(self):
        llm = self._make_llm()
        create = llm.client.chat.completions.create
        create.side_effect = [_DyingStream(TestLLMParamFallback._bad_request("nope"))]
        with pytest.raises(BadRequestError):
            llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert create.call_count == 1

    def test_retry_exhausts_and_raises(self):
        llm = self._make_llm()
        create = llm.client.chat.completions.create
        create.side_effect = [
            _DyingStream(self._connection_error()),
            _DyingStream(self._connection_error()),
            _DyingStream(self._connection_error()),
        ]
        from openai import APIConnectionError

        with mock.patch("corecoder.llm.time.sleep"), pytest.raises(APIConnectionError):
            llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert create.call_count == 3
