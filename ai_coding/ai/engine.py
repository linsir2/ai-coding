"""ChatEngine port — one model step at a time (R1a-2).

The orchestration layer owns the iteration loop; an engine only ever performs a
**single** model call.  ``messages`` already contains the current user turn, so
an engine must never append anything of its own — that was the source of the
double user-turn bug in the SDK path.

This module depends only on ``domain``.  Tool schemas arrive pre-built as plain
dicts, so the ``ai`` layer never imports the ``tools`` layer (that would create
a cycle: tools -> security -> ... and core -> ai).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage

DeltaSink = Callable[[str], None]

#: Told about a retry *before* the engine waits.  Keyword-only so the shape is
#: stated once and cannot be reordered at a call site; a positional free-for-all
#: of four same-typed arguments is exactly where a wiring bug hides.
RetryObserver = Callable[..., None]

#: Told when the active model is abandoned for another one (M2).  Keyword-only
#: for the same reason as ``RetryObserver``.
FallbackObserver = Callable[..., None]


class EngineError(RuntimeError):
    """A model-call failure the loop must see (never swallowed).

    ``kind`` is the verdict ``ai.openai_engine.classify_failure`` reached.  It
    lives on the base class rather than on ``FallbackWorthwhile`` because the
    loop makes a second, unrelated decision from it: a context overflow is not
    worth another model (M2) but *is* worth compacting the history (G5-1), and it
    needs the same field to tell the two apart.  Re-deriving it by matching the
    message would make a human-readable string a contract -- the mistake M2
    removed.

    Empty for failures that carry no verdict, such as a stream that broke after
    the operator already saw text.
    """

    kind: str = ""

    def __init__(self, *args: Any, kind: str = "") -> None:
        super().__init__(*args)
        self.kind = kind


class FallbackWorthwhile(EngineError):
    """One model gave up and another might still answer (M2).

    A subclass so every existing ``except EngineError`` keeps working: when the
    whole chain is exhausted the turn still ends in ``ERROR`` the way it always
    did.  What the subclass buys is the one distinction that was previously
    impossible to make -- an exhausted retry budget is worth another provider,
    while a stream that broke *after* the operator already saw text is not,
    because a second model would repeat the prose and splice a half-built tool
    call.

    Both spellings are raised by ``OpenAIChatEngine``; ``FallbackEngine`` is the
    only consumer that cares about the difference.

    Args:
        kind: the failure verdict, carried through verbatim from the classifier.
    """


class ChatEngine(ABC):
    """Port for executing exactly one step of a conversation."""

    #: Set by ``AgentLoop.set_model``; concretely a ``ModelConfig``.  Declared
    #: here (typing only) so the port matches how the loop actually uses it.
    model_config: Any

    #: M1: where a retry is reported.  Declared on the port because the loop
    #: subscribes unconditionally -- a ``getattr`` probe would invert the
    #: question ("does this engine already have a listener?") into "does this
    #: engine retry?", which no engine can answer before its first failure.
    on_retry: RetryObserver | None = None

    #: M2: where a switch to a different model is reported.  Declared here for
    #: the same reason as ``on_retry``: ``AgentLoop`` assigns it without asking,
    #: so the port has to carry it.  Only ``FallbackEngine`` ever calls it.
    on_fallback: FallbackObserver | None = None

    @abstractmethod
    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]],
        instructions: str | None = None,
        on_delta: DeltaSink | None = None,
        image_loader: Any | None = None,
    ) -> StepResult:
        """Run one model call over ``messages`` and return what it produced.

        ``messages`` is the full conversation *including* the current user turn.
        ``tools`` are OpenAI function schemas (empty list = no tools offered).
        ``instructions`` is the system prompt for this request, kept separate so
        the caller's persisted history stays pure conversation.
        ``on_delta`` receives streamed text deltas as they arrive.
        """

    async def aclose(self) -> None:
        """Release engine resources.  Default: nothing to release."""
        return None


__all__ = [
    "ChatEngine",
    "DeltaSink",
    "EngineError",
    "FallbackObserver",
    "FallbackWorthwhile",
    "RetryObserver",
]
