"""TDD (M2): a chain of providers, tried in order until one answers.

The whole feature rests on one distinction -- ``FallbackWorthwhile`` means "this
model is done, another one may still answer", while a plain ``EngineError``
means "stop, no one else can help".  Every test here pins one half of that line,
because the failure mode of getting it wrong is a second model repeating text
the operator has already read.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_coding.ai.engine import EngineError, FallbackWorthwhile
from ai_coding.domain.model_config import ModelConfig


class _StubEngine:
    """Stands in for a real ``OpenAIChatEngine``: no client, no network.

    A full engine per test would need a fake HTTP client each, and these tests
    are about the chain's control flow, not about streaming.
    """

    def __init__(self, model: ModelConfig, outcomes: list[object]) -> None:
        self.model_config = model
        self._outcomes = outcomes
        self.calls = 0
        self.closed = False
        self.on_retry = None
        self.on_fallback = None

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


def _model(name: str) -> ModelConfig:
    return ModelConfig(name=name, base_url=f"https://{name}.test/v1", api_key="k")


def _answer(text: str) -> object:
    return SimpleNamespace(text=text, tool_calls=(), reasoning=None, finish_reason="stop")


def _exhausted(kind: str = "rate_limited") -> FallbackWorthwhile:
    """Mirrors what the engine raises, ``kind`` included: the stub has to carry
    the same contract or it would let the chain get away with guessing it."""
    return FallbackWorthwhile(f"{kind} (after 4 attempts): boom", kind=kind)


def _fatal() -> EngineError:
    # Not a FallbackWorthwhile on purpose -- see the module docstring.
    return EngineError("fatal: bad request")


def _engine(*pairs):
    from ai_coding.ai.fallback_engine import FallbackEngine

    return FallbackEngine([_StubEngine(model, outcomes) for model, outcomes in pairs])


@pytest.mark.asyncio
async def test_it_returns_the_first_models_answer_without_asking_the_rest():
    engine = _engine((_model("a"), [_answer("hi")]), (_model("b"), [_answer("no")]))

    result = await engine.step([], tools=[])

    assert result.text == "hi"
    assert [e.calls for e in engine._engines] == [1, 0]


@pytest.mark.asyncio
async def test_it_moves_on_when_a_model_is_exhausted():
    engine = _engine((_model("a"), [_exhausted()]), (_model("b"), [_answer("from b")]))

    assert (await engine.step([], tools=[])).text == "from b"


@pytest.mark.asyncio
async def test_it_stops_at_a_failure_no_other_model_can_fix():
    """A 400 is a property of the request: asking model b would only spend its
    budget learning the same thing."""
    engine = _engine((_model("a"), [_fatal()]), (_model("b"), [_answer("never")]))

    with pytest.raises(EngineError) as excinfo:
        await engine.step([], tools=[])

    assert not isinstance(excinfo.value, FallbackWorthwhile)
    assert engine._engines[1].calls == 0


@pytest.mark.asyncio
async def test_it_stops_when_the_failed_model_had_already_output_text():
    """The invariant (spec §5).  If this test ever passes with b called, M2 is
    unsafe to ship: model b would repeat prose and splice a half-built tool call."""
    engine = _engine((_model("a"), [_fatal()]), (_model("b"), [_answer("never")]))

    with pytest.raises(EngineError):
        await engine.step([], tools=[])

    assert engine._engines[1].calls == 0


@pytest.mark.asyncio
async def test_it_reports_the_whole_chain_when_every_model_is_exhausted():
    engine = _engine((_model("a"), [_exhausted()]), (_model("b"), [_exhausted()]))

    with pytest.raises(FallbackWorthwhile) as excinfo:
        await engine.step([], tools=[])

    message = str(excinfo.value)
    assert "a" in message and "b" in message, message
    assert [e.calls for e in engine._engines] == [1, 1]


@pytest.mark.asyncio
async def test_it_says_so_before_switching():
    """The operator has to learn the model changed while it is happening, not
    afterwards from the status bar."""
    seen: list[tuple[str, str, str]] = []
    engine = _engine((_model("qwen"), [_exhausted()]), (_model("deepseek"), [_answer("ok")]))

    engine.on_fallback = lambda *, from_model, to_model, kind, reason: seen.append(
        (from_model, to_model, kind)
    )
    await engine.step([], tools=[])

    assert seen == [("qwen", "deepseek", "rate_limited")]


@pytest.mark.asyncio
async def test_a_switch_is_silent_without_a_listener():
    """Every observer here is optional, so an engine assembled by a test or a
    tool must not depend on someone having subscribed."""
    engine = _engine((_model("a"), [_exhausted()]), (_model("b"), [_answer("ok")]))

    assert (await engine.step([], tools=[])).text == "ok"


def test_model_config_follows_the_active_engine():
    engine = _engine((_model("a"), [_exhausted()]), (_model("b"), [_answer("ok")]))
    assert engine.model_config.name == "a"

    engine._active = 1

    assert engine.model_config.name == "b"


def test_setting_model_config_jumps_to_that_engine():
    engine = _engine((_model("a"), [_answer("x")]), (_model("b"), [_answer("y")]))
    engine._active = 1

    engine.model_config = _model("a")

    assert engine.model_config.name == "a"


def test_setting_a_model_outside_the_chain_is_refused():
    engine = _engine((_model("a"), [_answer("x")]))

    with pytest.raises(ValueError, match="stranger"):
        engine.model_config = _model("stranger")


def test_on_retry_reaches_every_engine_in_the_chain():
    """M1's contract, preserved one level up: without this, a fallback chain
    would swallow every retry the single model was built to report."""
    engine = _engine((_model("a"), [_answer("x")]), (_model("b"), [_answer("y")]))
    sink = object()

    engine.on_retry = sink

    assert all(e.on_retry is sink for e in engine._engines)


def test_on_fallback_reaches_every_engine_in_the_chain():
    engine = _engine((_model("a"), [_answer("x")]))
    sink = object()

    engine.on_fallback = sink

    assert engine._engines[0].on_fallback is sink


@pytest.mark.asyncio
async def test_aclose_releases_every_client():
    """Each engine in the chain created its own client (F3); leaking the ones
    nobody ended up using would leak a socket per fallback, per session."""
    engine = _engine((_model("a"), [_answer("x")]), (_model("b"), [_answer("y")]))

    await engine.aclose()

    assert all(e.closed for e in engine._engines)


def test_a_chain_must_not_be_empty():
    from ai_coding.ai.fallback_engine import FallbackEngine

    with pytest.raises(ValueError, match="at least one"):
        FallbackEngine([])


def test_duplicate_models_would_loop_forever_at_config_time():
    """Not enforced here -- ``factory`` rejects cycles when it resolves the
    chain.  Pinned so the decision is visible rather than accidental."""
    model = _model("a")
    assert replace(model, name="a") is not model
    assert model.name == "a"


# ---- G7: "which model answered" has to survive the next step -----------------


@pytest.mark.asyncio
async def test_model_config_names_the_engine_that_answered():
    """Not the engine that failed, and not the one from an earlier turn.

    ``_active`` used to be set to the *predicted* next engine when one gave up,
    and never reset: after a fallback every later step reported the fallback
    target, even a step the primary answered.  The status bar read that name and
    so did the cost ledger, which is how a mislabelled model becomes
    misattributed spend.
    """
    engine = _engine(
        (_model("a"), [_exhausted(), _answer("from a")]),
        (_model("b"), [_answer("from b")]),
    )

    assert (await engine.step([], tools=[])).text == "from b"
    assert engine.model_config.name == "b", "the fallback target answered"

    assert (await engine.step([], tools=[])).text == "from a"
    assert engine.model_config.name == "a", "the primary answered again"


@pytest.mark.asyncio
async def test_the_chain_starts_from_the_top_every_step():
    """A turn that fell back must not leave the next turn starting mid-chain:
    the primary is the one to prefer until it fails again."""
    engine = _engine(
        (_model("a"), [_exhausted(), _answer("from a")]),
        (_model("b"), [_answer("from b")]),
    )

    await engine.step([], tools=[])  # a fails, b answers
    await engine.step([], tools=[])

    assert engine._engines[0].calls == 2, "the next step skipped the primary"


@pytest.mark.asyncio
async def test_switching_to_a_chain_member_makes_it_primary():
    """``/model b`` has to actually change who answers.

    It used to set ``_active``, which ``step`` then ignored -- it always began
    at index 0 -- so the command reported success and nothing happened.
    """
    engine = _engine((_model("a"), [_answer("from a")]), (_model("b"), [_answer("from b")]))

    engine.model_config = _model("b")

    assert (await engine.step([], tools=[])).text == "from b"
    assert [e.model_config.name for e in engine._engines] == ["b", "a"]


@pytest.mark.asyncio
async def test_switching_keeps_the_remaining_chain_in_order():
    """What comes after the new primary is still the original chain, minus the
    model that moved to the front."""
    engine = _engine(
        (_model("a"), [_answer("a")]),
        (_model("b"), [_answer("b")]),
        (_model("c"), [_answer("c")]),
    )

    engine.model_config = _model("c")

    assert [e.model_config.name for e in engine._engines] == ["c", "a", "b"]
