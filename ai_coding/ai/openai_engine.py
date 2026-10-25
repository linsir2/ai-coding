"""Direct chat-completions engine (R1a-2).

Replaces the OpenAI Agents SDK runner for the self-owned loop: one
``chat.completions.create(stream=True)`` call per ``step``, with the streamed
fragments assembled into a ``StepResult``.

Construction never touches the network, and the client is injectable so the
whole assembly path is unit-testable offline.
"""

from __future__ import annotations

import asyncio
import enum
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from ai_coding.ai.engine import (
    ChatEngine,
    DeltaSink,
    EngineError,
    FallbackWorthwhile,
    RetryObserver,
)
from ai_coding.ai.mapping import history_to_chat_messages
from ai_coding.ai.toolcalls import finalize_tool_calls, merge_tool_call_delta
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig
from ai_coding.domain.usage import as_int, cache_from_usage


class FailureKind(enum.Enum):
    """What to do about one failed model call.

    The question each value answers is no longer just "wait or stop" but "wait,
    stop, or stop *this* model" — which is what lets a chain of providers be
    composed without any of them knowing about each other (M2).

    ``RATE_LIMITED`` earns its own value for the wait, not the verdict: a
    rate-limited provider states how long to wait and repeating it earlier earns
    another 429 (P0-1).  ``AUTH_ERROR`` earns its own because it is the one
    verdict where waiting is pointless but a different provider may still work.
    """

    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TRANSPORT = "transport"
    AUTH_ERROR = "auth_error"
    #: The prompt does not fit.  Not worth another model -- the same messages
    #: overflow every one of them, and ``ModelConfig`` carries no window to pick
    #: a bigger one with -- but worth compacting the history, which is what the
    #: loop does with this verdict (G5-1).
    CONTEXT_OVERFLOW = "context_overflow"
    FATAL = "fatal"


#: Substrings that identify a context overflow inside a 400/413 response.
#: A text match is the only signal available: none of the four reference
#: implementations gets a structured code from every provider, and all of them
#: fall back to the same thing (OpenCode keeps ~50 patterns, Hermes ~20).
#: Deliberately short -- a marker that misfires would rewrite the operator's
#: history for a failure compaction cannot fix.
_OVERFLOW_MARKERS = (
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "too many tokens",
    "prompt is too long",
    "request too large",
    "request_too_large",
    "input length",
    "reduce the length",
)


def _looks_like_context_overflow(exc: BaseException) -> bool:
    """True when a rejected request was rejected *for being too big*."""
    text = str(exc).lower()
    return any(marker in text for marker in _OVERFLOW_MARKERS)


#: Verdicts another provider could plausibly survive (M2 §5).  ``FATAL`` is
#: absent on purpose: it means the request itself is unacceptable, so every
#: provider would reject it identically and the next model would only burn its
#: own budget to learn nothing.  Kept beside the classifier so the two stay in
#: step.
WORTH_ANOTHER_MODEL = frozenset(
    {
        FailureKind.RATE_LIMITED,
        FailureKind.SERVER_ERROR,
        FailureKind.TRANSPORT,
        FailureKind.AUTH_ERROR,
    }
)


def _exhausted(kind: FailureKind, message: str) -> EngineError:
    """Pick the exception type for a model call that will not be retried here.

    One function, because the choice is the whole point of M2 and duplicating it
    at each ``raise`` is how the two paths drift: a stream that died after the
    operator saw text and a budget that ran out are both "this model is done",
    but only one of them may be taken over by another provider.

    The verdict travels on both spellings.  ``FallbackEngine`` reads the type to
    decide whether to try another model; the loop reads ``kind`` to decide
    whether to compact the history (G5-1) -- two independent decisions made from
    the same classifier, which is why neither may re-derive it from the message.
    """
    if kind in WORTH_ANOTHER_MODEL:
        return FallbackWorthwhile(message, kind=kind.value)
    return EngineError(message, kind=kind.value)


def classify_failure(exc: BaseException) -> FailureKind:
    """Sort a provider failure into a verdict.  Pure.

    Keyed on the SDK's own exception hierarchy and ``status_code`` rather than on
    named subclasses: an OpenAI-compatible gateway can answer 429 or 503 without
    the SDK picking a subclass, and the status is the fact that matters.
    Anything unrecognised is ``FATAL`` — the default has to be "do not spend
    three more round trips guessing".
    """
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return FailureKind.TRANSPORT
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 429:
            # A quota that is used up does not refill in `Retry-After` seconds:
            # waiting four times over burns four round trips to learn nothing.
            # Only when the provider *says* it is the quota -- a plain 429 is a
            # rate limit, and waiting is exactly right for it.
            #
            # ``str(exc)`` is not enough: the real SDK puts the message in the
            # exception text and the marker in the body, so a body-only check is
            # what makes this work on live traffic instead of only on the fake
            # one (measured: str() lacked "insufficient_quota", body had it).
            if _mentions_quota(exc):
                return FailureKind.FATAL
            return FailureKind.RATE_LIMITED
        if status >= 500:
            return FailureKind.SERVER_ERROR
        if status in (401, 403):
            return FailureKind.AUTH_ERROR
        if status in (400, 413) and _looks_like_context_overflow(exc):
            return FailureKind.CONTEXT_OVERFLOW
    return FailureKind.FATAL


def _mentions_quota(exc: BaseException) -> bool:
    """Does this 429 say the *quota* is spent, rather than "slow down"?

    The marker lives in the response body (``error.type``/``error.code``) on real
    SDK exceptions, and only sometimes in the message — so both are read.  A plain
    429 stays a rate limit: waiting is exactly right for it.
    """
    body = getattr(exc, "body", None)
    haystack = f"{exc} {body if body is not None else ''}".lower()
    return "insufficient_quota" in haystack or "exceeded your current quota" in haystack


def _retry_after(exc: BaseException) -> float | None:
    """The provider's own "come back in N seconds", in seconds.

    Only the numeric form is honoured.  HTTP also allows an HTTP-date, and
    parsing that would need a clock and a date parser to serve a value the turn
    deadline would overrule anyway; an unparsable header is simply absent.
    """
    response = getattr(exc, "response", None)
    raw = (getattr(response, "headers", None) or {}).get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def retry_delay(
    attempt: int,
    kind: FailureKind,
    *,
    base: float,
    cap: float,
    retry_after: float | None = None,
) -> float:
    """Seconds to wait before ``attempt`` (0-based).  Pure, and therefore testable.

    ``kind`` has no default: omitting it would silently drop the ``retry_after``
    branch, which is exactly the mistake worth making impossible.

    ``cap`` is the per-request timeout: waiting longer than one whole request
    would take is the turn deadline's decision, not this function's, and the
    engine cannot see the deadline.
    """
    if kind is FailureKind.RATE_LIMITED and retry_after is not None:
        return float(min(max(0.0, retry_after), cap))
    return float(min(base * (2**attempt), cap))


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
        on_retry: RetryObserver | None = None,
    ) -> None:
        if not isinstance(model_config, ModelConfig):
            raise TypeError("model_config must be a ModelConfig")
        self.model_config = model_config
        self._stream_usage = stream_usage
        # The engine is the deepest layer with no observer of its own, so a retry
        # is invisible unless it is handed someone to tell.  Optional by design:
        # every existing caller keeps working without one.
        self.on_retry = on_retry
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
        image_loader: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Map history to chat messages and prepend the system prompt if any.

        Nothing is appended here: ``messages`` already contains the user turn.
        ``image_loader`` (r17/T5) resolves message images to data URLs; the
        caller owns the workspace, so the loader travels with the call.
        """
        request = history_to_chat_messages(messages, image_loader=image_loader)
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
        image_loader: Any | None = None,
    ) -> StepResult:
        """One step, with bounded retries for a transient provider failure.

        The turn's own deadline is the outer bound: the loop wraps this call in
        ``run_with_deadline``, so a retry storm is abandoned at the deadline and
        this function never has to know what it is.
        """
        cfg = self.model_config
        kwargs: dict[str, Any] = {
            "model": cfg.name,
            "messages": self.build_request_messages(messages, instructions, image_loader),
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

        attempts = max(0, cfg.max_retries) + 1
        for attempt in range(attempts):
            try:
                return await self._stream(kwargs, on_delta)
            except EngineError:
                # Raised by _stream when output had already reached the caller:
                # a retry would hand the operator the same text twice.
                raise
            except Exception as exc:  # noqa: BLE001 — engine boundary, re-raised typed
                kind = classify_failure(exc)
                last = attempt == attempts - 1
                if kind not in WORTH_ANOTHER_MODEL or last:
                    # The verdict alone is not a diagnosis: "fatal: bad key"
                    # tells the operator nothing they could act on, so the
                    # underlying type goes in too.  The count is added only
                    # when retries actually ran, so the common no-retry case
                    # keeps its exact wording and never reads "1 attempts".
                    # Whether this is *fallback* worthy is a separate question,
                    # answered by the exception type (M2): a stream that broke
                    # after output also lands here, and no other model may take
                    # over from there.
                    tried = f" (after {attempt + 1} attempts)" if attempt else ""
                    message = f"{kind.value}{tried}: {type(exc).__name__}: {exc}"
                    raise _exhausted(kind, message) from exc
                if kind is FailureKind.AUTH_ERROR:
                    # Waiting cannot fix a rejected key, so the budget is not
                    # spent on it -- but another provider can.  Exits on the
                    # first attempt instead of after four identical 401s.
                    raise FallbackWorthwhile(
                        f"{kind.value}: {type(exc).__name__}: {exc}", kind=kind.value
                    ) from exc
                delay = retry_delay(
                    attempt,
                    kind,
                    base=cfg.retry_base_seconds,
                    cap=float(cfg.timeout),
                    retry_after=_retry_after(exc),
                )
                if self.on_retry is not None:
                    self.on_retry(
                        kind=kind.value,
                        attempt=attempt + 1,
                        delay_seconds=delay,
                        reason=str(exc),
                    )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable: the loop returns or raises on every path")

    async def _stream(
        self, kwargs: dict[str, Any], on_delta: DeltaSink | None
    ) -> StepResult:
        """One streaming attempt: assemble the fragments into a ``StepResult``."""
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
                # completed text per step, deliberately not streamed (adding an
                # ``on_reasoning`` sink would break every engine fake).
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
            if text_parts or acc:
                # The caller already saw this text and may already be holding a
                # half-built tool call; retrying would duplicate the former and
                # splice the latter.  This is the same split OpenCode draws
                # between `Retry` and `Continue` on `outputStarted`.
                raise EngineError(
                    f"stream failed after {len(text_parts)} text chunk(s) and "
                    f"{len(acc)} partial tool call(s) had already been delivered: {exc}"
                ) from exc
            raise

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
