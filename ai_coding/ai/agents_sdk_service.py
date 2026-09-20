"""OpenAI Agents SDK adapter — the ONLY module that imports ``agents``.

Everything upstream (AgentLoop, session service, CLI) stays SDK-agnostic.
Network-dependent ``execute_turn`` is not unit-tested; construction (offline)
is, to guarantee our model wiring is correct.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from agents import Agent, Runner
from agents.items import TResponseInputItem
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from ai_coding.ai.base import AIService
from ai_coding.ai.mapping import history_to_inputs
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.run import TurnResult

_DEFAULT_INSTRUCTIONS = (
    "You are an AI coding assistant. Respond to the user's coding questions "
    "clearly and concisely, in the same language the user writes in."
)


class AgentsSDKChatService(AIService):
    """An ``AIService`` backed by the OpenAI Agents SDK streaming runner."""

    def __init__(self, model_config: Any) -> None:
        from ai_coding.domain.model_config import ModelConfig

        if not isinstance(model_config, ModelConfig):
            raise TypeError("model_config must be a ModelConfig")
        self.model_config = model_config

    def _build_agent(self) -> Agent[Any]:
        """Build an SDK Agent wired to the configured model. Never touches the network."""
        cfg = self.model_config
        client = AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=cfg.timeout,
            max_retries=0,
        )
        sdk_model = OpenAIChatCompletionsModel(model=cfg.name, openai_client=client)
        return Agent(
            name=cfg.name,
            instructions=_DEFAULT_INSTRUCTIONS,
            model=sdk_model,
            model_settings={"temperature": cfg.temperature, "max_tokens": cfg.max_tokens},
        )

    async def execute_turn(
        self,
        history: list[ChatMessage],
        user_input: str,
        token_sink: Callable[[str], None] | None = None,
    ) -> TurnResult:
        # Full session history plus the new user turn as SDK input items.
        inputs: list[TResponseInputItem] = cast(
            list[TResponseInputItem], history_to_inputs(history)
        )
        inputs.append({"role": "user", "content": user_input})

        agent = self._build_agent()
        stream = Runner.run_streamed(agent, input=inputs)

        # Collect streamed text deltas; these also form the final text (M1: no tools).
        chunks: list[str] = []
        async for event in stream.stream_events():
            if getattr(event, "type", None) != "raw_response_event":
                continue
            delta = getattr(getattr(event, "data", None), "delta", None)
            if isinstance(delta, str) and delta:
                chunks.append(delta)
                if token_sink:
                    token_sink(delta)

        text = "".join(chunks) or None
        return TurnResult(text=text)


__all__ = ["AgentsSDKChatService"]
