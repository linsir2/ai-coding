"""M-G2: ``ai-coding trace overview`` — the CLI ``render_overview`` was missing.

R9b built ``render_overview``; R10 deleted the command that called it and left the
renderer exported in ``__all__`` with zero consumers.  This file pins the command,
and the "behaviourally equal to ``render_overview``" case is what keeps the
renderer consumed without asserting on source text.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from ai_coding.cli import app

runner = CliRunner()


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        + "Z"
    )


def _workspace(tmp_path: Path, *, sessions: dict[str, tuple[float, int]]) -> Path:
    """A workspace whose trace store holds one session per entry (ts, turns)."""
    root = tmp_path / ".ai-coding" / "traces"
    for name, (stamp, turns) in sessions.items():
        directory = root / name
        directory.mkdir(parents=True)
        for turn in range(1, turns + 1):
            (directory / f"turn-{turn}.jsonl").write_text(
                json.dumps({"v": 1, "ts": _iso(stamp + turn), "evt": "X"}) + "\n",
                encoding="utf-8",
            )
        (directory / "summary.json").write_text(
            json.dumps(
                {
                    "turns": turns,
                    "steps": turns,
                    "prompt_tokens": 100 * turns,
                    "completion_tokens": 10 * turns,
                    "cached_tokens": 80 * turns,
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


def test_the_trace_overview_cli_renders(tmp_path):
    ws = _workspace(tmp_path, sessions={"s1": (time.time(), 2)})

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "SESSION" in result.output
    assert "TOTAL" in result.output
    assert "s1" in result.output


def test_the_trace_overview_cli_matches_render_overview(tmp_path, monkeypatch):
    """The CLI must not grow a second renderer.

    Freezing the clock is not cosmetic: two ``build_report`` calls each read
    ``time.time()``, and ``age_days`` is printed to one decimal -- a run that
    straddles a 0.05-day boundary would differ by a digit.
    """
    import ai_coding.core.aggregate as aggregate_mod
    from ai_coding.app.trace_text import render_overview
    from ai_coding.core.aggregate import build_report
    from ai_coding.core.trace import TraceStore

    frozen = time.time()
    monkeypatch.setattr(aggregate_mod.time, "time", lambda: frozen)
    ws = _workspace(
        tmp_path, sessions={"s1": (frozen - 3 * 86400, 1), "s2": (frozen, 2)}
    )

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(ws)])
    expected = render_overview(
        build_report(TraceStore(ws / ".ai-coding" / "traces", workspace=ws))
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == expected.strip()


def test_the_trace_overview_cli_emits_json(tmp_path):
    ws = _workspace(tmp_path, sessions={"s1": (time.time(), 1)})

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(ws), "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["session"] for row in payload["sessions"]] == ["s1"]
    assert payload["totals"]["session"] == "(all)"


def test_the_trace_overview_cli_rejects_an_unknown_format(tmp_path):
    ws = _workspace(tmp_path, sessions={"s1": (time.time(), 1)})

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(ws), "--format", "xml"]
    )

    assert result.exit_code == 2
    assert "table or json" in result.output


def test_the_trace_overview_cli_on_an_empty_store(tmp_path):
    result = runner.invoke(app, ["trace", "overview", "--workspace", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "no traced sessions" in result.output


def test_the_trace_overview_cli_emits_json_for_an_empty_store(tmp_path):
    """`--format json` is a contract: an empty store is still a JSON document."""
    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(tmp_path), "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sessions"] == []
    assert payload["totals"]["session"] == "(all)"


def test_the_trace_overview_cli_since_filters(tmp_path):
    now = time.time()
    ws = _workspace(tmp_path, sessions={"old": (now - 30 * 86400, 1), "new": (now, 1)})

    result = runner.invoke(
        app, ["trace", "overview", "--workspace", str(ws), "--since", "7"]
    )

    assert result.exit_code == 0, result.output
    assert "new" in result.output
    assert "old" not in result.output
