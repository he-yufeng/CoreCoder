"""LLM provider layer - thin wrapper over OpenAI-compatible APIs.

Since most providers (DeepSeek, Qwen, Kimi, GLM, Ollama, etc.) expose an
OpenAI-compatible endpoint, we just use the openai SDK directly.  Switch
provider by changing OPENAI_BASE_URL + OPENAI_API_KEY. That's it.

For providers that are NOT OpenAI-compatible (AWS Bedrock, Google Vertex,
etc.), use the LiteLLM backend which routes to 100+ providers through a
single unified interface. Set CORECODER_PROVIDER=litellm.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from openai import APIConnectionError, APIError, APITimeoutError, BadRequestError, OpenAI, RateLimitError


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_content: str | None = None

    @property
    def message(self) -> dict:
        """Convert to OpenAI message format for appending to history."""
        msg: dict = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
            if self.reasoning_content is not None:
                msg["reasoning_content"] = self.reasoning_content
        return msg


# pricing per million tokens: (input, output)
# sources: openai.com/api/pricing, api-docs.deepseek.com, platform.claude.com,
#          platform.moonshot.ai, alibabacloud.com/help/en/model-studio
_PRICING = {
    # OpenAI - current flagships
    "gpt-5.5": (5, 30),
    "gpt-5.4": (2.5, 15),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.2, 1.25),
    "o4-mini": (1.1, 4.4),
    # OpenAI - previous gen (still widely used)
    "gpt-4.1": (2, 8),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o": (2.5, 10),
    "gpt-4o-mini": (0.15, 0.6),
    # DeepSeek
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    # Anthropic Claude
    "claude-opus-4-6": (5, 25),
    "claude-sonnet-4-6": (3, 15),
    "claude-haiku-4-5": (1, 5),
    # Alibaba Qwen
    "qwen3-max": (0.78, 3.9),
    "qwen3-plus": (0.26, 0.78),
    "qwen-max": (0.78, 3.9),
    # Moonshot Kimi
    "kimi-k3": (3, 15),
    "kimi-k2.5": (0.6, 3),
}

# same shape as the table above; entries here win over the built-ins
PRICING_FILE = Path.home() / ".corecoder" / "pricing.json"


def _load_pricing() -> dict:
    """Built-in prices overlaid with the user's pricing.json, so a new model
    or a price change lands without waiting for a release. A missing or
    broken file just means the built-ins."""
    pricing = dict(_PRICING)
    try:
        overrides = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        pricing.update({m: (float(r[0]), float(r[1])) for m, r in overrides.items()})
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass
    return pricing


def _adapt_rejected_param(params: dict, exc: BadRequestError) -> bool:
    """Translate or drop one parameter a provider rejected with a 400.

    Newer OpenAI models (gpt-5, o-series) accept only ``max_completion_tokens``
    and the default temperature; the 400 message quotes the offender
    (``'max_tokens'``). Keep that name quoted: the rejection text
    for ``max_completion_tokens`` must not re-trigger the translation.
    Returns False when nothing recognizable was rejected.
    """
    message = str(exc).lower()
    if "'max_tokens'" in message and "max_tokens" in params:
        params["max_completion_tokens"] = params.pop("max_tokens")
        return True
    if "temperature" in message and "temperature" in params:
        params.pop("temperature")
        return True
    return False


class LLM:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        fallback_models: list[str] | tuple[str, ...] | None = None,
        on_fallback=None,
        **kwargs,
    ):
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.fallback_models = tuple(dict.fromkeys(m.strip() for m in fallback_models or () if m.strip()))
        self.on_fallback = on_fallback
        self.extra = kwargs  # temperature, max_tokens, etc.
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._usage_by_model: dict[str, list[int]] = {}
        self._usage_lock = threading.Lock()

    @property
    def estimated_cost(self) -> float | None:
        """Rough cost estimate in USD, using the model that served each call."""
        usage = getattr(self, "_usage_by_model", None)
        if usage is not None:
            with self._usage_lock:
                model_usage = [(model, *counts) for model, counts in usage.items()]
                if not model_usage:
                    model_usage = [(self.model, self.total_prompt_tokens, self.total_completion_tokens)]
        else:
            # Keep lightweight LLM.__new__ embedders and older pickled objects valid.
            model_usage = [(self.model, self.total_prompt_tokens, self.total_completion_tokens)]
        pricing = _load_pricing()
        cost = 0.0
        for model, prompt_tokens, completion_tokens in model_usage:
            if not prompt_tokens and not completion_tokens:
                continue
            rates = pricing.get(model)
            if rates is None:
                return None
            input_rate, output_rate = rates
            cost += prompt_tokens * input_rate / 1_000_000 + completion_tokens * output_rate / 1_000_000
        return cost

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        if isinstance(exc, (RateLimitError, APITimeoutError, APIConnectionError)):
            return True
        return isinstance(exc, APIError) and (getattr(exc, "status_code", None) or 0) >= 500

    def _create_stream(self, params: dict):
        """Create one stream, adapting only parameters the provider names in a 400."""
        while True:
            try:
                return self._call_with_retry(params)
            except BadRequestError as e:
                if _adapt_rejected_param(params, e):
                    continue
                if "stream_options" not in params:
                    raise
                params.pop("stream_options")

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token=None,
        on_reasoning=None,
    ) -> LLMResponse:
        """Send messages, stream back response, handle tool calls.

        on_reasoning, when given, receives reasoning deltas from thinking
        models. Reasoning stays display-only for plain answers and OpenRouter;
        DeepSeek/Kimi reasoning_content is kept on tool-call turns because
        their thinking protocols require it in the following request.
        """
        models = (self.model, *(m for m in self.fallback_models if m != self.model))
        last_err = None
        for model_index, model in enumerate(models):
            params: dict = {
                "model": model,
                "messages": messages,
                "stream": True,
                **self.extra,
                "stream_options": {"include_usage": True},
            }
            if tools:
                params["tools"] = tools

            try:
                stream = self._create_stream(params)
            except Exception as e:
                # Fallback is only for a request that never started streaming.
                # A 400 or an exhausted mid-stream retry must not be hidden by
                # replaying already visible output through another model.
                if model_index + 1 < len(models) and self._is_transient_error(e):
                    last_err = e
                    continue
                raise

            if model_index and getattr(self, "on_fallback", None):
                self.on_fallback(model)
            for attempt in range(3):
                try:
                    return self._drain(stream, on_token, on_reasoning, model=model)
                except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                    last_err = e
                except APIError as e:
                    # retry 5xx mid-stream drops but not 4xx
                    status_code = getattr(e, "status_code", None)
                    if not status_code or status_code < 500:
                        raise
                    last_err = e
                # Nothing has been executed yet, so re-issuing this same model's
                # request is safe. Create-time retries stay in _call_with_retry.
                if attempt < 2:
                    time.sleep(2**attempt)
                    stream = self._create_stream(params)
            raise last_err
        raise last_err

    def _drain(self, stream, on_token, on_reasoning, model: str | None = None) -> LLMResponse:
        """Consume one stream into an LLMResponse."""
        content_parts: list[str] = []
        reasoning_parts: list[str] | None = None
        tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}
        prompt_tok = 0
        completion_tok = 0

        for chunk in stream:
            # usage info comes in the final chunk; getattr-style reads stay safe
            # across OpenAI SDK objects and litellm's provider-varying shapes
            usage = getattr(chunk, "usage", None)
            if usage:
                # some providers send usage with null fields; coerce to 0 so the
                # running totals below don't blow up on int + None
                prompt_tok = getattr(usage, "prompt_tokens", 0) or 0
                completion_tok = getattr(usage, "completion_tokens", 0) or 0

            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta

            # thinking models stream their chain-of-thought separately; show
            # it when someone is listening, but never mix it into the reply.
            # reasoning_content is the DeepSeek/Kimi dialect, reasoning is
            # what aggregators like OpenRouter normalize it to
            reasoning_content = getattr(delta, "reasoning_content", None)
            if reasoning_content is not None:
                if reasoning_parts is None:
                    reasoning_parts = []
                reasoning_parts.append(reasoning_content)
            reasoning = reasoning_content or getattr(delta, "reasoning", None)
            if reasoning and on_reasoning:
                on_reasoning(reasoning)

            # accumulate text
            if getattr(delta, "content", None):
                content_parts.append(delta.content)
                if on_token:
                    on_token(delta.content)

            # accumulate tool calls across chunks
            if getattr(delta, "tool_calls", None):
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

        # parse accumulated tool calls
        parsed: list[ToolCall] = []
        for idx in sorted(tc_map):
            raw = tc_map[idx]
            try:
                args = json.loads(raw["args"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            parsed.append(ToolCall(id=raw["id"], name=raw["name"], arguments=args))

        with self._usage_lock:
            self.total_prompt_tokens += prompt_tok
            self.total_completion_tokens += completion_tok
            if hasattr(self, "_usage_by_model"):
                usage = self._usage_by_model.setdefault(model or self.model, [0, 0])
                usage[0] += prompt_tok
                usage[1] += completion_tok

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=parsed,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            reasoning_content=(
                "".join(reasoning_parts) if reasoning_parts is not None else None
            ),
        )

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
                # retry 5xx server errors but not 4xx; base APIError has no status_code so read it defensively
                status_code = getattr(e, "status_code", None)
                if status_code and status_code >= 500 and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise


class LiteLLM(LLM):
    """LLM backend via LiteLLM, supporting 100+ providers.

    Use this when your target provider is NOT OpenAI-compatible
    (AWS Bedrock, Google Vertex, Cohere, etc.) or when you want
    a single interface to switch between any provider by changing
    the model string.

    Set CORECODER_PROVIDER=litellm and use LiteLLM model strings
    like ``anthropic/claude-3-haiku``, ``bedrock/anthropic.claude-v2``,
    ``vertex_ai/gemini-pro``, etc.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        fallback_models: list[str] | tuple[str, ...] | None = None,
        on_fallback=None,
        **kwargs,
    ):
        # skip LLM.__init__ which creates an OpenAI client
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.fallback_models = tuple(dict.fromkeys(m.strip() for m in fallback_models or () if m.strip()))
        self.on_fallback = on_fallback
        self.extra = kwargs
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._usage_by_model: dict[str, list[int]] = {}
        self._usage_lock = threading.Lock()

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(
            keyword in message
            for keyword in ["rate_limit", "timeout", "connection", "500", "502", "503", "504", "529"]
        )

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        """Retry on transient errors with exponential backoff via litellm."""
        import litellm

        params["drop_params"] = True
        if self.api_key:
            params["api_key"] = self.api_key
        if self.base_url:
            params["api_base"] = self.base_url

        for attempt in range(max_retries):
            try:
                return litellm.completion(**params)
            except Exception as e:
                if self._is_transient_error(e) and attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise
