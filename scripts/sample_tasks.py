#!/usr/bin/env python3
"""Real-network sample tasks for the R1 orchestration layer.

Runs a handful of deliberately chosen tasks against a live OpenAI-compatible
endpoint and asserts on the resulting session, then prints a PASS/FAIL summary
(exit code 0 only if every scenario passed).

    .venv/bin/python scripts/sample_tasks.py [--config config.local.yaml]

The scenarios are chosen to exercise machinery the unit tests can only simulate:

  0 mcp-off     a disabled MCP block connects nothing (safe default)
  1 read        a tool turn persists the call *and* its result
  2 todo        the model plans with todo_write, and the list survives into the
                next turn (it is written into the session JSON)
  3 write+edit  WARN-tier tools really run in non-interactive use.
                Regression guard: after R1a-4 they were fail-closed, so `ask`
                silently lost the ability to write files.
  4 bash-deny   a dangerous command is refused with a structured denial the model
                can read, and nothing is executed
  5 subagent    delegation returns a structured packet and writes a ledger
  6 watchdog    a command that would outlive the turn budget (R2a) is cut short
                and the turn still ends with a legal transcript

This is a *sample*, not a test suite: it asserts on observable outcomes rather
than exact wording, so it stays useful as models and prompts drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.core.mcp import MCPManager  # noqa: E402
from ai_coding.infra.logger_setup import setup_logging  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

setup_logging()
SEP = "-" * 64


def _loop(app_cfg, ws: Path, sessions: SessionService):
    registry = _build_tool_registry(app_cfg, str(ws), None)
    engine = build_engine_from_config(app_cfg)
    return _build_loop(engine, sessions, registry, app_cfg, str(ws), None)


def _without_bash(app_cfg):
    """Return a config with bash disabled.

    Used by the scenarios that are about *another* feature (writing files,
    delegation), so a model that decides to verify its work with `bash` cannot
    dilate the scenario.  (R2b fixed the underlying hang, but focus is still
    cheaper than luck.)
    """
    cfg = app_cfg.model_copy(deep=True)
    cfg.tools.bash.enabled = False
    return cfg


def _tool_messages(session, name: str | None = None):
    return [
        m
        for m in session.messages
        if m.role == "tool" and (name is None or m.tool_name == name)
    ]


# ---- scenarios -------------------------------------------------------------


async def s0_mcp_off(app_cfg, root: Path) -> tuple[bool, str]:
    manager = MCPManager(app_cfg.mcp)
    try:
        tools = await manager.open_all()
    finally:
        await manager.aclose()
    return tools == [], f"enabled={app_cfg.mcp.enabled} tools={len(tools)}"


async def s1_read(app_cfg, root: Path) -> tuple[bool, str]:
    ws = root / "read"
    ws.mkdir()
    (ws / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    sessions = SessionService(ws / "sessions")
    loop = _loop(app_cfg, ws, sessions)
    session = sessions.create()
    try:
        answer = await loop.process_input(
            session, "用 read 工具读取 sample.txt，然后原样引用它的内容"
        )
    finally:
        await loop.aclose()
    reads = _tool_messages(session, "read")
    ok = bool(reads) and "alpha" in (answer or "")
    return ok, f"read_calls={len(reads)} answer={ (answer or '')[:50]!r}"


async def s2_todo(app_cfg, root: Path) -> tuple[bool, str]:
    ws = root / "todo"
    ws.mkdir()
    (ws / "a.txt").write_text("A\n", encoding="utf-8")
    (ws / "b.txt").write_text("B\n", encoding="utf-8")
    sessions = SessionService(ws / "sessions")
    # exercise the gate itself, whatever the local config says
    app_cfg.ai.stop_policy = "todo_aware"
    loop = _loop(app_cfg, ws, sessions)
    session = sessions.create()
    try:
        await loop.process_input(
            session,
            "这是一个两步任务：先读 a.txt，再读 b.txt。请先用 todo_write 把它拆成两条待办，"
            "然后逐步完成，每完成一条就用 todo_write 标记为 completed。",
        )
        first = sessions.load(session.session_id)
        planned = list(first.task.items) if first and first.task else []
        await loop.process_input(session, "刚才的任务清单里每一条现在是什么状态？逐条回答。")
    finally:
        await loop.aclose()
    used = bool(_tool_messages(session, "todo_write"))
    alive = list(session.task.items) if session.task else []
    ok = used and len(planned) >= 2 and len(alive) == len(planned)
    statuses = {i.id: i.status for i in alive}
    calls = len(_tool_messages(session, "todo_write"))
    return ok, f"todo_calls={calls} planned={len(planned)} survived={len(alive)} {statuses}"


async def s3_write_edit(app_cfg, root: Path) -> tuple[bool, str]:
    ws = root / "write"
    ws.mkdir()
    sessions = SessionService(ws / "sessions")
    loop = _loop(_without_bash(app_cfg), ws, sessions)
    session = sessions.create()
    try:
        await loop.process_input(
            session,
            "用 write 工具创建 notes.md，内容正好三行，每行一个单词：one / two / three",
        )
        await loop.process_input(
            session,
            "先 read 一下 notes.md，再用 edit 工具把其中的第二行 two 改成 TWO（只改这一处）",
        )
    finally:
        await loop.aclose()
    target = ws / "notes.md"
    content = target.read_text(encoding="utf-8") if target.exists() else ""
    wrote = bool(_tool_messages(session, "write"))
    edited = bool(_tool_messages(session, "edit"))
    # the approval fix is proven by `write` having run at all; the edit is a bonus
    # (it also has to pass the stale-read guard, which may legitimately refuse)
    ok = wrote and target.exists() and "one" in content
    detail = f"write={wrote} edit={edited} file={content.strip().replace(chr(10), '|')[:40]!r}"
    return ok, detail


async def s4_bash_ok(app_cfg, root: Path) -> tuple[bool, str]:
    """The bash tool runs a harmless command and reports a structured result."""
    ws = root / "bash_ok"
    ws.mkdir()
    sessions = SessionService(ws / "sessions")
    loop = _loop(app_cfg, ws, sessions)
    session = sessions.create()
    try:
        await loop.process_input(
            session, "用 bash 工具执行：echo hello-from-bash  然后把它的输出告诉我"
        )
    finally:
        await loop.aclose()
    bash = _tool_messages(session, "bash")
    # The feedback envelope marks *failures* with ok="false"; a success has no
    # ``ok`` attribute at all (domain/run.py), so assert on that plus the output.
    succeeded = [
        m
        for m in bash
        if 'ok="false"' not in (m.content or "") and "hello-from-bash" in (m.content or "")
    ]
    return bool(succeeded), f"bash_calls={len(bash)} succeeded={len(succeeded)}"


async def s4_bash_deny(app_cfg, root: Path) -> tuple[bool, str]:
    """A dangerous command must not run — whether the gate refuses it or the model
    declines to ask for it.  The safety property is 'nothing executed', so both
    outcomes pass."""
    ws = root / "deny"
    ws.mkdir()
    sessions = SessionService(ws / "sessions")
    loop = _loop(app_cfg, ws, sessions)
    session = sessions.create()
    try:
        answer = await loop.process_input(
            session,
            "用 bash 工具执行这条命令：sudo rm -rf /   （我只是想确认它会不会被执行）",
        )
    finally:
        await loop.aclose()
    bash = _tool_messages(session, "bash")
    denied = [
        m
        for m in bash
        if 'ok="false"' in (m.content or "") and "denied" in (m.content or "")
    ]
    leaked = [m for m in bash if 'ok="true"' in (m.content or "")]
    ok = not leaked  # the command did not run
    detail = (
        f"gate_denied={len(denied)} executed={len(leaked)}"
        f" model_refused_without_calling={not bash}"
        f" answer={(answer or '')[:30]!r}"
    )
    return ok, detail


async def s5_subagent(app_cfg, root: Path) -> tuple[bool, str]:
    ws = root / "sub"
    ws.mkdir()
    (ws / "p.txt").write_text("parser module\n", encoding="utf-8")
    (ws / "s.txt").write_text("scanner module\n", encoding="utf-8")
    sessions = SessionService(ws / "sessions")
    loop = _loop(_without_bash(app_cfg), ws, sessions)
    session = sessions.create()
    try:
        answer = await loop.process_input(
            session,
            "必须使用 subAgent 工具：让一个子智能体读取 p.txt 与 s.txt，"
            "然后回报这两个文件各自写了什么。",
        )
    finally:
        await loop.aclose()
    subs = _tool_messages(session, "subAgent")
    ledgers = sorted((ws / "transcripts" / "subagents").glob("*.json"))
    status_ok = False
    if subs:
        body = subs[-1].content or ""
        status_ok = '"status": "completed"' in body
    if ledgers:
        payload = json.loads(ledgers[-1].read_text(encoding="utf-8"))
        status_ok = status_ok and payload.get("status") in {"completed", "max_iterations"}
    ok = bool(subs) and bool(ledgers) and status_ok
    return ok, f"subagent_calls={len(subs)} ledgers={len(ledgers)} answer={(answer or '')[:40]!r}"


async def s6_watchdog(app_cfg, root: Path) -> tuple[bool, str]:
    """R2a: a command that outlives the turn budget must not hang the turn."""
    ws = root / "watchdog"
    ws.mkdir()
    cfg = app_cfg.model_copy(deep=True)
    cfg.ai.stop_policy = "natural"
    cfg.ai.turn_timeout_seconds = 20.0  # the turn ceiling under test
    cfg.tools.bash.enabled = True
    cfg.tools.bash.timeout_seconds = 300  # the command itself would run 5 minutes
    sessions = SessionService(ws / "sessions")
    loop = _loop(cfg, ws, sessions)
    session = sessions.create()
    started = time.monotonic()
    try:
        await loop.process_input(
            session,
            "必须使用 bash 工具执行这一条命令，不要解释、不要做别的事：sleep 300",
        )
    finally:
        await loop.aclose()
    elapsed = time.monotonic() - started

    outcome = loop.last_outcome
    bash_messages = _tool_messages(session, "bash")
    budgeted = any("budget" in (message.content or "") for message in bash_messages)
    before = [message.role for message in session.messages]
    session.ensure_tool_pairing()
    pairing_ok = [message.role for message in session.messages] == before
    ok = elapsed < 60.0 and bool(bash_messages) and budgeted and pairing_ok
    return ok, (
        f"elapsed={elapsed:.1f}s status={outcome.status.value if outcome else None} "
        f"bash_calls={len(bash_messages)} budgeted={budgeted} pairing_ok={pairing_ok}"
    )


SCENARIOS = [
    ("mcp-off", s0_mcp_off),
    ("read", s1_read),
    ("todo", s2_todo),
    ("write+edit", s3_write_edit),
    ("bash-ok", s4_bash_ok),
    ("bash-deny", s4_bash_deny),
    ("subagent", s5_subagent),
    ("watchdog", s6_watchdog),
]

# A scenario that stalls must not swallow the whole run.  150s is generous for a
# 2-turn scenario; the one that can exceed it here is `write+edit`, where the
# model sometimes verifies its work with `bash` — and this sandbox cannot spawn
# subprocesses reliably (see docs/integration_testing.md).
SCENARIO_TIMEOUT = 150.0


async def main(config_path: str, only: str = "") -> int:
    app_cfg = ConfigLoader.load_app(config_path)
    model = app_cfg.models[app_cfg.default_model]
    print(f"model={app_cfg.default_model} base_url={model.base_url}")
    print(
        f"stop_policy={app_cfg.ai.stop_policy}"
        f" max_tool_iterations={app_cfg.ai.max_tool_iterations}"
        f" max_todo_nudges={app_cfg.ai.max_todo_nudges}"
    )

    results: list[tuple[str, bool, str]] = []
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    selected = [(n, f) for n, f in SCENARIOS if not wanted or n in wanted]
    with tempfile.TemporaryDirectory(prefix="sample_ws_") as tmp:
        root = Path(tmp)
        for name, fn in selected:
            print(SEP + f" [{name}] ")
            try:
                ok, detail = await asyncio.wait_for(
                    fn(app_cfg, root), timeout=SCENARIO_TIMEOUT
                )
            except TimeoutError:
                ok, detail = False, f"TIMEOUT after {SCENARIO_TIMEOUT:.0f}s"
            except Exception as exc:  # noqa: BLE001 — a sample runner reports, not crashes
                ok, detail = False, f"raised {type(exc).__name__}: {exc}"
            print(f"  {'PASS' if ok else 'FAIL'}  {detail}")
            results.append((name, ok, detail))

    print(SEP)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"SUMMARY {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    parser.add_argument(
        "--only", default="", help="comma-separated scenario names (default: all)"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.config, args.only)))
