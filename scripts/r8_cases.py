#!/usr/bin/env python3
"""R8 acceptance cases — offline by default, ``--real`` adds the live ones.

Each offline case is one claim of the round, run against the real units with
fakes only where IO would be needed (docs/r8-analysis.md §7).  The live cases are
observations: they run a real model turn and report, they do not grade.

    .venv/bin/python scripts/r8_cases.py
    .venv/bin/python scripts/r8_cases.py --only cancel_kills_the_group
    .venv/bin/python scripts/r8_cases.py --real --config config.local.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.config.models import AIConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.core.dispatch import ToolDispatcher  # noqa: E402
from ai_coding.core.events import ToolChunk  # noqa: E402
from ai_coding.core.turn_books import TurnBooks  # noqa: E402
from ai_coding.domain.loop import StepResult, TurnStatus  # noqa: E402
from ai_coding.domain.message import (  # noqa: E402
    CANCELLED_BY_USER,
    ChatMessage,
    ToolCallRef,
    ToolResult,
)
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.domain.turn_digest import build_digest  # noqa: E402
from ai_coding.infra.subprocess_runner import run_process  # noqa: E402
from ai_coding.memory.note import MemoryNote  # noqa: E402
from ai_coding.memory.relevance import RecallParams, rank_notes  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.security.approval import AutoApproveCallback  # noqa: E402
from ai_coding.security.permission_gate import PermissionGate  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402
from ai_coding.tools.memory_search_tool import MemorySearchTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


class _Engine(ChatEngine):
    """One scripted tool call, then a final answer."""

    def __init__(self, tool: str | None = None, arguments: str = "{}") -> None:
        self.tool = tool
        self.arguments = arguments
        self.steps = 0
        self.requests: list[list[ChatMessage]] = []
        self.instructions: list[str | None] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.steps += 1
        self.requests.append(list(messages))
        self.instructions.append(instructions)
        if self.tool is not None and self.steps == 1:
            return StepResult(
                text=None,
                tool_calls=[
                    ToolCallRef(id="c1", name=self.tool, arguments=self.arguments)
                ],
                finish_reason="tool_calls",
            )
        return StepResult(text="收尾了", finish_reason="stop")


class _CheckpointTool(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "checkpoint_write"

    @property
    def description(self) -> str:
        return "checkpoint"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"note": {"type": "string"}}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        note = str(params.get("note", ""))
        if ctx is not None and ctx.note_checkpoint is not None:
            ctx.note_checkpoint(note)
        return ToolResult(success=True, output=f"checkpoint: {note}")


class _ChunkyTool(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "chunky"

    @property
    def description(self) -> str:
        return "chunky"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        if ctx is not None:
            ctx.emit(
                ToolChunk(
                    call_id=ctx.call_id,
                    tool_name="chunky",
                    stream="stdout",
                    text="secret-chunk\n",
                    seq=0,
                )
            )
        return ToolResult(success=True, output="final-output")


class _Sessions:
    def __init__(self) -> None:
        self.session = SessionData(
            session_id="r8", title="", created_time="t", last_access_time="t"
        )

    def create(self) -> SessionData:
        return self.session

    def save(self, session: SessionData) -> None:
        return None

    def mark_pending_approval(self, session_id: str, request: object) -> None:
        """P3: the real SessionService writes this marker beside the session
        file; these cases never touch disk."""
        return None


def _loop(engine: ChatEngine, *tools: BaseTool, **overrides: Any) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(
            stop_policy="natural", max_tool_iterations=4, turn_timeout_seconds=0,
            **overrides,
        ),
    )


def _note(note_id: str, text: str, evidence: str, **over: Any) -> MemoryNote:
    return MemoryNote(id=note_id, text=text, evidence=evidence, **over)


# ------------------------------------------------------------- offline ----


async def evidence_breaks_a_tie(_tmp: Path) -> tuple[bool, str]:
    notes = [_note("a", "构建用 uv", "model"), _note("b", "构建用 uv", "tool")]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams())
    order = [item.note.id for item in ranked]
    return order == ["b", "a"], f"order={order}"


async def evidence_filter(tmp: Path) -> tuple[bool, str]:
    store = MemoryStore(tmp / "m.json")
    store.add_note(_note("t1", "构建用 uv", "tool"))
    # The wording of `m1` is load-bearing: 「构建用什么」 tokenises to
    # ['构建','建用','用什'] and R10's 0.25 relevance floor needs `m1` to share
    # 建用, not just 构建. 「构建改用 uv」 covers 1 of the 3 known terms
    # (relevance 0.221) and this case fails on the floor, not on evidence.
    store.add_note(_note("m1", "构建用 uv，改用别家", "model"))
    store.add_note(_note("l1", "构建用 uv，老数据", ""))
    tool = MemorySearchTool(MemoryService(store))
    tool_hits = await tool.execute({"query": "构建用什么", "evidence": "tool"})
    model_hits = await tool.execute({"query": "构建用什么", "evidence": "model"})
    bad = await tool.execute({"query": "构建用什么", "evidence": "maybe"})
    ids = [n["id"] for n in tool_hits.data["notes"]]
    other = sorted(n["id"] for n in model_hits.data["notes"])
    ok = ids == ["t1"] and other == ["l1", "m1"] and bad.error == "bad_evidence"
    return ok, f"tool={ids} model={other} bad={bad.error}"


async def digest_carries_the_note(_tmp: Path) -> tuple[bool, str]:
    from ai_coding.domain.loop import TurnOutcome

    digest = build_digest(
        turn=3,
        user_input="修 bash 超时",
        outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="做完了"),
        model_note="下一步：先补 runner 的用例",
    )
    line = digest.render()
    ok = (
        line == "- t3 修 bash 超时 → 下一步：先补 runner 的用例"
        and digest.source == "model"
        and digest.outcome == "做完了"
    )
    return ok, line


async def checkpoint_reaches_the_digest(_tmp: Path) -> tuple[bool, str]:
    engine = _Engine("checkpoint_write", json.dumps({"note": "记住这一行"}))
    loop = _loop(engine, _CheckpointTool())
    session = loop.sessions.create()
    await loop.process_input(session, "先记一行")
    digest = session.turn_digests[-1]
    return digest.note == "记住这一行", f"note={digest.note!r} source={digest.source}"


async def checkpoint_is_not_evidence(_tmp: Path) -> tuple[bool, str]:
    books = TurnBooks()
    from ai_coding.core.dispatch import DispatchOutcome
    from ai_coding.domain.message import ToolCall

    books.note_outcomes(
        [
            DispatchOutcome(
                call=ToolCall(id="c1", name="checkpoint_write"),
                result=ToolResult(success=True, output="ok"),
            )
        ]
    )
    ok = books.evidence_calls == 0 and books.successful_calls == 1
    return ok, f"evidence_calls={books.evidence_calls} successful={books.successful_calls}"


async def the_call_is_written_back(_tmp: Path) -> tuple[bool, str]:
    engine = _Engine("checkpoint_write", json.dumps({"note": "记住这一行"}))
    loop = _loop(engine, _CheckpointTool())
    session = loop.sessions.create()
    await loop.process_input(session, "先记一行")
    tool_messages = [m.content or "" for m in session.messages if m.role == "tool"]
    found = any("checkpoint:" in body for body in tool_messages)
    return found, f"{len(tool_messages)} tool messages"


async def chunks_arrive_early(_tmp: Path) -> tuple[bool, str]:
    stamps: list[float] = []
    started = time.monotonic()
    run_process(
        "echo one; sleep 0.4; echo two",
        shell=True,
        timeout=10,
        on_chunk=lambda _stream, _text: stamps.append(time.monotonic() - started),
    )
    ok = bool(stamps) and stamps[0] < 0.4
    return ok, f"first chunk at {stamps[0]:.2f}s" if stamps else "no chunks"


async def stdin_still_works(_tmp: Path) -> tuple[bool, str]:
    outcome = run_process("cat", shell=True, timeout=5, stdin_data="from-stdin\n")
    return outcome.stdout == "from-stdin\n", repr(outcome.stdout)


async def output_after_exit_is_not_lost(_tmp: Path) -> tuple[bool, str]:
    outcome = run_process(
        "echo first; (sleep 0.4; echo late) & exit 0", shell=True, timeout=5
    )
    ok = "first" in outcome.stdout and "late" in outcome.stdout
    return ok, repr(outcome.stdout)


async def cancel_kills_the_group(tmp: Path) -> tuple[bool, str]:
    pid_file = tmp / "pid"
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    outcome = run_process(
        f"echo $$ > {pid_file}; sleep 30", shell=True, timeout=30, cancel=cancel
    )
    if not outcome.cancelled or outcome.timed_out:
        return False, f"cancelled={outcome.cancelled} timed_out={outcome.timed_out}"
    pgid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True, "cancelled and the group is gone"
        time.sleep(0.05)
    return False, "the process group survived the cancel"


async def cancel_is_not_a_failure(_tmp: Path) -> tuple[bool, str]:
    from ai_coding.core.dispatch import DispatchOutcome
    from ai_coding.domain.message import ToolCall

    books = TurnBooks()
    books.note_outcomes(
        [
            DispatchOutcome(
                call=ToolCall(id="c1", name="bash"),
                result=ToolResult(success=False, output="cancelled", error=CANCELLED_BY_USER),
            )
        ]
    )
    ok = books.failed_streak == 0 and books.failures == []
    return ok, f"streak={books.failed_streak} failures={len(books.failures)}"


async def chunks_never_enter_the_request(_tmp: Path) -> tuple[bool, str]:
    engine = _Engine("chunky")
    loop = _loop(engine, _ChunkyTool())
    session = loop.sessions.create()
    await loop.process_input(session, "跑点东西")
    request_text = "\n".join(m.content or "" for m in engine.requests[-1])
    in_history = any("secret-chunk" in (m.content or "") for m in session.messages)
    got_result = any("final-output" in (m.content or "") for m in session.messages)
    return (
        "secret-chunk" not in request_text and not in_history and got_result,
        f"leaked={in_history} final_in_history={got_result}",
    )


async def dispatcher_cancel_is_structured(tmp: Path) -> tuple[bool, str]:
    class _Hanging(BaseTool):
        parallel_safe = True

        @property
        def name(self) -> str:
            return "read"

        @property
        def description(self) -> str:
            return "hangs"

        @property
        def params_json_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}}

        async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
            await asyncio.sleep(30)
            return ToolResult(success=True, output="never")

    cancel = asyncio.Event()
    registry = ToolRegistry()
    registry.register(_Hanging())
    dispatcher = ToolDispatcher(registry, PermissionGate(), cancel=cancel)

    async def trip() -> None:
        await asyncio.sleep(0.1)
        cancel.set()

    batches = await asyncio.gather(
        dispatcher.dispatch_batch(
            [ToolCallRef(id="c1", name="read", arguments="{}")], ToolContext()
        ),
        trip(),
    )
    result = batches[0][0].result
    ok = result.error == CANCELLED_BY_USER and result.success is False
    return ok, f"error={result.error}"


OFFLINE: dict[str, CaseFn] = {
    "evidence_breaks_a_tie": evidence_breaks_a_tie,
    "evidence_filter": evidence_filter,
    "digest_carries_the_note": digest_carries_the_note,
    "checkpoint_reaches_the_digest": checkpoint_reaches_the_digest,
    "checkpoint_is_not_evidence": checkpoint_is_not_evidence,
    "the_call_is_written_back": the_call_is_written_back,
    "chunks_arrive_early": chunks_arrive_early,
    "stdin_still_works": stdin_still_works,
    "output_after_exit_is_not_lost": output_after_exit_is_not_lost,
    "cancel_kills_the_group": cancel_kills_the_group,
    "cancel_is_not_a_failure": cancel_is_not_a_failure,
    "chunks_never_enter_the_request": chunks_never_enter_the_request,
    "dispatcher_cancel_is_structured": dispatcher_cancel_is_structured,
}


# ---------------------------------------------------------------- live ----


async def _real_loop(
    config_path: str, workspace: Path, *, observer: Any | None = None
) -> tuple[Any, Any, Path]:
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.config.loader import ConfigManager
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.builder import build_tool_registry

    manager = ConfigManager()
    manager.initialize(config_path)
    app_cfg = manager.app
    registry = build_tool_registry(app_cfg, str(workspace))
    sessions = SessionService(workspace / "sessions")
    engine = build_engine_from_config(app_cfg)
    memory = MemoryService(MemoryStore(workspace / ".memory"))
    # The REPL registers these three next to each other (cli.repl); an acceptance
    # run wants the same surface the user actually gets.
    from ai_coding.tools.checkpoint_tool import CheckpointTool
    from ai_coding.tools.memory_write_tool import MemoryWriteTool

    for tool in (MemorySearchTool(memory), MemoryWriteTool(memory), CheckpointTool()):
        if registry.get(tool.name) is None:
            registry.register(tool)
    loop = AgentLoop(
        engine,
        sessions,
        registry,
        memory=memory,
        approval=AutoApproveCallback(),
        observer=observer,
        settings=app_cfg.ai,
        workspace=str(workspace),
    )
    return loop, registry, workspace


async def real_turn_still_works(tmp: Path) -> tuple[bool, str]:
    config_path = os.environ.get("R8_CONFIG", "config.local.yaml")
    loop, registry, _ = await _real_loop(config_path, tmp)
    try:
        answer = await loop.process_input(
            loop.sessions.create(), "用一句话说明这个仓库是做什么的。"
        )
    finally:
        await loop.aclose()
    names = sorted(registry.list_names())
    ok = bool(answer) and loop.last_outcome is not None
    return ok, (
        f"{len(answer)} chars, status="
        f"{loop.last_outcome.status.value if loop.last_outcome else '?'}, "
        f"checkpoint_write={'yes' if 'checkpoint_write' in names else 'no'}"
    )


async def real_streaming_bash(tmp: Path) -> tuple[bool, str]:
    config_path = os.environ.get("R8_CONFIG", "config.local.yaml")
    from ai_coding.core.events import RecordingObserver

    observer = RecordingObserver()
    loop, _registry, _ = await _real_loop(config_path, tmp, observer=observer)
    try:
        answer = await loop.process_input(
            loop.sessions.create(),
            "用 bash 跑 `for i in 1 2 3; do echo line-$i; sleep 0.2; done`，然后告诉我看到了什么。",
        )
    finally:
        await loop.aclose()
    seen = len(observer.of_type(ToolChunk))
    return bool(answer), f"chunks={seen}, answer={len(answer)} chars"


async def real_checkpoint_round_trip(tmp: Path) -> tuple[bool, str]:
    """Does the model use checkpoint_write when the turn is ending mid-work?"""
    config_path = os.environ.get("R8_CONFIG", "config.local.yaml")
    loop, _registry, _ = await _real_loop(config_path, tmp)
    session = loop.sessions.create()
    try:
        answer = await loop.process_input(
            session,
            "先看看 README 的前 5 行，然后用 checkpoint_write 记一行你下一步要做什么。",
        )
    finally:
        await loop.aclose()
    notes = [d.note for d in session.turn_digests if d.note]
    return bool(answer), f"digest notes={notes!r}"


LIVE: dict[str, CaseFn] = {
    "real_turn_still_works": real_turn_still_works,
    "real_streaming_bash": real_streaming_bash,
    "real_checkpoint_round_trip": real_checkpoint_round_trip,
}


# ----------------------------------------------------------------- main ----


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    parser.add_argument("--real", action="store_true", help="also run the live cases")
    parser.add_argument("--config", default="config.local.yaml")
    args = parser.parse_args(argv)

    os.environ["R8_CONFIG"] = args.config
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
