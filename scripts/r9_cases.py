#!/usr/bin/env python3
"""R9 acceptance cases — offline by default, ``--real`` adds the live one.

Each offline case is one claim of the round, run against the real units with fakes
only where IO would be needed (docs/r9-analysis.md §7).  The live case is an
observation: it runs a real model turn and reports, it does not grade.

    .venv/bin/python scripts/r9_cases.py
    .venv/bin/python scripts/r9_cases.py --only gate_refuses_completion
    .venv/bin/python scripts/r9_cases.py --real --config config.local.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.core.aggregate import build_report  # noqa: E402
from ai_coding.core.export_html import (  # noqa: E402
    read_session_view,
    render_session_html,
)
from ai_coding.core.export_otlp import rows_to_otlp  # noqa: E402
from ai_coding.core.trace import TraceRecorder, TraceStore  # noqa: E402
from ai_coding.domain.task import TaskState  # noqa: E402
from ai_coding.tools.base import ToolContext  # noqa: E402
from ai_coding.tools.todo_write_tool import TodoWriteTool  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


# ------------------------------------------------------ the dependency gate ----


async def gate_refuses_completion(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    result = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    ok = result.error == "blocked_by_dependencies" and task.get("2").status == "pending"
    return ok, f"error={result.error} status={task.get('2').status}"


async def gate_refuses_start(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    result = await tool.execute({"action": "update", "id": "2", "status": "in_progress"}, ctx)
    ok = result.error == "blocked_by_dependencies" and task.get("2").status == "pending"
    return ok, f"error={result.error} status={task.get('2').status}"


async def gate_unblocks_after_completion(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    await tool.execute({"action": "update", "id": "1", "status": "completed"}, ctx)
    result = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    ok = result.success is True and task.get("2").status == "completed"
    return ok, f"error={result.error} status={task.get('2').status}"


async def gate_allows_after_drop(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    await tool.execute({"action": "update", "id": "1", "status": "dropped"}, ctx)
    result = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    return result.success is True, f"error={result.error} status={task.get('2').status}"


async def add_refuses_unknown_and_self(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    unknown = await tool.execute({"action": "add", "content": "b", "depends_on": ["9"]}, ctx)
    self_dep = await tool.execute({"action": "add", "content": "a", "depends_on": ["1"]}, ctx)
    ok = (
        unknown.error == "unknown_dependency"
        and self_dep.error == "self_dependency"
        and task.items == []
    )
    return ok, f"unknown={unknown.error} self={self_dep.error} items={len(task.items)}"


async def the_forward_reference_cycle_is_gone(tmp: Path) -> tuple[bool, str]:
    """The R9 analysis measured that this used to succeed (making a 1→2→1 cycle)."""
    task = TaskState(goal="g")
    tool = TodoWriteTool()
    ctx = ToolContext(task=task)
    first = await tool.execute({"action": "add", "content": "a", "depends_on": ["2"]}, ctx)
    ok = first.error == "unknown_dependency" and task.items == []
    return ok, f"error={first.error} items={len(task.items)}"


async def gate_can_be_switched_off(tmp: Path) -> tuple[bool, str]:
    task = TaskState(goal="g")
    tool = TodoWriteTool(enforce_dependencies=False)
    ctx = ToolContext(task=task)
    await tool.execute({"action": "add", "content": "dep"}, ctx)
    await tool.execute({"action": "add", "content": "next", "depends_on": ["1"]}, ctx)
    result = await tool.execute({"action": "update", "id": "2", "status": "completed"}, ctx)
    ok = result.success is True and task.get("2").status == "completed"
    return ok, f"legacy behaviour restored={ok}"


# ------------------------------------------------------- the three outlets ----


def _fake_store(tmp: Path, *, age_days: float = 30.0) -> TraceStore:
    """One session, one turn, one hostile tool output.

    ``age_days`` is written into the trace line's ``ts``, which is what
    ``SessionRef.last_activity`` reads -- the directory mtime no longer decides
    how old a session is.
    """
    stamp = datetime.fromtimestamp(
        time.time() - age_days * 86_400, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    store = TraceStore(tmp / "traces", workspace=tmp)
    writer = store.begin_turn("sess-1")
    writer.write_line(
        json.dumps(
            {
                "v": 1,
                "ts": stamp,
                "session": "sess-1",
                "turn": 1,
                "span": "c1",
                "parent": "i1",
                "iter": 1,
                "evt": "ToolStarted",
                "data": {
                    "tool_name": "bash",
                    "params": {"command": "echo '<script>alert(1)</script>'"},
                    "duration_ms": 12,
                },
            }
        )
    )
    (store.session_dir("sess-1") / "summary.json").write_text(
        json.dumps(
            {
                "turns": 1,
                "steps": 1,
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cached_tokens": 5,
            }
        ),
        encoding="utf-8",
    )
    return store


async def overview_totals(tmp: Path) -> tuple[bool, str]:
    report = build_report(_fake_store(tmp))
    ok = report.totals.turns == 1 and abs(report.cache_ratio - 0.5) < 1e-9
    return ok, f"turns={report.totals.turns} hit={report.cache_ratio:.2f}"


async def html_escapes_tool_output(tmp: Path) -> tuple[bool, str]:
    store = _fake_store(tmp)
    body = render_session_html(read_session_view(store, "sess-1"))
    ok = "&lt;script&gt;" in body and "<script>" not in body
    return ok, f"{len(body)} chars"


async def html_has_no_external_resources(tmp: Path) -> tuple[bool, str]:
    body = render_session_html(read_session_view(_fake_store(tmp), "sess-1"))
    ok = not any(token in body for token in ("http://", "https://", "<script", "<link"))
    return ok, "clean" if ok else "external resource found"


async def otlp_is_deterministic(tmp: Path) -> tuple[bool, str]:
    view = read_session_view(_fake_store(tmp), "sess-1")
    first = json.dumps(rows_to_otlp([view]), sort_keys=True)
    second = json.dumps(rows_to_otlp([view]), sort_keys=True)
    return first == second, f"{len(first)} chars"


async def prune_and_overview_agree(tmp: Path) -> tuple[bool, str]:
    """Both must read ``SessionRef.last_activity`` (D-R9-8, M-G2).

    The session's age lives in its own trace, so an ``rsync`` that rewrites the
    directory mtime cannot make ``prune`` and ``since_days`` disagree.
    """
    store = _fake_store(tmp, age_days=30.0)
    old = build_report(store, since_days=7).rows == ()
    return old and store.prune() == 1, "consistent"


OFFLINE: dict[str, CaseFn] = {
    "gate_refuses_completion": gate_refuses_completion,
    "gate_refuses_start": gate_refuses_start,
    "gate_unblocks_after_completion": gate_unblocks_after_completion,
    "gate_allows_after_drop": gate_allows_after_drop,
    "add_refuses_unknown_and_self": add_refuses_unknown_and_self,
    "the_forward_reference_cycle_is_gone": the_forward_reference_cycle_is_gone,
    "gate_can_be_switched_off": gate_can_be_switched_off,
    "overview_totals": overview_totals,
    "html_escapes_tool_output": html_escapes_tool_output,
    "html_has_no_external_resources": html_has_no_external_resources,
    "otlp_is_deterministic": otlp_is_deterministic,
    "prune_and_overview_agree": prune_and_overview_agree,
}


# ---------------------------------------------------------------- live ----+


async def _real_loop_with_trace(
    config_path: str, workspace: Path
) -> tuple[Any, TraceStore, Any]:
    """The REPL's stack, plus a recorder, so the report has something to read."""
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.config.loader import ConfigManager
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.memory.service import MemoryService
    from ai_coding.memory.store import MemoryStore
    from ai_coding.security.approval import AutoApproveCallback
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.builder import build_tool_registry
    from ai_coding.tools.checkpoint_tool import CheckpointTool
    from ai_coding.tools.memory_search_tool import MemorySearchTool
    from ai_coding.tools.memory_write_tool import MemoryWriteTool

    manager = ConfigManager()
    manager.initialize(config_path)
    app_cfg = manager.app
    registry = build_tool_registry(app_cfg, str(workspace))
    sessions = SessionService(workspace / "sessions")
    session = sessions.create()
    engine = build_engine_from_config(app_cfg)
    memory = MemoryService(MemoryStore(workspace / ".memory"))
    for tool in (MemorySearchTool(memory), MemoryWriteTool(memory), CheckpointTool()):
        if registry.get(tool.name) is None:
            registry.register(tool)
    store = TraceStore(workspace / ".ai-coding" / "traces", workspace=workspace)
    recorder = TraceRecorder(store, session_id=session.session_id)
    loop = AgentLoop(
        engine,
        sessions,
        registry,
        memory=memory,
        approval=AutoApproveCallback(),
        observer=recorder,
        trace=recorder,
        settings=app_cfg.ai,
        workspace=str(workspace),
    )
    return loop, store, session


async def real_report_includes_the_turn(tmp: Path) -> tuple[bool, str]:
    """A real model turn lands in the trace, and the report counts it."""
    config_path = os.environ.get("R9_CONFIG", "config.local.yaml")
    loop, store, session = await _real_loop_with_trace(config_path, tmp)
    try:
        answer = await loop.process_input(session, "用一句话说明这个仓库是做什么的。")
    finally:
        await loop.aclose()
    report = build_report(store)
    ok = bool(answer) and report.totals.turns >= 1
    return ok, (
        f"answer={len(answer)} chars, sessions={len(report.rows)}, "
        f"turns={report.totals.turns}, prompt={report.totals.prompt_tokens}"
    )


LIVE: dict[str, CaseFn] = {"real_report_includes_the_turn": real_report_includes_the_turn}


# ----------------------------------------------------------------- main ----


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    parser.add_argument("--real", action="store_true", help="also run the live cases")
    parser.add_argument("--config", default="config.local.yaml")
    args = parser.parse_args(argv)

    os.environ["R9_CONFIG"] = args.config
    cases: dict[str, CaseFn] = dict(OFFLINE)
    if args.real:
        cases.update(LIVE)
    if args.only:
        cases = {name: fn for name, fn in cases.items() if name == args.only}
        if not cases:
            print(f"no such case: {args.only}")
            return 2

    passed = failed = offline_failed = 0
    for name, case in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            try:
                ok, detail = await case(Path(directory))
            except Exception as exc:  # noqa: BLE001 — a case must not kill the script
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
        passed += ok
        failed += not ok
        if not ok and name in OFFLINE:
            offline_failed += 1

    print(SEP)
    print(
        f"SUMMARY {passed}/{passed + failed} passed "
        f"({len(OFFLINE)} offline, live cases report, they are not graded)"
    )
    return 1 if offline_failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
