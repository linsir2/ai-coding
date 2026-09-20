"""OpenAI Agents SDK adapter — one of two modules that imports ``agents``.

The other is ``ai_coding/tools/sdk_adapter.py``.  Everything else stays SDK-agnostic.
Network-dependent ``execute_turn`` is not unit-tested; construction (offline)
is, to guarantee our model wiring is correct.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from agents import Agent, Runner
from agents.items import TResponseInputItem
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.stream_events import RunItemStreamEvent
from openai import AsyncOpenAI

from ai_coding.ai.base import AIService
from ai_coding.ai.mapping import history_to_inputs
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
from ai_coding.domain.run import TurnResult

_DEFAULT_INSTRUCTIONS = (
    "You are an AI coding assistant. Respond to the user's coding questions "
    "clearly and concisely, in the same language the user writes in."
)


class AgentsSDKChatService(AIService):
    """An ``AIService`` backed by the OpenAI Agents SDK streaming runner."""

    def __init__(
        self,
        model_config: Any,
        tools: list[Any] | None = None,
    ) -> None:
        from ai_coding.domain.model_config import ModelConfig

        if not isinstance(model_config, ModelConfig):
            raise TypeError("model_config must be a ModelConfig")
        self.model_config = model_config
        self._tools = list(tools) if tools else []

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
            tools=list(self._tools),
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

        chunks: list[str] = []
        tool_calls: list[ToolCallRef] = []
        tool_outputs: list[ToolOutput] = []
        seen_call_ids: set[str] = set()
        seen_output_ids: set[str] = set()

        async for event in stream.stream_events():
            event_type = getattr(event, "type", None)

            if event_type == "raw_response_event":
                delta = getattr(getattr(event, "data", None), "delta", None)
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                    if token_sink:
                        token_sink(delta)
                continue

            if event_type == "run_item_stream_event":
                # Collect tool_call entries for session history persistence.
                if isinstance(event, RunItemStreamEvent) and event.name == "tool_called":
                    item = event.item
                    call_id = getattr(item, "call_id", None)
                    tool_name = getattr(item, "name", None)
                    if call_id and tool_name and call_id not in seen_call_ids:
                        args_json = _extract_arguments_json(item)
                        tool_calls.append(
                            ToolCallRef(
                                id=str(call_id),
                                name=str(tool_name),
                                arguments=args_json,
                            )
                        )
                        seen_call_ids.add(str(call_id))
                # Collect tool outputs so AgentLoop can persist strict-pairing
                # ``role=tool`` messages for the session history.
                if isinstance(event, RunItemStreamEvent) and event.name == "tool_output":
                    item = event.item
                    call_id = getattr(item, "call_id", None)
                    tool_name = getattr(item, "name", None)
                    if call_id and tool_name and call_id not in seen_output_ids:
                        tool_outputs.append(
                            ToolOutput(
                                call_id=str(call_id),
                                tool_name=str(tool_name),
                                output=_extract_tool_output(item),
                            )
                        )
                        seen_output_ids.add(str(call_id))
                continue

        text = "".join(chunks) or None
        return TurnResult(
            text=text,
            tool_calls=tool_calls,
            tool_outputs=tool_outputs,
        )


def _extract_tool_output(item: Any) -> str:
    """Pull the tool output text that the SDK returned for an executed tool."""
    output = getattr(item, "output", None)
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    # output may hold a structured object for message-based tools; stringify defensively.
    import json

    try:
        return json.dumps(output, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(output)


def _extract_arguments_json(item: Any) -> str:
    """Pull the raw arguments JSON string (or a serialized form) from a tool-call item."""
    raw = getattr(item, "raw_item", None)
    if raw is None:
        return "{}"
    # raw_item may be a dict or a pydantic model
    if isinstance(raw, dict):
        args = raw.get("arguments")
        if isinstance(args, str):
            return args
        import json

        return json.dumps(args or {}, ensure_ascii=False)
    # pydantic model
    args = getattr(raw, "arguments", None)
    if isinstance(args, str):
        return args
    import json

    try:
        return json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


__all__ = ["AgentsSDKChatService"]
