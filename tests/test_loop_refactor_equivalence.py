"""R7a: the loop behaves the same after the split — proved, not asserted.

R5 established the method: a refactor of the loop is only allowed if **existing
assertions do not change** *and* the observable behaviour is identical
(docs/r5-analysis.md).  "Identical" needs a recording, so this file records the
event stream of three scripted turns and compares against the JSON files in
``tests/golden/loop_events/``.

The golden files were recorded **before** `process_input` was touched
(`AI_CODING_RECORD_GOLDEN=1 pytest tests/test_loop_refactor_equivalence.py`), and
committed in that state, so the comparison is against the pre-refactor code — not
against whatever the refactor happens to produce.

Two kinds of field are deliberately dropped from the recording, because they are
wall-clock noise rather than behaviour: ``duration_ms`` / ``latency_ms`` and the
provider's ``usage`` (the fakes do not report any).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import RecordingObserver
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState, TodoItem
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry

GOLDEN_DIR = Path(__file__).parent / "golden" / "loop_events"
RECORD = os.environ.get("AI_CODING_RECORD_GOLDEN") == "1"

pytestmark = pytest.mark.asyncio

#: The deterministic fields of each event, in a fixed order.
_KEEP: dict[str, tuple[str, ...]] = {
    "IterationStarted": ("index", "max_iterations"),
    "AssistantMessage": ("text", "tool_calls"),
    "ToolApprovalRequested": ("tool_name", "reason"),
    "ToolStarted": ("call_id", "tool_name", "iteration"),
    "ToolFinished": (
        "call_id",
        "tool_name",
        "success",
        "output_preview",
        "error",
        "iteration",
    ),
    "ModelStepFinished": ("index", "finish_reason", "text_chars", "tool_call_count"),
    "ContextAssembled": (
        "iteration",
        "system_chars",
        "tools_chars",
        "history_messages",
        "history_chars",
        "tail_blocks",
        "estimated_tokens",
    ),
    "Degraded": ("component", "reason", "fallback"),
    "TaskUpdated": ("task",),
    "MemoryInjected": ("note_ids", "chars"),
    "TurnFinished": ("outcome",),
}

_OUTCOME_KEEP = ("status", "text", "handoff", "iterations", "reason")


def _plain(value: Any) -> Any:
    """A JSON-safe, deterministic view of one event field."""
    if isinstance(value, TurnOutcome):
        return {name: _plain(getattr(value, name)) for name in _OUTCOME_KEEP}
    if isinstance(value, TurnStatus):
        return value.value
    if isinstance(value, ToolCallRef):
        return {"id": value.id, "name": value.name}
    if isinstance(value, TaskState):
        return {
            "goal": value.goal,
            "items": [(item.id, item.content, item.status) for item in value.items],
        }
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _normalize(event: Any) -> dict[str, Any]:
    name = type(event).__name__
    record: dict[str, Any] = {"type": name}
    for field_name in _KEEP.get(name, ()):
        record[field_name] = _plain(getattr(event, field_name, None))
    return record


# ---------------------------------------------------------------- the fakes


class _ScriptedEngine(ChatEngine):
    """Answers from a script: ``("text", "...")`` or ``("tool", "poke")``."""

    def __init__(self, script: list[tuple[str, str]]) -> None:
        self.script = list(script)
        self.requests: list[list[Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        kind, payload = self.script.pop(0) if self.script else ("text", "（脚本用完了）")
        if kind == "tool":
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(id=f"c{len(self.requests)}", name=payload, arguments="{}")
                ],
                finish_reason="tool_calls",
            )
        return StepResult(text=payload, tool_calls=[], finish_reason="stop")


class _Poke(BaseTool):
    """A tool whose name is outside the gate's table, so it is WARN tier."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "poke"

    @property
    def description(self) -> str:
        return "poke"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="poked")


class _Sessions:
    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)

    def mark_pending_approval(self, session_id: str, request) -> None:
        """P3: stand-in for the marker the real SessionService writes beside
        the session file; this fake keeps nothing on disk."""
        return None


def _session(scenario: str) -> SessionData:
    session = SessionData(
        session_id=scenario, title="", created_time="t", last_access_time="t"
    )
    if scenario == "todo_nudge":
        session.task = TaskState(
            goal="长任务",
            items=[TodoItem(id="1", content="还没做完的那一步", status="pending")],
        )
    return session


def _script(scenario: str) -> list[tuple[str, str]]:
    if scenario == "normal":
        return [("text", "做完了")]
    if scenario == "todo_nudge":
        # 13 §2.1: a stop with an open item buys L1; a second stop escalates to
        # the ask (L2); a third stop is the ladder's end → COMPLETED with the
        # model's own last words.
        return [
            ("text", "我先停一下"),
            ("text", "再停一下"),
            ("text", "好了，真的做完了"),
        ]
    return [("tool", "poke")] * 5  # never finishes on its own


async def _run(scenario: str) -> tuple[list[dict[str, Any]], dict[str, Any], SessionData]:
    engine = _ScriptedEngine(_script(scenario))
    registry = ToolRegistry()
    registry.register(_Poke())
    session = _session(scenario)
    sessions = _Sessions()
    observer = RecordingObserver()
    loop = AgentLoop(
        engine,
        sessions,  # type: ignore[arg-type] — only save() is used
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(
            # 13 §2.1: the ladder needs L1 nudge → L2 ask → the stop, i.e. three
            # model steps; the other scenarios keep the tighter 2-step budget
            # so their recordings stay byte-identical.
            max_tool_iterations=4 if scenario == "todo_nudge" else 2,
            stop_policy="todo_aware",
            max_todo_nudges=1,
        ),
        observer=observer,
    )
    answer = await loop.process_input(session, "开始")
    outcome = loop.last_outcome or TurnOutcome(
        status=TurnStatus.ERROR, text=None, iterations=0, reason="no outcome"
    )
    summary = {
        "answer": answer,
        "outcome": _plain(outcome),
        "messages": [message.role for message in session.messages],
        "digests": [digest.turn for digest in session.turn_digests],
        "saves": len(sessions.saved),
    }
    return [_normalize(event) for event in observer.events], summary, session


# ------------------------------------------------------------------ the test


@pytest.mark.parametrize("scenario", ["normal", "todo_nudge", "exhausted"])
async def test_the_event_sequence_matches_the_recording(scenario: str):
    events, summary, _session_data = await _run(scenario)
    path = GOLDEN_DIR / f"{scenario}.json"
    if RECORD:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"events": events, "summary": summary}, indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
    assert path.is_file(), f"missing golden recording {path}"
    golden = json.loads(path.read_text(encoding="utf-8"))
    assert events == golden["events"]
    assert summary == golden["summary"]
