"""M-G2: ``ai-coding trace overview`` — the CLI ``render_overview`` was missing.

R9b built ``render_overview``; R10 deleted the command that called it and left the
renderer exported in ``__all__`` with zero consumers.  This file pins the command,
and the "behaviourally equal to ``render_overview``" case is what keeps the
renderer consumed without asserting on source text.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_coding.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Never read the machine's real config while judging config behaviour."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("AI_CODING_TRUST_PROJECT", raising=False)


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


def _seed_store(root: Path, sessions: dict[str, tuple[float, int]]) -> None:
    for name, (stamp, turns) in sessions.items():
        directory = root / name
        directory.mkdir(parents=True)
        for turn in range(1, turns + 1):
            (directory / f"turn-{turn}.jsonl").write_text(
                json.dumps({"v": 1, "ts": _iso(stamp + turn), "evt": "X"}) + "\n",
                encoding="utf-8",
            )


def test_the_overview_reads_the_configured_trace_dir(tmp_path, monkeypatch):
    """X-02b: the writer honours ``tracing.dir``, so the reader must too.

    Both stores are populated on purpose: reading the default one would still
    print a table, and only asserting which session appears can fail.
    """
    custom = tmp_path / "custom-traces"
    _seed_store(custom, {"configured-session": (time.time(), 1)})
    ws = _workspace(tmp_path, sessions={"default-session": (time.time(), 1)})

    config_home = Path(os.environ["XDG_CONFIG_HOME"]) / "ai-coding"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        f"tracing:\n  dir: {custom}\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "configured-session" in result.output
    assert "default-session" not in result.output


def test_without_a_configured_dir_the_default_store_is_still_read(tmp_path):
    ws = _workspace(tmp_path, sessions={"default-session": (time.time(), 1)})

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "default-session" in result.output


def test_the_overview_honours_an_explicit_config_path(tmp_path):
    """X-02b: ``-p`` must reach the reader as well as the recorder.

    The explicit file is the only layer, so nothing under the user config home
    can make this pass by accident.
    """
    custom = tmp_path / "explicit-traces"
    _seed_store(custom, {"explicit-session": (time.time(), 1)})
    ws = _workspace(tmp_path, sessions={"default-session": (time.time(), 1)})
    config = tmp_path / "custom.yaml"
    config.write_text(f"tracing:\n  dir: {custom}\n", encoding="utf-8")

    result = runner.invoke(
        app, ["-p", str(config), "trace", "overview", "--workspace", str(ws)]
    )

    assert result.exit_code == 0, result.output
    assert "explicit-session" in result.output
    assert "default-session" not in result.output


def test_a_relative_trace_dir_is_resolved_against_the_workspace(
    tmp_path, monkeypatch
):
    """A relative ``tracing.dir`` belongs to the workspace, not the process CWD."""
    ws = tmp_path / "proj"
    ws.mkdir()
    _seed_store(ws / "custom-traces", {"workspace-relative": (time.time(), 1)})
    # The same relative name beside the process CWD: without the fix this is
    # the store the reader would pick, so the assertion below can fail.
    _seed_store(tmp_path / "custom-traces", {"process-cwd": (time.time(), 1)})

    config_home = Path(os.environ["XDG_CONFIG_HOME"]) / "ai-coding"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text(
        "tracing:\n  dir: custom-traces\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "workspace-relative" in result.output
    assert "process-cwd" not in result.output


def test_the_overview_says_when_it_falls_back_to_the_default_directory(
    tmp_path, monkeypatch
):
    from ai_coding.config.loader import ConfigLoader

    ws = _workspace(tmp_path, sessions={"s1": (time.time(), 1)})

    def _broken(*args, **kwargs):
        raise ValueError("unreadable config")

    monkeypatch.setattr(
        ConfigLoader, "load_layers_report", staticmethod(_broken)
    )

    result = runner.invoke(app, ["trace", "overview", "--workspace", str(ws)])

    assert result.exit_code == 0, result.output
    assert "default trace directory" in result.output
    assert "s1" in result.output, "a broken config must not hide the traces"
