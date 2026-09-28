"""TDD (R4a): the ``trace`` command and the loop-to-trace wiring.

Recording is only useful if it happens on a real run and can be read back
without opening JSONL by hand (docs/r4-analysis.md 11).
"""

import json
import os
import time
from pathlib import Path

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


# ---- R9b: trace overview (docs/r9-analysis.md 3.2) ----


def _session_with(tmp_path, session: str, *, turns: int, summary: dict) -> Path:
    session_dir = tmp_path / ".ai-coding" / "traces" / session
    session_dir.mkdir(parents=True)
    for index in range(1, turns + 1):
        (session_dir / f"turn-{index}.jsonl").write_text("{}\n", encoding="utf-8")
    (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return session_dir


def test_r9_overview_renders_sessions_and_a_total(tmp_path):
    _session_with(tmp_path, "sess-1", turns=2,
                  summary={"turns": 2, "steps": 3, "prompt_tokens": 200,
                           "completion_tokens": 20, "cached_tokens": 150})
    _session_with(tmp_path, "sess-2", turns=1,
                  summary={"turns": 1, "steps": 1, "prompt_tokens": 100,
                           "completion_tokens": 10, "cached_tokens": 0})

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])

    assert result.exit_code == 0
    assert "sess-1" in result.stdout and "sess-2" in result.stdout
    assert "TOTAL" in result.stdout
    assert "300" in result.stdout        # prompt tokens summed
    assert "50.0%" in result.stdout      # 150/300


def test_r9_overview_json_is_machine_readable(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1,
                  summary={"turns": 1, "steps": 2, "prompt_tokens": 10,
                           "completion_tokens": 1, "cached_tokens": 5})

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(tmp_path), "--format", "json"]
    )

    payload = json.loads(result.stdout)
    assert payload["totals"]["session"] == "(all)"
    assert payload["totals"]["turns"] == 1
    assert payload["rows"][0]["session"] == "sess-1"
    assert payload["rows"][0]["cache_ratio"] == 0.5


def test_r9_overview_since_filters_old_sessions(tmp_path):
    old = _session_with(tmp_path, "old", turns=1, summary={"turns": 1})
    _session_with(tmp_path, "new", turns=1, summary={"turns": 1})
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(tmp_path), "--since", "7"]
    )

    assert "new" in result.stdout
    assert "old" not in result.stdout


def test_r9_overview_on_an_empty_workspace(tmp_path):
    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "no traces recorded" in result.stdout


def test_r9_overview_flags_a_summary_mismatch(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1, summary={"turns": 9})
    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])
    assert "mismatch" in result.stdout.lower()


# ---- R9c: trace export (docs/r9-analysis.md 3.3) ----


def test_r9_export_html_writes_a_file(tmp_path):
    _session_with(tmp_path, "sess-1", turns=1, summary={"turns": 1})
    out = tmp_path / "trace.html"

    result = runner.invoke(
        app,
        ["trace", "export", "--format", "html", "--session", "sess-1",
         "--workspace", str(tmp_path), "--out", str(out)],
    )

    assert result.exit_code == 0
    body = out.read_text(encoding="utf-8")
    assert "<!doctype html>" in body and "sess-1" in body


def test_r9_export_an_unknown_session_reports_instead_of_crashing(tmp_path):
    result = runner.invoke(
        app,
        ["trace", "export", "--format", "html", "--session", "nope",
         "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "no traces" in result.stdout.lower() or "not found" in result.stdout.lower()
