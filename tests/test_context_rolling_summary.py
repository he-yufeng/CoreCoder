"""Rolling summary: compress the delta, not the summary of a summary."""

from corecoder.context import ContextManager


class RecordingLLM:
    """Summarizer that records its payload and returns a marked summary."""

    def __init__(self):
        self.payloads: list[str] = []

    def chat(self, messages, **kw):
        self.payloads.append(messages[-1]["content"])
        index = len(self.payloads)

        class Response:
            content = f"summary#{index}"

        return Response()


def _chatty_rounds(messages: list[dict], tag: str, count: int = 4) -> None:
    for i in range(count):
        messages.append({"role": "user", "content": f"{tag}-{i} " + "c" * 300})
        messages.append({"role": "assistant", "content": f"{tag}-r{i} " + "d" * 300})


def _seed() -> list[dict]:
    messages = [{"role": "user", "content": "the deploy key lives in /etc/app/deploy.key"}]
    for i in range(12):
        messages.append({"role": "user", "content": f"task {i} " + "a" * 300})
        messages.append({"role": "assistant", "content": f"done {i} " + "b" * 300})
    return messages


def test_second_compression_summarizes_only_the_new_turns():
    ctx = ContextManager(max_tokens=2000)
    llm = RecordingLLM()
    messages = _seed()

    ctx.maybe_compress(messages, llm)
    _chatty_rounds(messages, "second")
    ctx.maybe_compress(messages, llm)

    assert len(llm.payloads) == 2
    assert "task 0" in llm.payloads[0]
    # The second pass must not re-summarize the summary the first one wrote.
    assert "[Context compressed" not in llm.payloads[1]
    assert "summary#1" not in llm.payloads[1]
    # It summarizes the turns that aged out of the kept tail since then.
    assert "task 8" in llm.payloads[1]
    assert "second-3" not in llm.payloads[1]


def test_prior_summary_is_carried_forward_verbatim():
    ctx = ContextManager(max_tokens=2000)
    llm = RecordingLLM()
    messages = _seed()

    ctx.maybe_compress(messages, llm)
    _chatty_rounds(messages, "second")
    ctx.maybe_compress(messages, llm)
    _chatty_rounds(messages, "third")
    ctx.maybe_compress(messages, llm)

    head = messages[0]["content"]
    assert "summary#1" in head
    assert "summary#2" in head
    assert "summary#3" in head


def test_hard_collapse_also_preserves_the_earlier_summary():
    ctx = ContextManager(max_tokens=2000)
    llm = RecordingLLM()
    messages = _seed()
    ctx.maybe_compress(messages, llm)
    _chatty_rounds(messages, "second", count=10)

    ctx._hard_collapse(messages, llm)

    assert "summary#1" in messages[0]["content"]
