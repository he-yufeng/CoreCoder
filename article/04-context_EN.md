# Surviving a long task in a finite window

An agent has one physical constraint it can't get around: the context window is only so big.

And coding tasks happen to be prolific token producers. The model reads a thousand-line file, and those thousand lines, line numbers and all, go into the history; it runs a test, and several hundred lines of output go into the history; it greps once, and dozens of matches go into the history. A halfway-decent task running a dozen-odd rounds burns tens of thousands of tokens. Once the window fills, either the API errors or you have to cut the history, and cut it badly and the agent starts "forgetting": a file it read earlier it reads again, a decision it just made it overturns.

So fitting a long task into a finite window is one of the most hardcore subproblems in agent engineering. This piece looks at how `corecoder/context.py` (222 lines) solves it.

## Layered, lightest to heaviest

Claude Code's strategy is four layers in public teardowns, escalating from the cheapest handling to the most aggressive. CoreCoder distills it to three, same idea: space you can save with a cheap means, never spend an expensive means on. The three layers only trigger once the window hits certain proportions:

```python
self._snip_at = int(max_tokens * 0.50)      # 50% -> snip bloated tool outputs
self._summarize_at = int(max_tokens * 0.70)  # 70% -> LLM-summarize old conversation
self._collapse_at = int(max_tokens * 0.90)   # 90% -> hard collapse, last resort
```

`maybe_compress` is the dispatcher. It first estimates how many tokens are currently used, then applies each layer on demand from light to heavy, re-estimating after each, and stops once it's enough:

```python
def maybe_compress(self, messages, llm=None) -> bool:
    current = estimate_tokens(messages)
    compressed = False

    if current > self._snip_at:
        if self._snip_tool_outputs(messages):
            compressed = True
            current = estimate_tokens(messages)

    if current > self._summarize_at and len(messages) > 10:
        if self._summarize_old(messages, llm, keep_recent=8):
            compressed = True
            current = estimate_tokens(messages)

    if current > self._collapse_at and len(messages) > 4:
        self._hard_collapse(messages, llm)
        compressed = True

    return compressed
```

Those two `self.context.maybe_compress(...)` calls in the previous pieces call into here. It fires before every request and after every round of tool execution, but the overwhelming majority of the time the window isn't full and the function does nothing and returns. Compression is lazy, spending effort only when about to hit the wall.

As for how tokens get estimated, `estimate_tokens` uses a method crude to the point of being endearing: character count divided by 3.

```python
def _approx_tokens(text: str) -> int:
    """Rough token count, roughly 3 chars per token for mixed en/zh content."""
    return len(text) // 3
```

It's not accurate; for real accuracy you'd bring in a tokenizer. But the compression decision doesn't need an exact value, it needs a sense of "roughly what fraction are we at," and dividing by 3 is good enough for mixed English/Chinese content, plus it's zero-dependency, zero-overhead. Between "good enough" and "precise but heavy," it clearly chose the former here. Where to be crude and where to be exacting is part of engineering taste.

## Layer one: old tool outputs have a shelf life

The first layer, `_snip_tool_outputs`, is the cheapest, calling no model, pure text processing. It snips tool results over 1500 characters down to just the first three and last three lines:

```python
content = m.get("content", "")
if len(content) <= 1500:
    continue
lines = content.splitlines()
if len(lines) <= 6:
    continue
snipped = (
    "\n".join(lines[:3])
    + f"\n... ({len(lines)} lines, snipped to save context) ...\n"
    + "\n".join(lines[-3:])
)
m["content"] = snipped
```

Behind this layer is an insight I find rather beautiful: tool output has a shelf life.

Those two hundred lines of matches a grep spat out twenty rounds ago were very useful at the time; the model used them to locate the code. But by now the model long ago finished with that result and made the corresponding edit, and those two hundred lines have become pure placeholder garbage, kept only to hog window. Snipping them to a few head and tail lines preserves the clue "a search happened here, roughly these files," while throwing away the vast majority of the dead weight. Fresh information is valuable, stale information is cheap, and compression should compress the stale first. Public teardowns call this layer HISTORY_SNIP, doing the same thing.

Why snip head and tail rather than just the head? Because a command's most useful information is often at both ends: the head is what it's doing, the tail is the result and the error. The big middle of the process can usually be dropped. This "keep head and tail, discard the middle" choice is of a piece with the bash output truncation from the last piece.

## Layer two: have the model write a summary of the old conversation

Layer one only compresses tool output; it can't budge the conversation itself. When the window climbs to 70%, layer two `_summarize_old` steps in: hand the whole old conversation to the model to write a summary, keeping only the most recent 8 messages untouched.

```python
split = self._safe_split(messages, keep_recent)
old = messages[:split]
tail = messages[split:]

summary = self._get_summary(old, llm)

messages.clear()
messages.append({
    "role": "user",
    "content": f"[Context compressed - conversation summary]\n{summary}",
})
messages.append({
    "role": "assistant",
    "content": "Got it, I have the context from our earlier conversation.",
})
messages.extend(tail)
```

The old conversation gets replaced by a single user message saying "this is a summary of the earlier conversation," plus an assistant response of "got it," and then the untouched recent messages are appended. The summary itself is generated by `_get_summary`, whose instruction to the model is tightly focused: keep the file paths that were changed, the key decisions made, the errors encountered, and the current task state; drop the verbose command output, the code listings, and the back-and-forth chatter. This is exactly what genuinely needs to be remembered in a long task.

If there's no model available (or the summary call itself fails), it degrades to `_extract_key_info`, using regex to pull out file paths and lines containing "error" to stitch a crude summary. Graceful degradation again: better a crude summary than letting this compression step drag the whole session down.

## The trap that's bound to bite you: orphaned tool messages

Now the centerpiece of this piece, also a trap I genuinely stepped on while polishing this project.

Recall the iron rule from piece one: an assistant message carrying `tool_calls` must be followed by paired `tool` replies, and the API rejects it if even one is missing. The essence of compression is cutting once at some position in the history, compressing what's before and keeping what's after. Here's the question: what if that cut lands right in the middle of a group of tool calls?

Picture the history as this stretch: assistant initiates tool calls, immediately followed by the corresponding tool replies. If the "keep the most recent N" boundary happens to fall on a tool reply, then the kept tail begins with a tool message while the assistant message that produced it got cut to the front and compressed into the summary, gone. This tool reply is orphaned; there's no matching tool_calls before it. Send that orphan out on the next request and the API rejects it on the spot. Your compression logic, meant to save the day, has killed the session with its own hands.

`_safe_split` exists to prevent this. Before cutting, it walks the boundary backward until the message at the boundary is no longer a tool:

```python
@staticmethod
def _safe_split(messages: list[dict], keep_recent: int) -> int:
    """Index where the kept tail should start.

    Walk the boundary back so a 'tool' result is never separated from the
    assistant message whose tool_calls produced it - an orphaned tool
    message has no preceding tool_calls and OpenAI-compatible APIs reject it.
    """
    split = max(0, len(messages) - keep_recent)
    while split > 0 and messages[split].get("role") == "tool":
        split -= 1
    return split
```

Just one `while` loop walking back. The logic couldn't be shorter, but without it compression is a time bomb that stays quiet normally and goes off exactly during a long conversation, with the window tight, at the moment it least should. Both layer two and layer three go through `_safe_split` when cutting, never `len - keep_recent` directly.

This trap is worth remembering, because it has every feature of a "hidden bug": it depends on an invariant spanning multiple messages (a tool must immediately follow its tool_calls), that invariant is written nowhere conspicuous, it doesn't trigger normally, and it only surfaces at the specific moment when "the cut point happens to land in the middle of a tool call." This kind of bug is hard to spot by staring at a single function; you have to hold both "the compression logic" and "the API's pairing constraint" in your head at once to realize they'll clash in some corner. CoreCoder nails this invariant down with two tests: `test_safe_split_never_orphans_a_tool_message` checks the cut point doesn't land on a tool, and `test_compress_never_leaves_an_orphan_tool_reply` checks that after a full round of compression every tool reply still immediately follows its tool_calls. Writing this kind of test is essentially solidifying an invariant hidden in your head into code, so nobody breaks it carelessly later.

## Layer three: the last resort

When the window climbs to 90%, it means the first two layers didn't compress enough, and layer three `_hard_collapse` is the emergency brake: keep only the last few messages plus a summary, collapse everything else. It likewise goes through `_safe_split` to guarantee no orphans. This layer rarely fires; its reason to exist is "in case the first two layers aren't enough, at least don't let the agent hit the wall and die outright," preferring to discard more context to keep the session alive.

## Compared with Claude Code

The difference between four layers and three is mainly that Claude Code adds a cache-backed micro-compression (microcompact) and a periodic background auto-compression, more refined as engineering. But the core idea, "layered, lightest to heaviest, lazily triggered, compress the stale first," is identical in both. CoreCoder compresses it into three layers, just enough for you to see clearly what each layer solves and what it costs, without drowning in cache and scheduling details.

This piece also answers a question from the opening: why does an agent occasionally "forget"? Because it really does forget; compression is lossy, and the details summarized away are gone. A good compression strategy isn't about losing nothing, it's about losing smart, dropping the things that have run out of shelf life first.

## What this piece leaves you with

- The context window is the agent's hardest physical constraint, and coding tasks are prolific token producers, so hitting the wall is only a matter of time.
- Compression should be layered, lightest to heaviest, lazily triggered: what you can save with pure-text truncation, don't spend an LLM summary on.
- Tool output has a shelf life, and stale output is the priority compression target. Fresh information is valuable, stale information is cheap.
- The orphaned tool message is a textbook hidden bug: it depends on a cross-message invariant, doesn't act up normally, and only blows up when the cut point lands in the middle of a tool call. Nailing this kind of invariant down as a test is the right way to fight this class of bug.
- Compression is lossy, and an agent "forgetting" is its inherent cost. A good strategy isn't losing nothing, it's losing smart.

Next piece, we return to another spot skipped in piece one: when the model returns several tool calls at once, how to run them concurrently, and when it can open a sub-agent to share the load.
