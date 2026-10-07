"""A chain of providers, tried in order until one answers (M2).

Composition, not inheritance: each ``OpenAIChatEngine`` keeps sole responsibility
for one model -- its own client, its own request kwargs, its own partial-output
guard.  Putting the loop inside ``step()`` instead would mean swapping clients
and rebuilding kwargs mid-call, reopening lifecycle questions those classes
already answer.

The line this class exists to draw: ``FallbackWorthwhile`` means "this model is
done, another one may still answer"; a plain ``EngineError`` means "stop, no one
else can help".  Swapping a model after its output already reached the operator
would repeat prose and splice a half-built tool call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ai_coding.ai.engine import ChatEngine, DeltaSink, FallbackWorthwhile
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage

__all__ = ["FallbackEngine"]


class FallbackEngine(ChatEngine):
    """Try each engine in turn; hand back the first answer that arrives.

    Holds no state beyond which engine is currently active, so a chain built but
    never used costs nothing and a turn that never falls back behaves exactly
    like the single engine underneath.
    """

    def __init__(self, engines: Sequence[ChatEngine]) -> None:
        if not engines:
            raise ValueError("a fallback chain needs at least one engine")
        self._engines: tuple[ChatEngine, ...] = tuple(engines)
        self._active = 0

    # -- what the loop and the status bar read -------------------------------

    @property
    def model_config(self) -> Any:
        """The config of the engine that is actually being asked.

        ``AgentLoop.set_model`` assigns here, so the setter below has to exist;
        without it that line would raise ``AttributeError`` on this class alone
        and only once someone repairs the call (``AIConfig`` has no ``models``
        field today, so it never runs).
        """
        return self._engines[self._active].model_config

    @model_config.setter
    def model_config(self, value: Any) -> None:
        """Make this model the one to try first.

        Not "start at its index": ``step`` begins at the top of the chain on
        purpose, so recording a preference anywhere else would be ignored -- which
        is exactly what ``/model`` used to do while reporting success.  Moving the
        engine to the front keeps the promise the command makes, and the models
        that followed it stay behind it in their original order.

        Rebuilding the chain from a model that is *not* in it would mean
        replacing the engine -- new clients, new subscriptions, the old ones
        closed -- so that is refused rather than half-done.
        """
        for index, engine in enumerate(self._engines):
            if engine.model_config is value or engine.model_config == value:
                rest = tuple(e for i, e in enumerate(self._engines) if i != index)
                self._engines = (engine, *rest)
                self._active = 0
                return
        raise ValueError(f"model {getattr(value, 'name', value)!r} is not in this fallback chain")

    # -- callbacks, broadcast so a chain is as observable as a single engine --

    @property
    def on_retry(self) -> Any:
        return self._engines[0].on_retry

    @on_retry.setter
    def on_retry(self, value: Any) -> None:
        for engine in self._engines:
            engine.on_retry = value

    @property
    def on_fallback(self) -> Any:
        return self._engines[0].on_fallback

    @on_fallback.setter
    def on_fallback(self, value: Any) -> None:
        for engine in self._engines:
            engine.on_fallback = value

    # -- the port ------------------------------------------------------------

    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]],
        instructions: str | None = None,
        on_delta: DeltaSink | None = None,
    ) -> StepResult:
        last: FallbackWorthwhile | None = None
        # Start from the top every step: a turn that fell back must not leave the
        # next one beginning mid-chain, and ``_active`` has to end up naming the
        # engine that actually answered (the status bar and the cost ledger both
        # read it afterwards).
        self._active = 0
        for index, engine in enumerate(self._engines):
            self._active = index
            try:
                return await engine.step(
                    messages, tools=tools, instructions=instructions, on_delta=on_delta
                )
            except FallbackWorthwhile as exc:
                last = exc
                if index + 1 >= len(self._engines):
                    raise
                self._announce(self._engines[index], self._engines[index + 1], exc)
        # Unreachable: the loop either returns or re-raises on its last pass.
        raise AssertionError("unreachable") from last

    def _announce(
        self, failed: ChatEngine, chosen: ChatEngine, cause: FallbackWorthwhile
    ) -> None:
        sink = self.on_fallback
        if sink is None:
            return
        sink(
            from_model=getattr(failed.model_config, "name", ""),
            to_model=getattr(chosen.model_config, "name", ""),
            kind=cause.kind,
            reason=str(cause),
        )

    async def aclose(self) -> None:
        """Release every client, including the ones no turn ever asked.

        Each engine built its own client at construction, so a chain that fell
        back once would otherwise leak a socket for the rest of the session.
        """
        for engine in self._engines:
            await engine.aclose()
