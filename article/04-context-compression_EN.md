# Part 4: Limited Window, Unlimited Tasks

128K tokens sounds like a lot. But let's actually do the math.

Take a slightly complex programming task—"Refactoring the error handling of this module." The LLM first needs to read the relevant files: three or four files, each a few hundred lines long, roughly 4000-8000 tokens. Then it modifies the files, runs tests, checks errors, and modifies again. Each round of tool calls outputs anywhere from a few hundred tokens (reading a small file) to several thousand tokens (running a complete `npm test` log). After a dozen rounds, the tool output alone might take up 50K-80K tokens. Add system prompts (usually 3K-5K), historical conversations, LLM replies, and 128K is almost gone.

The first time I used Claude Code for a big project, it suddenly said "I need to compress the context," and I thought it was just simple truncation. After reading the source code, I discovered that Claude Code's context management is the most sophisticated I've ever seen: **four compression mechanisms of different granularities work together**, with later ones only used if earlier ones fall short.

---

## Four-Layer Strategy

I found four relevant Feature Flags in the source code:

``` 
HISTORY_SNIP        → First Layer: Tool output truncation
CACHED_MICROCOMPACT → Second Layer: cached LLM summaries
CONTEXT_COLLAPSE    → Third Layer: structured archiving
REACTIVE_COMPACT    → Fourth Layer: background automatic compression (i.e., Autocompact)
```

### First Layer: HISTORY_SNIP — Noise Removal

The most prone to bloat isn't user messages or LLM replies, but **tool output**.

A `grep` search returns 200 matching lines, but the LLM might only use 3 of them. The remaining 197 lines are pure noise that waste valuable context space as are useless for subsequent decisions.

HISTORY_SNIP iterates through all results with `role: "tool"` in the history messages. If the content exceeds a threshold, it replaces it with a simplified version. The simplification strategy is to retain the first and last few lines (the most useful information is usually in the initial command echo and the final error/summary message), replacing the rest with `[snipped N lines]`.

```
Before: 
[tool result] (482 lines) 
src/auth.py:12: import jwt 
src/auth.py:34: jwt.decode(token, SECRET) 
src/auth.py:56: jwt.encode(payload, SECRET) 
... (479 more lines of grep results)

after: 
[tool result] (snipped to 6 lines) 
src/auth.py:12: import jwt 
src/auth.py:34: jwt.decode(token, SECRET) 
src/auth.py:56: jwt.encode(payload, SECRET) 
[snipped 476 lines] 
src/utils/crypto.py:89: verify_jwt(token) 
src/utils/crypto.py:102: `refresh_jwt(token)`
```

With this simple compression method no critical information is lost (start and end are preserved), and the effect is immediate. A single grep output was compressed from 2000 tokens to 100 tokens without losing anything. This is the lowest-cost method too as no LLM call is performed in this layer.

The `_snip_tool_outputs()` function in NanoCoder's `context.py` is the implementation for this layer.

### Second Layer: `CACHED_MICROCOMPACT` — costs tokens!

If the first layer is still too long (because of multiple rounds, each with valid information), the second layer is activated.

This layer takes old dialogue fragments (such as the earliest 10 rounds of interaction) and sends them to the LLM for a dedicated summary call:

```
System: "Compress this dialogue into key information.
        Retain: file path, decisions made, errors encountered, current task status.
        Discard: lengthy tool output, repetitive discussions, formatted code."

User:   [Concatenated text of the 10 rounds of old dialogue]
```

The summary returned by the LLM is typically only 1/5 to 1/10 of the original dialogue. This summary is then used to replace the original 10 rounds of old dialogue.

"Cached" means that the summary is cached. If compression is needed again, the previous summary is used directly without re-calling the LLM. This avoids spending money on summarizing in each iteration.

This layer also utilizes the Anthropic API's `cache_deleted_input_tokens` capability—marking certain tokens as "deleted" at the API's caching level, without consuming cached token quotas. This effectively frees up space in the cache without altering the message content.

NanoCoder's `_summarize_old()` implements the core logic: it uses the same LLM for summarization, and if the LLM call fails, it falls back to extracting key information (file path, error message) based on regular expressions.

### Third Layer: CONTEXT_COLLAPSE — Structured Archiving

If the first two layers are insufficient (the user handles multiple different tasks within a very long session), the third layer is activated.

While the previous layer simply shortens the old conversation, preserving its structure. CONTEXT_COLLAPSE **completely replaces** the old conversation with a structured summary, similar to a Git log—showing what was done in each round, the conclusion, and which files were modified.

```
[Context collapsed - 30 turns summarized]

Turn 1-5: Read auth module, identified 3 functions without error handling
Turn 6-12: Added try/except to verify_token(), refresh_token(), decode_payload()
Turn 13-15: Ran tests, found regression in test_expired_token
Turn 16-20: Fixed test, all 47 tests passing
Turn 21-25: Updated API documentation for new error responses
Turn 26-30: Code review suggestions applied

Files modified: src/auth.py, tests/test_auth.py, docs/api.md
Current state: All changes committed, ready for PR
```

This layer will lose details. But it's better than simple truncation: truncation cuts in chronological order (the earliest is discarded first), while CONTEXT_COLLAPSE at least retains the key decision points of each round. LLM doesn't know the specific code changes, but it knows "the error handling in auth.py was modified before," and won't repeat what it has already done.

NanoCoder's `_hard_collapse()` implements this layer.

### Fourth Layer: Autocompact — Automatic Compactor

Claude Code has a `/compact` command that allows users to actively trigger compression. But REACTIVE_COMPACT (also called Autocompact) is **automatic**:

The system checks the current token usage before each API call. If it's close to the limit, compression is automatically performed without the user's awareness. Users don't need to worry about token management.

NanoCoder's `maybe_compress()` uses this mechanism—automatically checking at the beginning of each iteration of `agent.chat()` and after each round of tool execution.

---

## Engineering Trade-offs in Compression

The hardest part of context compression isn't "how to compress," but "what to compress."

**What information absolutely cannot be lost?** 
- File Paths: LLM needs to know which files have been edited previously; otherwise, it might read or overwrite files repeatedly. 
- Critical decisions made: if instructions like "The user said not to modify config.yaml" are suppressed, LLM might violate them. 
- Unresolved Errors: if information about bugs being processed is lost, LLM will start the investigation from scratch.

Claude Code's digest prompt explicitly lists these reserved items. NanoCoder does the same.

**How ​​many tokens does the digest itself occupy?**

If the digest is too long, compression is wasted. Claude Code limits the output length of digest calls using the `max_tokens` parameter. NanoCoder limits the digest call prompt to 15,000 characters.

**Will LLM "forget" its tasks after compression?**

This is the biggest risk. Suppose you write, ‘Modify the auth module and then run a full test.’ If the first part gets carried out by the LLM, but the second part gets absorbed into a summary during compression, the model might end up forgetting to run the tests.

Claude Code's strategy emphasizes "preserving user-explicitly requested operations and constraints" in the summary prompt. However, this isn't 100% reliable—the summary LLM itself can make mistakes. This is a known imperfection, and there's currently no perfect solution.

**Which model to use for summarizing?**

Claude Code uses the same model. The cost of one summary call is approximately equal to the cost of one normal conversation. NanoCoder works similarly. If you want to save money, you can use a cheaper model specifically for summarizing (e.g., using DeepSeek as the primary model but GPT-4o-mini for summarizing).

---

## Different "Shelf Life" of Information

After reading the context management code, my biggest takeaway is: **Different information has different shelf lives and should be handled with different strategies.**

The intermediate output of the tool becomes useless after a few rounds (grep results are no longer needed once you find the file you want to modify). But the user's described background information may need to be preserved throughout the entire session. The previous thought process of LLM can be discarded, but the decisions it made ("I choose to use try/except instead of if/else") should be retained.

Claude Code's four-layer strategy is essentially a hierarchical approach based on "shelf life": the first layer discards the shortest-lived elements (lengthy tool outputs), and the last layer addresses the longest-lived elements (dialogue structure and decision history).

---

> This article is the 4th in the [Claude Code Source Code Guide](00-index_EN.md) series. Accompanying implementation: [NanoCoder](https://github.com/he-yufeng/NanoCoder)