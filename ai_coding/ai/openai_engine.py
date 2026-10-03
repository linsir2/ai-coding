"""Direct chat-completions engine (R1a-2).

Replaces the OpenAI Agents SDK runner for the self-owned loop: one
``chat.completions.create(stream=True)`` call per ``step``, with the streamed
fragments assembled into a ``StepResult``.

Construction never touches the network, and the client is injectable so the
whole assembly path is unit-testable offline.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from ai_coding.ai.engine import ChatEngine, DeltaSink, EngineError
from ai_coding.ai.mapping import history_to_chat_messages
from ai_coding.ai.toolcalls import finalize_tool_calls, merge_tool_call_delta
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig
from ai_coding.domain.usage import as_int, cache_from_usage


def _usage_to_dict(usage: Any) -> dict[str, int]:
    """Normalize an SDK usage object into a plain flat dict.

    Missing keys are omitted.  R4a adds the prompt-cache figures: a trace is only
    allowed to claim "the prefix hit the cache" using the provider's own numbers,
    so whatever the endpoint reports has to survive this boundary
    (docs/r4-analysis.md 4.4).
    """
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    raw = _usage_mapping(usage)
    cached, written = cache_from_usage(raw)
    if cached is not None:
        out["cached_tokens"] = cached
    if written is not None:
        out["cache_write_tokens"] = written
    return out


def _usage_mapping(usage: Any) -> dict[str, Any]:
    """Best-effort view of a vendor usage object as a mapping.

    SDKs hand back pydantic models, and the cache figures hide in nested details,
    so the nested containers are flattened one level before normalisation.
    """
    data: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    for key, inner in (
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cached_tokens"),
    ):
        detail = getattr(usage, key, None)
        inner_value = getattr(detail, inner, None) if detail is not None else None
        as_int_value = as_int(inner_value)
        if as_int_value is not None:
            data.setdefault("cached_tokens", as_int_value)
    try:
        dumped = usage.model_dump() if hasattr(usage, "model_dump") else None
    except Exception:  # noqa: BLE001 — a vendor object that cannot dump is fine
        dumped = None
    if isinstance(dumped, dict):
        for key in (
            "cached_tokens",
            "cache_write_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "prompt_cache_hit_tokens",
            "prompt_tokens_details",
        ):
            if key in dumped:
                data.setdefault(key, dumped[key])
    return data


class OpenAIChatEngine(ChatEngine):
    """A ``ChatEngine`` backed by a single streaming chat-completions call."""

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        client: AsyncOpenAI | None = None,
        stream_usage: bool = True,
    ) -> None:
        if not isinstance(model_config, ModelConfig):
            raise TypeError("model_config must be a ModelConfig")
        self.model_config = model_config
        self._stream_usage = stream_usage
        # Ownership matters: we only close a client we created ourselves.
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            base_url=model_config.base_url,
            api_key=model_config.api_key,
            timeout=model_config.timeout,
            max_retries=0,
        )

    @staticmethod
    def build_request_messages(
        messages: list[ChatMessage],
        instructions: str | None,
    ) -> list[dict[str, Any]]:
        """Map history to chat messages and prepend the system prompt if any.

        Nothing is appended here: ``messages`` already contains the user turn.
        """
        request = history_to_chat_messages(messages)
        if instructions:
            request.insert(0, {"role": "system", "content": instructions})
        return request

    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]],
        instructions: str | None = None,
        on_delta: DeltaSink | None = None,
    ) -> StepResult:
        cfg = self.model_config
        kwargs: dict[str, Any] = {
            "model": cfg.name,
            "messages": self.build_request_messages(messages, instructions),
            "stream": True,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "top_p": cfg.top_p,
        }
        # An empty ``tools`` list is rejected by some OpenAI-compatible endpoints,
        # so the parameter is omitted entirely when there is nothing to offer.
        if tools:
            kwargs["tools"] = tools
        if self._stream_usage:
            kwargs["stream_options"] = {"include_usage": True}

        acc: dict[int, dict[str, str]] = {}
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, int] | None = None

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                # The include_usage trailer has empty ``choices`` but real usage,
                # so usage must be read before the choices check.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = _usage_to_dict(chunk_usage)

                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                choice = choices[0]

                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish_reason = str(reason)

                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    text_parts.append(str(content))
                    if on_delta is not None:
                        on_delta(str(content))

                # R1-4: thinking arrives on sibling delta fields
                # (``reasoning_content`` on DeepSeek-reasoner-likes,
                # ``reasoning`` on others) and is joined exactly like prose —
                # completed text per step, deliberately not streamed (adding
                # an ``on_reasoning`` sink would break every engine fake).
                # Endpoints that send no thinking yield no reasoning, ever.
                thinking = getattr(delta, "reasoning_content", None)
                if thinking is None:
                    thinking = getattr(delta, "reasoning", None)
                if thinking:
                    reasoning_parts.append(str(thinking))

                # Text and tool-call fragments can arrive in the same chunk, so
                # this is not an ``elif``.
                tool_deltas = getattr(delta, "tool_calls", None)
                if tool_deltas:
                    for tool_delta in tool_deltas:
                        merge_tool_call_delta(acc, tool_delta)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 — engine boundary, re-raised typed
            raise EngineError(str(exc)) from exc

        return StepResult(
            text="".join(text_parts) or None,
            tool_calls=finalize_tool_calls(acc),
            finish_reason=finish_reason,
            usage=usage,
            reasoning="".join(reasoning_parts) or None,
        )

    async def aclose(self) -> None:
        """Close the client, but only if this engine created it."""
        if self._owns_client:
            await self._client.close()


__all__ = ["OpenAIChatEngine"]
