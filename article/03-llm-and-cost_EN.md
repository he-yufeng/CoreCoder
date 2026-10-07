# Plug in any model, and get the bill right while you're at it

The last two pieces covered the loop and the tools, the agent's hands and feet. This piece covers the brain's interface: how the model gets plugged in, how streaming output is handled, how to survive a provider acting up, and a question many tutorials skip but that you'll care about on day one after going live, namely how much this round actually cost.

The file is `corecoder/llm.py`, 349 lines, the largest single file in the engine. It's large because it carries, on your behalf, all the inelegant parts of dealing with a real API.

## A bet: everyone looks like OpenAI

The comment at the top of `llm.py` lays the whole design's bet out plainly:

> Since most providers (DeepSeek, Qwen, Kimi, GLM, Ollama, etc.) expose an OpenAI-compatible interface, we just use the openai SDK. Switching providers only takes changing `OPENAI_BASE_URL` and `OPENAI_API_KEY`, and that's it.

As of 2026 this bet is basically a sure thing. OpenAI's `/v1/chat/completions` shape has become the de facto standard, and the vast majority of model services at home and abroad either are natively compatible or offer a compatible endpoint. So CoreCoder's workhorse `LLM` class is essentially a thin wrapper around the official openai SDK. To go from OpenAI to DeepSeek you change not a line of code, just two environment variables:

```bash
export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com CORECODER_MODEL=deepseek-chat
```

This "one interface catches everyone" choice is one big reason CoreCoder can stay this small. It writes no adapter for each provider, because it bets everyone will converge toward OpenAI's shape.

So what about the incompatible ones, like AWS Bedrock or Google Vertex? There's a `LiteLLM` subclass at the end of `llm.py` as a fallback, covered later. First the main path.

## Streaming output, a bit more trouble than you'd think

`LLM.chat()` sends the messages out with `stream=True`, then receives them chunk by chunk. Text is easy: a chunk arrives, you concatenate it. The real trouble is tool calls, because a tool call's arguments are streamed too; one call's JSON arguments get sliced into several fragments that arrive across multiple chunks, and you have to stitch them back together in the order of the call.

CoreCoder stitches with a dictionary keyed by index:

```python
tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}

for chunk in stream:
    # ...
    if delta.tool_calls:
        for tc_delta in delta.tool_calls:
            idx = tc_delta.index
            if idx not in tc_map:
                tc_map[idx] = {"id": "", "name": "", "args": ""}
            if tc_delta.id:
                tc_map[idx]["id"] = tc_delta.id
            if tc_delta.function:
                if tc_delta.function.name:
                    tc_map[idx]["name"] = tc_delta.function.name
                if tc_delta.function.arguments:
                    tc_map[idx]["args"] += tc_delta.function.arguments
```

Notice that `+=`. The argument string is accumulated, because only a slice of it arrives at a time. Once the stream finishes, each call's accumulated argument string gets `json.loads`-ed into a real dictionary:

```python
for idx in sorted(tc_map):
    raw = tc_map[idx]
    try:
        args = json.loads(raw["args"])
    except (json.JSONDecodeError, KeyError):
        args = {}
    parsed.append(ToolCall(id=raw["id"], name=raw["name"], arguments=args))
```

There's a defense here: if the accumulated argument string isn't valid JSON (the model occasionally emits half-finished or malformed arguments), instead of crashing the whole flow it degrades to an empty dictionary `{}`, lets the call proceed with empty arguments, and leaves the argument validation from the last piece to hand the model a "your arguments are wrong" reply. In stream parsing, "bad data should degrade gracefully" is a recurring theme, because the stream can hand you something incomplete at any moment.

The text part also supports an `on_token` callback, fired every time a slice of text arrives, which the CLI uses to implement the typewriter effect, letting you watch words appear one by one instead of waiting tens of seconds for a whole block to pop out.

## How tokens get counted

To compute cost, you first need to know how many tokens each call used. CoreCoder doesn't estimate on its own; it asks the provider for the exact number. The way is to add a `stream_options` to the request:

```python
params["stream_options"] = {"include_usage": True}
```

With this added, the provider returns `usage` in the last chunk of the stream, holding `prompt_tokens` and `completion_tokens`. The code catches it inside the loop:

```python
if chunk.usage:
    # some providers send usage with null fields; coerce to 0 so the
    # running totals below don't blow up on int + None
    prompt_tok = chunk.usage.prompt_tokens or 0
    completion_tok = chunk.usage.completion_tokens or 0
```

That `or 0` isn't redundant. Some providers send `usage` back but with null fields, and if you take it straight into the running total, `int + None` throws and brings the whole session down. One `or 0` flattens this dirty data. Another example of "a real API won't be as clean as the docs," where the person writing the wrapper layer has to pave over these pits for the layers above.

The counted tokens accumulate into `total_prompt_tokens` and `total_completion_tokens`, which the CLI's `/tokens` command can query at any time.

## The provider acts up, and we ride it out ourselves

This is the setup planted at the end of the last piece. The main loop in `agent.py` has no retry logic at all, because retry was pushed down to this layer. `_call_with_retry` is responsible for riding out transient failures:

```python
def _call_with_retry(self, params: dict, max_retries: int = 3):
    """Retry on transient errors with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return self.client.chat.completions.create(**params)
        except (RateLimitError, APITimeoutError, APIConnectionError):
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            time.sleep(wait)
        except APIError as e:
            # retry 5xx server errors but not 4xx; base APIError has no
            # status_code so read it defensively
            status_code = getattr(e, "status_code", None)
            if status_code and status_code >= 500 and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
```

The logic is classic exponential backoff: failures that are obviously transient, like rate limiting, timeout, and connection errors, get retried after 1, 2, 4 seconds, and after three tries it gives up and raises. Server-side 5xx is retried too, but client-side 4xx (bad arguments, auth failure, that sort) is never retried, because retrying a hundred times gives the same result and only wastes the wait.

This retry logic is worth calling out specifically, because "a project this small surely has no room to bother with retries" is a natural misconception, and the truth is the opposite. Retry is not only present, it's thought through quite carefully, even defending with `getattr` against the base `APIError` possibly lacking a `status_code` attribute (different SDK versions have different exception hierarchies, and grabbing the attribute by force would blow up).

What CoreCoder didn't do is Claude Code's heavier graceful degradation: auto-switching to a fallback model on persistent server-side 529s, and a dollar-denominated budget cap. It genuinely didn't do these two, but the reason isn't oversight, it's a tradeoff. A fallback model involves the "which model can stand in for which" policy that's hard to unify across providers, and a dollar budget requires maintaining a price table that keeps changing. CoreCoder did the part it could implement cleanly (backoff retry) and left the part that would drag in a lot of provider-specific logic (fallback, hard budget) for you to add as needed. Knowing "what it did" and "what it deliberately didn't do" is far more useful than a vague "it's crude."

There's also an easily overlooked coordination detail. `stream_options` is an OpenAI extension that some providers don't recognize and answer with a 400. CoreCoder handles it like this:

```python
try:
    stream = self._call_with_retry(params)
except BadRequestError:
    params.pop("stream_options", None)
    stream = self._call_with_retry(params)
```

Catch `BadRequestError` (400), drop `stream_options`, and try once more. The comment specifically explains why the fallback is only here and not folded into `_call_with_retry`: because `_call_with_retry` has already retried the transient errors, and stuffing this "drop a param and retry" in there too would double the retry count. Two kinds of retry, one for "the param was rejected" and one for "a transient failure," have different causes, so they stay separate and uncoupled. This kind of accounting for "why this code is here and not there" is the most valuable thing when reading source.

## The OpenAI-incompatible ones go to LiteLLM

The `LiteLLM` subclass is the escape hatch for providers that don't speak the OpenAI-compatible interface. It inherits `LLM` but bypasses the parent constructor's step of creating an openai client, handing the request instead to the `litellm` library to route; litellm supports hundreds of providers:

```python
class LiteLLM(LLM):
    def _call_with_retry(self, params, max_retries=3):
        import litellm
        params["drop_params"] = True   # drop unsupported params instead of erroring
        if self.api_key:
            params["api_key"] = self.api_key
        if self.base_url:
            params["api_base"] = self.base_url
        # ...the same exponential backoff...
```

Set `CORECODER_PROVIDER=litellm` and you can use litellm's model strings, like `anthropic/claude-3-haiku`, `bedrock/anthropic.claude-v2`, `vertex_ai/gemini-pro`. `drop_params=True` is a thoughtful switch: when a provider doesn't support some param, it gets dropped instead of erroring, sparing you from trimming params per provider. The vast majority of people get by with the main path; LiteLLM is the back road for "in case your provider is too idiosyncratic."

## Working out the cost

Knowing the tokens, computing cost is just a table lookup and a multiply. `llm.py` has a table priced per million tokens (input price, output price):

```python
_PRICING = {
    "gpt-5.5": (5, 30),           # CoreCoder's default model
    "deepseek-chat": (0.27, 1.10),
    "claude-sonnet-4-6": (3, 15),
    "kimi-k2.5": (0.6, 3),
    # ...
}
```

That table is only the default. `_load_pricing()` overlays it with `~/.corecoder/pricing.json` in the same shape, so a new model or a moved price lands without waiting for a release; a missing or broken file quietly means the built-ins.

The `estimated_cost` property takes the accumulated tokens and multiplies by the matching unit price:

```python
@property
def estimated_cost(self) -> float | None:
    pricing = _load_pricing().get(self.model)
    if not pricing:
        return None
    input_rate, output_rate = pricing
    return (
        self.total_prompt_tokens * input_rate / 1_000_000
        + self.total_completion_tokens * output_rate / 1_000_000
    )
```

Notice the return type is `float | None`. For a model not in the table, it doesn't guess, it honestly returns `None`, and the CLI, seeing `None`, just doesn't show a price rather than inventing a number to fool you. This is a small but important honesty: better to say "I don't know" than to hand you a cost that looks precise but is made up. The CLI's `/tokens` command shows it:

```
Tokens: 12043 prompt + 3201 completion = 15244 total  (~$0.0621)
```

Of course this is only an estimate; the price table goes stale, and cache discounts and batch pricing aren't counted. But for a sense of "roughly how much money did this run burn," at this order of magnitude it's enough. An agent that gives you a feel for cost, versus one that makes you flinch at the bill at month's end, are two different experiences.

## Where config comes from

Finally, a thread through `config.py` (55 lines). It reads config from environment variables with a sensible priority:

```python
api_key = (
    os.getenv("CORECODER_API_KEY")
    or os.getenv("OPENAI_API_KEY")
    or os.getenv("DEEPSEEK_API_KEY")
    or ""
)
```

The dedicated variable wins, then it falls back to the generic `OPENAI_API_KEY`, then to `DEEPSEEK_API_KEY`. It also walks up from the current directory to the home directory loading a `.env` file (`override=False`, so it won't overwrite the real environment variables you've already set). This way you drop a `.env` in the project directory and it works on entry, no need to export every time. A small thing, but handy.

## What this piece leaves you with

- The bet that "most providers are OpenAI-compatible" lets the whole provider layer thin out to a single wrapper over the openai SDK, and switching providers is two environment variables.
- In streaming output, tool-call arguments arrive in fragments, to be accumulated by index then `json.loads`-ed, and bad data must degrade gracefully.
- Retry belongs in the provider layer, not stuffed into the main loop. Exponential backoff retries only transient failures and 5xx, never 4xx.
- CoreCoder did backoff retry and deliberately skipped the fallback model and dollar hard budget, because the latter two drag in a lot of provider-specific logic. Tell "didn't do" apart from "deliberately didn't do."
- Cost estimation returns `None` for an unknown model rather than inventing a falsely precise number. Honesty beats looking good.

Next piece, we face the agent's hardest physical constraint: the context window is only so big, and how a long task fits inside it.
