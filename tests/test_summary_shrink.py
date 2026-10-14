"""TDD (A.P2-1): the summarizer request shrinks when the provider says it is
too big.

The L4 summary is its own chat call sharing the main loop's window, and on a
32k-window model a 60k-character CJK history (the ``_clip_for_summary`` cap) is
already past that window.  The old code answered any failure with ``None``,
which sent the full trimmed history back into the main request — the request
that was too big in the first place — and the turn died.

This pins the OpenCode rule (``compaction.ts:421``) on our seam: a rejection
that *looks like* a context overflow retries with ``SHRINK_STEPS`` applied to
the size that was actually sent, up to the last step, and anything else keeps
its old behaviour (one plain ``None``, no retries, no shrinking).

``_chat`` is monkeypatched for the loop cases, and driven for real in
``test_the_real_async_client_retries_a_400_overflow_and_ships_a_smaller_body``
over a local HTTP server that answers the first POST with the provider's 400.
That is the one this file exists for: the wire must really carry a smaller
second request, not a mock agreeing with itself.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ai_coding.ai.llm_helpers as llm_helpers
from ai_coding.domain.model_config import ModelConfig


class _Resp:
    """Duck-shaped openai response: the SDK's error constructors only read
    ``.request``, ``.status_code`` and ``.headers``."""

    def __init__(self) -> None:
        self.request = object()
        self.status_code = 400
        self.headers: dict = {}


def _overflow() -> Exception:
    import openai

    return openai.BadRequestError(
        "This model's maximum context length is 32768 tokens. "
        "However, you requested 42000 tokens",
        response=_Resp(),
        body=None,
    )


def _big_history(n: int = 60_000) -> str:
    """Enough CJK that the 60k cap actually bounds it (≈30k tokens)."""
    return "内容" * (n // 2)


def _ok_summary() -> str:
    return "## 目标\n压缩完成"


async def _summarize(text: str, previous: str | None = None):
    return await llm_helpers.build_summarizer(object())(text, previous)


# ---- R1: an overflow on the first size retries at a lower cap ---------------


@pytest.mark.asyncio
async def test_an_overflow_on_the_first_size_is_retried_at_a_lower_cap(monkeypatch):
    """The defect: the straggler path returned ``None`` on *any* exception, so
    a request that was merely too big silently handed the full history back to
    a main request that was also too big.  The retry must exist, and it must
    actually carry a smaller prompt."""
    calls: list[str] = []
    raised = False

    async def fake_chat(model_config, system, user, max_tokens):
        nonlocal raised
        calls.append(user)
        if not raised:
            raised = True
            raise _overflow()
        return _ok_summary()

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await _summarize(_big_history())

    assert got == "## 目标\n压缩完成"
    assert len(calls) == 2, "one shrink retry, not zero"
    assert len(calls[1]) < len(calls[0]), "the retry must carry less text"


# ---- R2: the real client really ships a smaller second request --------------


class _OpenAILikeHandler(BaseHTTPRequestHandler):
    """A real socket that pretends to be a 32k-window model: the first
    completion request gets the provider's 400, every later one a valid
    answer.  Records the raw byte length of each request so the test can watch
    the wire shrink."""

    def log_message(self, *args) -> None:  # keep test output readable
        return None

    def _send(self, payload: bytes, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 — HTTP verb
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        state = self.server.state
        state.request_sizes.append(len(body))
        if len(state.request_sizes) == 1:
            error = {
                "error": {
                    "message": "This model's maximum context length is 32768 "
                    "tokens. However, your messages resulted in 42000 tokens.",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                }
            }
            self._send(json.dumps(error).encode(), 400)
            return
        completion = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _ok_summary()},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        self._send(json.dumps(completion).encode(), 200)


class _OpenAILikeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        class _State:
            request_sizes: list[int] = []

        self.state = _State()
        super().__init__(("127.0.0.1", 0), _OpenAILikeHandler)

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
        self.server_close()


@pytest.mark.asyncio
async def test_the_real_async_client_retries_a_400_overflow_and_ships_a_smaller_body():
    """The real ``AsyncOpenAI`` client against a real local socket.  The first
    POST answers 400 ``context_length_exceeded``; if the client then sent the
    same huge body again there would be nothing to prove.  It must send a
    smaller one and get the summary back."""
    with _OpenAILikeServer() as server:
        model = ModelConfig(
            name="m",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            api_key="k",
        )
        summarize = llm_helpers.build_summarizer(model)

        got = await summarize(_big_history())

        assert got == "## 目标\n压缩完成"
        assert len(server.state.request_sizes) == 2, "one 400 then one retry"
        assert server.state.request_sizes[1] < server.state.request_sizes[0], (
            "the retried request must be smaller on the wire"
        )


# ---- I1: every size refused, give up after the ladder ------------------------


@pytest.mark.asyncio
async def test_every_size_refused_gives_up_after_the_ladder(monkeypatch):
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        raise _overflow()

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await _summarize(_big_history())

    assert got is None
    assert len(calls) == 1 + len(llm_helpers._SHRINK_STEPS)
    sizes = [len(c) for c in calls]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes), (
        "each retry must be strictly smaller than the last"
    )


# ---- I2: a rejection that is NOT an overflow is still a plain None ----------


@pytest.mark.asyncio
async def test_a_failure_that_is_not_an_overflow_is_not_retried(monkeypatch):
    """Rate limits and network blips must not trip the shrink ladder: the
    request size was fine, shrinking it cannot help, and burning three more
    calls on the same window is exactly the block-list misbehaviour the old
    one-shot behaviour avoided."""
    import openai

    async def fake_chat(model_config, system, user, max_tokens):
        raise openai.RateLimitError("slow down", response=_Resp(), body=None)

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await _summarize(_big_history())

    assert got is None


# ---- I3: a shape failure still walks the NUDGE path, not the shrink ---------


@pytest.mark.asyncio
async def test_a_shape_failure_still_takes_the_nudge_not_the_shrink(monkeypatch):
    """Shrinking the input cannot fix an answer that ignored the format, so an
    unstructured reply must consume the two NUDGE attempts and stop — exactly
    as before the shrink ladder existed, and without paying for extra calls."""
    replies = iter(["好的，我总结了一下。", "好的，我总结了一下。"])
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        return next(replies)

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await _summarize(_big_history())

    assert got is None
    assert len(calls) == 2, "two NUDGE attempts, not a shrink ladder"
    assert calls[1].startswith(calls[0]), (
        "the history prefix is unchanged — only the nudge text was appended"
    )


# ---- I4: a request that fits first time is not retried -----------------------


@pytest.mark.asyncio
async def test_a_first_attempt_that_fits_is_not_retried(monkeypatch):
    calls: list[str] = []

    async def fake_chat(model_config, system, user, max_tokens):
        calls.append(user)
        return _ok_summary()

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await _summarize(_big_history())

    assert got == "## 目标\n压缩完成"
    assert len(calls) == 1


# ---- I5: _clip_for_summary honours an explicit cap ---------------------------


def test_clip_for_summary_honours_an_explicit_cap():
    text = "x" * 3000 + "y" * 3000

    assert llm_helpers._clip_for_summary(text, cap=1000) == "x" * 1000 + "\n[历史过长，此处截断]"
    assert llm_helpers._clip_for_summary(text) == text, "default cap still leaves short text alone"


# ---- P1: the merge prompt keeps its bigger output budget while shrinking -----


@pytest.mark.asyncio
async def test_the_merge_prompt_keeps_its_output_budget_while_shrinking(monkeypatch):
    """A merge (``previous`` set) overflows too, and must retry at a lower
    cap — but its output budget is the merge's, not the create's."""
    calls: list[str] = []
    budgets: list[int] = []
    raised = False

    async def fake_chat(model_config, system, user, max_tokens):
        nonlocal raised
        calls.append(user)
        budgets.append(max_tokens)
        if not raised:
            raised = True
            raise _overflow()
        return _ok_summary()

    monkeypatch.setattr(llm_helpers, "_chat", fake_chat)

    got = await _summarize("以前的结论\n" + _big_history(), previous="以前的结论")

    assert got == "## 目标\n压缩完成"
    assert len(calls) == 2
    assert budgets == [llm_helpers._MERGE_TOKENS, llm_helpers._MERGE_TOKENS], (
        "shrinking input must not shrink the merge's output budget"
    )
