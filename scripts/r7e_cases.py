#!/usr/bin/env python3
"""R7e acceptance cases — offline by default, ``--real`` adds the live ones.

Each offline case is one claim of the round, run against the real units with fakes
only where IO would be needed (docs/r7e-analysis.md §8).  The live cases are the two
things that need a terminal plus one real model turn.

    .venv/bin/python scripts/r7e_cases.py
    .venv/bin/python scripts/r7e_cases.py --only steering_line_becomes_an_instruction
    .venv/bin/python scripts/r7e_cases.py --real --config config.local.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pty
import re
import select
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.config.models import AIConfig, AppConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.core.events import RecordingObserver, ToolSurfaceWarning  # noqa: E402
from ai_coding.core.steering import SteeringQueue  # noqa: E402
from ai_coding.core.subagent import SubAgentRunner  # noqa: E402
from ai_coding.domain.loop import StepResult  # noqa: E402
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult  # noqa: E402
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.domain.subagent import ParentContext  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.security.approval import AutoApproveCallback  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402
from ai_coding.tools.builder import build_tool_registry  # noqa: E402
from ai_coding.tools.memory_write_tool import MemoryWriteTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


# ---------------------------------------------------------------- fakes ----


class _Engine(ChatEngine):
    """One tool call on the first step, then speak."""

    def __init__(self, *, tool: str | None = "poke") -> None:
        self.tool = tool
        self.requests: list[dict[str, Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "instructions": instructions})
        if self.tool is not None and len(self.requests) == 1:
            return StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name=self.tool, arguments="{}")],
                finish_reason="tool_calls",
            )
        return StepResult(text="好", tool_calls=[], finish_reason="stop")


class _Poke(BaseTool):
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
    def save(self, session: SessionData) -> None:
        return None

    def mark_pending_approval(self, session_id: str, request: object) -> None:
        """P3: the real SessionService writes this marker beside the session
        file; these cases never touch disk."""
        return None


def _session() -> SessionData:
    return SessionData(session_id="r7e", title="", created_time="t", last_access_time="t")


def _loop(
    engine: ChatEngine,
    *,
    steering: SteeringQueue | None = None,
    observer: RecordingObserver | None = None,
    tools: tuple[BaseTool, ...] = (_Poke(),),
    **overrides: Any,
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural", **overrides),
        observer=observer,
        steering=steering,
    )


# -------------------------------------------------------------- offline ----


async def case_memory_write_adds_and_dedupes(root: Path) -> tuple[bool, str]:
    service = MemoryService(MemoryStore(root / ".memory"))
    tool = MemoryWriteTool(service)
    first = await tool.execute({"text": "CI 缓存改走 uv", "kind": "decision"}, ToolContext())
    second = await tool.execute({"text": "CI 缓存改走 uv", "kind": "decision"}, ToolContext())
    ok = (
        first.success
        and first.data["action"] == "added"
        and second.data["action"] == "skipped"
        and second.data["note_id"] == first.data["note_id"]
        and len(service.store.all()) == 1
    )
    notes = len(service.store.all())
    return ok, f"added={first.data['note_id'][:8]} skipped→same id, notes={notes}"


async def case_memory_write_is_not_visible_to_a_subagent(root: Path) -> tuple[bool, str]:
    stack = build_tool_registry(AppConfig(), str(root), None, include_subagent=False)
    ok = stack.get("memory_write") is None and stack.get("memory_search") is None
    return ok, f"child stack tools={stack.list_names()}"


async def case_tools_chars_warning_fires_once(root: Path) -> tuple[bool, str]:
    class _Fat(_Poke):
        @property
        def description(self) -> str:
            return "y" * 3000

        @property
        def origin(self) -> str:
            return "mcp"  # r22: only the MCP part is judged

    observer = RecordingObserver()
    loop = _loop(_Engine(), observer=observer, tools=(_Fat(),), mcp_chars_warn=100)
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    warnings = observer.of_type(ToolSurfaceWarning)
    ok = len(warnings) == 1 and warnings[0].mcp_chars > warnings[0].threshold
    return ok, f"warnings={len(warnings)} mcp_chars={warnings[0].mcp_chars if warnings else 0}"


async def case_tools_chars_warning_keeps_the_request_identical(root: Path) -> tuple[bool, str]:
    class _Fat(_Poke):
        @property
        def description(self) -> str:
            return "y" * 3000

        @property
        def origin(self) -> str:
            return "mcp"  # r22: only the MCP part is judged

    quiet_engine, loud_engine = _Engine(), _Engine()
    await _loop(quiet_engine, tools=(_Fat(),), mcp_chars_warn=0).process_input(_session(), "一")
    await _loop(loud_engine, tools=(_Fat(),), mcp_chars_warn=100).process_input(_session(), "一")
    ok = [m.content for m in quiet_engine.requests[-1]["messages"]] == [
        m.content for m in loud_engine.requests[-1]["messages"]
    ]
    return ok, f"requests identical: {ok}"


async def case_steering_line_becomes_an_instruction(root: Path) -> tuple[bool, str]:
    queue = SteeringQueue()
    queue.put("改用 uv")
    engine = _Engine()
    session = _session()
    await _loop(engine, steering=queue).process_input(session, "改构建")
    persisted = [m.content or "" for m in session.messages]
    seen = [m.content or "" for m in engine.requests[1]["messages"]]
    ok = any("[steering] 改用 uv" in text for text in persisted) and any(
        "[steering] 改用 uv" in text for text in seen
    )
    where = f"persisted={any('[steering]' in t for t in persisted)}"
    return ok, f"{where} seen_by_model={any('[steering]' in t for t in seen)}"


async def case_steering_keeps_the_rest_queued(root: Path) -> tuple[bool, str]:
    queue = SteeringQueue(max_messages=1)
    queue.put("第一条")
    queue.put("第二条")
    await _loop(_Engine(tool=None), steering=queue).process_input(_session(), "一")
    ok = queue.pending == 1
    return ok, f"leftover in queue: {queue.pending}"


async def case_steering_resets_the_streak_only(root: Path) -> tuple[bool, str]:
    """策略层：转向重置失败连胜，但不改工具计数（用账本本身断言）。"""
    from ai_coding.core.turn_books import TurnBooks

    books = TurnBooks(failed_streak=2, attempted_calls=3, successful_calls=0)
    books.note_steering(2)
    ok = (
        books.steering_messages == 2
        and books.failed_streak == 0
        and books.attempted_calls == 3
        and books.successful_calls == 0
    )
    return ok, (
        f"streak={books.failed_streak} attempted={books.attempted_calls} "
        f"steering={books.steering_messages}"
    )


async def case_subagent_task_only_has_a_clean_window(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool=None)
    cfg = AIConfig(subagent_context_mode="task_only", max_tool_iterations=2)
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
        base_instructions="BASE",
    )
    result = await runner.run(
        "只做这一件事",
        parent=ParentContext(
            messages=(ChatMessage(role="user", content="parent-said-this", timestamp="t"),),
            instructions="PARENT",
        ),
    )
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    ok = (
        not any("parent-said-this" in c for c in contents)
        and engine.requests[0]["instructions"] == "BASE"
        and result.context_mode == "task_only"
    )
    leaked = any("parent-said-this" in c for c in contents)
    return ok, f"mode={result.context_mode} parent_leaked={leaked}"


async def case_subagent_full_still_inherits_the_parent(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool=None)
    cfg = AIConfig(max_tool_iterations=2)
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
    )
    await runner.run(
        "接着做",
        parent=ParentContext(
            messages=(ChatMessage(role="user", content="parent-said-this", timestamp="t"),),
            instructions="PARENT",
        ),
    )
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    ok = (
        any("parent-said-this" in c for c in contents)
        and engine.requests[0]["instructions"] == "PARENT"
    )
    visible = any("parent-said-this" in c for c in contents)
    return ok, f"mode={runner.context_mode} parent_visible={visible}"


OFFLINE: dict[str, CaseFn] = {
    "memory_write_adds_and_dedupes": case_memory_write_adds_and_dedupes,
    "memory_write_is_not_visible_to_a_subagent": case_memory_write_is_not_visible_to_a_subagent,
    "tools_chars_warning_fires_once": case_tools_chars_warning_fires_once,
    "tools_chars_warning_keeps_the_request_identical": (
        case_tools_chars_warning_keeps_the_request_identical
    ),
    "steering_line_becomes_an_instruction": case_steering_line_becomes_an_instruction,
    "steering_keeps_the_rest_queued": case_steering_keeps_the_rest_queued,
    "steering_resets_the_streak_only": case_steering_resets_the_streak_only,
    "subagent_task_only_has_a_clean_window": case_subagent_task_only_has_a_clean_window,
    "subagent_full_still_inherits_the_parent": case_subagent_full_still_inherits_the_parent,
}


# ----------------------------------------------------------------- live ----


def _read_until(
    master: int, needle: str, deadline: float, buffer: bytes = b"", occ: int = 1
) -> bytes:
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.3)
        if not ready:
            if buffer.count(needle.encode()) >= occ:
                break
            continue
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        if buffer.count(needle.encode()) >= occ:
            break
    return buffer


def _parse_result(text: str) -> list[str] | None:
    clean = ANSI.sub("", text).replace("\r", "")
    lines = [line for line in clean.splitlines() if line.startswith("RESULT ")]
    if not lines:
        return None
    return list(json.loads(lines[-1][len("RESULT ") :]))


def _drive_pty(mode: str, *, mid_turn_line: str = "", answer: str = "") -> list[str] | None:
    child = ROOT / "tests" / "_pty_reader_child.py"
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, str(child), mode],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    buffer = b""
    deadline = time.monotonic() + 60
    try:
        buffer = _read_until(master, "you>", deadline, buffer)
        os.write(master, "改一下构建\n".encode())
        if mid_turn_line:
            time.sleep(0.7)  # the model is inside the slow tool by now
            os.write(master, (mid_turn_line + "\n").encode())
        if answer:
            buffer = _read_until(master, "改 A 还是 B？", deadline, buffer)
            os.write(master, (answer + "\n").encode())
        buffer = _read_until(master, "you>", deadline, buffer, occ=2)
        os.write(master, b"/quit\n")
        buffer = _read_until(master, "RESULT ", deadline, buffer)
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
        os.close(master)
    return _parse_result(buffer.decode(errors="replace"))


async def real_pty_steering(root: Path) -> tuple[bool, str]:
    """A line typed mid-turn lands in the transcript as [steering]."""
    messages = await asyncio.to_thread(
        _drive_pty, "steering", mid_turn_line="改用 uv，不要 pip"
    )
    ok = bool(messages) and any("[steering]" in message for message in messages)
    return ok, f"transcript={[m[:34] for m in (messages or [])]}"


async def real_pty_ask_user_through_reader(root: Path) -> tuple[bool, str]:
    """A question and a steering line share the one prompt the reader owns."""
    messages = await asyncio.to_thread(_drive_pty, "question", answer="改 A")
    ok = bool(messages) and any('"answer": "改 A"' in message for message in messages)
    return ok, f"transcript={[m[:34] for m in (messages or [])]}"


async def real_turn_still_works(root: Path) -> tuple[bool, str]:
    """One real model turn through the composition root."""
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.cli import _build_loop, _build_tool_registry, _make_recorder
    from ai_coding.config.loader import ConfigManager
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService

    config_path = ROOT / "config.local.yaml"
    if not config_path.is_file():
        return True, "no config.local.yaml — skipped (report only)"
    manager = ConfigManager()
    manager.initialize(str(config_path))
    app_cfg = manager.app
    app_cfg.ai.max_tool_iterations = 3
    engine = build_engine_from_config(app_cfg)
    recorder = _make_recorder(app_cfg, str(ROOT), None)
    registry = _build_tool_registry(
        app_cfg, str(ROOT), None, user_prompt=CLIUserPromptCallback()
    )
    loop = _build_loop(
        engine,
        SessionService(root / "sessions"),
        registry,
        app_cfg,
        str(ROOT),
        None,
        observer=recorder,
        trace=recorder,
    )
    try:
        answer = await loop.process_input(_session(), "用一句话说明这个仓库是做什么的。")
    finally:
        await loop.aclose()
    outcome = loop.last_outcome
    ok = bool(answer) and outcome is not None
    return ok, (
        f"{len(answer)} chars, status={outcome.status.value if outcome else '?'}, "
        f"tools={sorted(registry.list_names())}"
    )


LIVE: dict[str, CaseFn] = {
    "pty_steering": real_pty_steering,
    "pty_ask_user_through_reader": real_pty_ask_user_through_reader,
    "turn_still_works": real_turn_still_works,
}


# ----------------------------------------------------------------- main ----


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    parser.add_argument("--real", action="store_true", help="also run the live cases")
    parser.add_argument("--config", default="config.local.yaml")
    args = parser.parse_args(argv)

    cases: dict[str, CaseFn] = dict(OFFLINE)
    if args.real:
        cases.update(LIVE)
    if args.only:
        cases = {name: fn for name, fn in cases.items() if name == args.only}
        if not cases:
            print(f"no such case: {args.only}")
            return 2

    passed = failed = 0
    offline_failed = 0
    for name, case in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            try:
                ok, detail = await case(Path(directory))
            except Exception as exc:  # noqa: BLE001 — a case must not kill the script
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name}: {detail}")
        passed += ok
        failed += not ok
        if not ok and name in OFFLINE:
            offline_failed += 1

    print(SEP)
    print(f"SUMMARY {passed}/{passed + failed} passed (live cases report, they are not graded)")
    return 1 if offline_failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
