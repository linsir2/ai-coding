"""TDD (R4a): the ``trace`` command and the loop-to-trace wiring.

Recording is only useful if it happens on a real run and can be read back
without opening JSONL by hand (docs/r4-analysis.md 11).
"""

import json

import pytest
from typer.testing import CliRunner

from ai_coding.ai.engine import ChatEngine
from ai_coding.cli import _compose_observer, _make_recorder, app
from ai_coding.config.models import AIConfig, AppConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.observers import Braid
from ai_coding.core.trace import TraceRecorder, TraceStore
from ai_coding.domain.loop import StepResult
from ai_coding.domain.session import SessionData
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry

runner = CliRunner()


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        return StepResult(
            text="done",
            finish_reason="stop",
            usage={"prompt_tokens": 40, "completion_tokens": 4, "total_tokens": 44},
        )

    async def aclose(self) -> None:
        return None


def _session() -> SessionData:
    return SessionData(
        session_id="sess-1",
        title="",
        created_time="2026-01-01T00:00:00Z",
        last_access_time="",
    )


@pytest.mark.asyncio
async def test_a_real_turn_writes_a_readable_trace(tmp_path):
    store = TraceStore(tmp_path / ".ai-coding" / "traces")
    recorder = TraceRecorder(store, session_id="sess-1")
    loop = AgentLoop(
        _Engine(),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural"),
        observer=Braid(recorder),
        trace=recorder,
        workspace=str(tmp_path),
    )

    await loop.process_input(_session(), "hello")

    path = recorder.current_path
    assert path is not None
    assert path.name == "turn-1.jsonl"
    assert path.parent.parent.parent == tmp_path / ".ai-coding"
    events = [json.loads(line)["evt"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "ContextAssembled" in events
    assert "ModelStepFinished" in events
    assert events[-1] == "TurnFinished"

    summary = store.read_summary("sess-1")
    assert summary["prompt_tokens"] == 40


def test_make_recorder_respects_the_enabled_flag(tmp_path):
    off = AppConfig(tracing={"enabled": False})  # type: ignore[arg-type]
    assert _make_recorder(off, str(tmp_path), _session()) is None

    on = AppConfig()  # tracing defaults to enabled
    recorder = _make_recorder(on, str(tmp_path), _session())
    assert recorder is not None
    assert recorder.session_id == "sess-1"


def test_compose_observer_returns_the_base_when_tracing_is_off():
    class Base:
        def on_event(self, event):  # type: ignore[no-untyped-def]
            return None

    base = Base()
    assert _compose_observer(base, None) is base
    braid = _compose_observer(base, TraceRecorder(TraceStore("."), session_id="s"))
    assert isinstance(braid, Braid)


def test_trace_list_reports_an_empty_workspace(tmp_path):
    result = runner.invoke(app, ["trace", "list", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "no traces recorded" in result.stdout


def test_trace_show_and_budget_render_a_recorded_session(tmp_path):
    session_dir = tmp_path / ".ai-coding" / "traces" / "sess-1"
    session_dir.mkdir(parents=True)
    (session_dir / "turn-1.jsonl").write_text(
        json.dumps(
            {
                "v": 1,
                "ts": "t",
                "session": "sess-1",
                "turn": 1,
                "span": "step1",
                "parent": "i1",
                "iter": 1,
                "evt": "ModelStepFinished",
                "data": {
                    "usage": {"prompt_tokens": 40, "completion_tokens": 4},
                    "cached_tokens": 30,
                    "latency_ms": 500,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (session_dir / "summary.json").write_text(
        json.dumps({"turns": 1, "steps": 1, "prompt_tokens": 40, "completion_tokens": 4}),
        encoding="utf-8",
    )

    listed = runner.invoke(app, ["trace", "list", "--workspace", str(tmp_path)])
    assert "sess-1" in listed.stdout
    assert "1 turn" in listed.stdout

    shown = runner.invoke(app, ["trace", "show", "sess-1", "--workspace", str(tmp_path)])
    assert shown.exit_code == 0
    assert "turn 1" in shown.stdout
    assert "cache 75%" in shown.stdout

    budget = runner.invoke(app, ["trace", "budget", "sess-1", "--workspace", str(tmp_path)])
    assert budget.exit_code == 0
    assert "1 turns" in budget.stdout


def test_trace_show_with_no_session_is_an_error(tmp_path):
    result = runner.invoke(app, ["trace", "show", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
