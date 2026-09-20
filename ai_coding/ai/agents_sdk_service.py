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

    def _build_agent(self, instructions: str | None = None) -> Agent[Any]:
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
            instructions=instructions or _DEFAULT_INSTRUCTIONS,
            model=sdk_model,
            tools=list(self._tools),
            model_settings={"temperature": cfg.temperature, "max_tokens": cfg.max_tokens},
        )

    async def execute_turn(
        self,
        history: list[ChatMessage],
        user_input: str,
        token_sink: Callable[[str], None] | None = None,
        *,
        instructions: str | None = None,
    ) -> TurnResult:
        # Full session history plus the new user turn as SDK input items.
        inputs: list[TResponseInputItem] = cast(
            list[TResponseInputItem], history_to_inputs(history)
        )
        inputs.append({"role": "user", "content": user_input})

        agent = self._build_agent(instructions)
        stream = Runner.run_streamed(agent, input=inputs)

        # ---- collection state -------------------------------------------
        tool_calls: list[ToolCallRef] = []
        tool_outputs: list[ToolOutput] = []
        call_id_to_name: dict[str, str] = {}
        seen_call_ids: set[str] = set()
        seen_output_ids: set[str] = set()
        final_message: str | None = None  # last assistant output message

        async for event in stream.stream_events():
            event_type = getattr(event, "type", None)

            if event_type == "raw_response_event":
                # Only the assistant *text* deltas are emitted to the live sink /
                # captured for turn.text.  Tool-call *arguments* arrive as
                # ``response.function_call_arguments.delta`` and would pollute the
                # natural-language answer if collected here.
                if getattr(getattr(event, "data", None), "type", None) == (
                    "response.output_text.delta"
                ):
                    delta = getattr(getattr(event, "data", None), "delta", None)
                    if isinstance(delta, str) and delta:
                        if token_sink:
                            token_sink(delta)
                continue

            if event_type == "run_item_stream_event":
                if isinstance(event, RunItemStreamEvent):
                    if event.name == "message_output_created":
                        final_message = _extract_message_text(event.item)
                    elif event.name == "tool_called":
                        item = event.item
                        call_id = _item_call_id(item)
                        tool_name = _item_tool_name(item)
                        if call_id and tool_name and call_id not in seen_call_ids:
                            call_id_to_name[call_id] = tool_name
                            tool_calls.append(
                                ToolCallRef(
                                    id=call_id,
                                    name=tool_name,
                                    arguments=_extract_arguments_json(item),
                                )
                            )
                            seen_call_ids.add(call_id)
                    elif event.name == "tool_output":
                        item = event.item
                        call_id = _item_call_id(item)
                        if call_id and call_id not in seen_output_ids:
                            tool_outputs.append(
                                ToolOutput(
                                    call_id=call_id,
                                    tool_name=call_id_to_name.get(call_id, "tool"),
                                    output=_extract_tool_output(item),
                                )
                            )
                            seen_output_ids.add(call_id)
                continue

        return TurnResult(
            text=final_message,
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


def _item_call_id(item: Any) -> str | None:
    """Call id from a ToolCallItem / ToolCallOutputItem (works for dict or model raw)."""
    raw = getattr(item, "raw_item", item)
    if isinstance(raw, dict):
        cid = raw.get("call_id") or raw.get("id")
        return str(cid) if cid is not None else None
    cid = getattr(raw, "call_id", None) or getattr(raw, "id", None)
    return str(cid) if cid is not None else None


def _item_tool_name(item: Any) -> str | None:
    """Tool name from a ToolCallItem (``tool_name`` property, version-independent)."""
    raw = getattr(item, "raw_item", item)
    if isinstance(raw, dict):
        return raw.get("name")
    return getattr(raw, "name", None)


def _extract_message_text(item: Any) -> str | None:
    """Natural-language text of a MessageOutputItem (its last content part wins)."""
    raw = getattr(item, "raw_item", item)
    content = raw.get("content") if isinstance(raw, dict) else getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if content:
        for part in content:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if text:
                parts.append(str(text))
    return "".join(parts) if parts else None


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
