#!/usr/bin/env python3
"""R7 acceptance cases — offline by default, ``--real`` adds the live ones.

Each case is one of the round's measured claims (docs/r7-analysis.md §1), run
against the real units with fakes only where IO would be needed.  The live cases
re-run the same claims where they depend on the machine: a real MCP stdio
server, and one real model turn.

    .venv/bin/python scripts/r7_cases.py
    .venv/bin/python scripts/r7_cases.py --only l2_envelope_is_recoverable
    .venv/bin/python scripts/r7_cases.py --real --config config.local.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.config.models import (  # noqa: E402
    AIConfig,
    AppConfig,
    MCPConfig,
    MCPServerConfig,
    MCPTimeout,
)
from ai_coding.core.context_compression import (  # noqa: E402
    ContextManager,
    _joined_history_text,
    estimate_tokens,
    placeholder_old_results,
)
from ai_coding.core.dispatch import ToolDispatcher  # noqa: E402
from ai_coding.core.mcp import MCPManager, build_mcp_catalog  # noqa: E402
from ai_coding.core.trace import short_json  # noqa: E402
from ai_coding.core.turn_books import TurnBooks  # noqa: E402
from ai_coding.core.turn_close import TurnCloser  # noqa: E402
from ai_coding.core.turn_policy import TurnAction, TurnPolicy  # noqa: E402
from ai_coding.domain.loop import TurnOutcome, TurnStatus  # noqa: E402
from ai_coding.domain.message import (  # noqa: E402
    ChatMessage,
    ToolCallRef,
    ToolResult,
)
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.domain.task import TaskState  # noqa: E402
from ai_coding.infra.deadline import Deadline  # noqa: E402
from ai_coding.security.approval import (  # noqa: E402
    ApprovalCallback,
    ApprovalResult,
    CLIApprovalCallback,
)
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402
from ai_coding.tools.mcp_call import MCPCallTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402
from ai_coding.tools.schema import tool_schemas  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


# ---------------------------------------------------------------- helpers ----


def _session() -> SessionData:
    return SessionData(session_id="r7", title="", created_time="t", last_access_time="t")


def _tool_message(body: str, call_id: str, tool: str = "bash") -> ChatMessage:
    return ChatMessage(
        role="tool", content=body, timestamp="t", tool_call_id=call_id, tool_name=tool
    )


def _long_body(marker: str) -> str:
    return f"{marker}-HEAD " + "x" * 1200 + " TAIL-marker"


def _long_history(turns: int = 70, body: int = 3000) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for number in range(1, turns + 1):
        messages.append(ChatMessage(role="user", content=f"第 {number} 步", timestamp="t"))
        messages.append(
            ChatMessage(
                role="assistant",
                content=None,
                timestamp="t",
                tool_calls=[
                    ToolCallRef(id=f"c{number}", name="bash", arguments="{}")
                ],
            )
        )
        messages.append(_tool_message(f"RESULT-{number} " + "x" * body, f"c{number}"))
        messages.append(
            ChatMessage(role="assistant", content=f"第 {number} 步完成。", timestamp="t")
        )
    return messages


class _Bash(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        return "bash"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="ran")


class _SlowApproval(ApprovalCallback):
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def approve(self, tool_name, params, reason):
        await asyncio.sleep(self.seconds)
        return ApprovalResult.YES


# ---------------------------------------------------------------- offline ----


async def case_books_feed_the_policy(_root: Path) -> tuple[bool, str]:
    """The counters decide the ladder, and the snapshot is built where they live."""
    books = TurnBooks(iterations=3, attempted_calls=4, successful_calls=0)
    policy = TurnPolicy()
    decision = policy.decide_next_iteration(
        books.snapshot(iteration=4, tokens_spent=0)
    )
    ok = decision.action is TurnAction.FINISH and "no progress" in decision.reason
    return ok, f"action={decision.action.name} reason={decision.reason!r}"


async def case_close_reports_when_the_model_is_silent(_root: Path) -> tuple[bool, str]:
    """No text → a handoff; text → untouched.  The digest is always appended."""
    reporter = type(
        "R",
        (),
        {
            "finish_turn": lambda self, outcome, observer: outcome,
            "report_degraded": lambda *args: None,
        },
    )()
    sessions = type("S", (), {"save": lambda self, session: None})()
    settings = type("C", (), {"persist_checkpoint_messages": 100, "working_memory_keep": 12})()
    closer = TurnCloser(sessions, reporter, settings)
    session = _session()
    silent = closer.close(
        session,
        user_input="修好它",
        task=TaskState(goal="修好它"),
        turn_number=1,
        outcome=TurnOutcome(status=TurnStatus.TIMEOUT, text=None, iterations=2, reason="时钟"),
        failures=[("bash", "tool_timeout", "")],
    )
    spoken = closer.close(
        session,
        user_input="做完了吗",
        task=TaskState(goal="修好它"),
        turn_number=2,
        outcome=TurnOutcome(
            status=TurnStatus.COMPLETED, text="做完了", iterations=1, reason="model finished"
        ),
    )
    ok = (
        silent.text is None
        and silent.handoff.startswith("[回合未完成")
        and spoken.text == "做完了"
        and spoken.handoff == ""
        and [d.turn for d in session.turn_digests] == [1, 2]
    )
    return ok, f"handoff={len(silent.handoff)} chars, digests={len(session.turn_digests)}"


async def case_l2_envelope_is_recoverable(root: Path) -> tuple[bool, str]:
    """What L2 elides is written to disk, and the envelope says where."""
    body = _long_body("t1")
    out = placeholder_old_results(
        [_tool_message(body, "c1"), _tool_message("fresh", "c2")],
        keep_recent=1,
        window_chars=100,
        elide_root=root,
    )
    text = out[0].content or ""
    path = Path(text.split("full output: ", 1)[1].split("]")[0].strip())
    ok = path.is_file() and path.read_text(encoding="utf-8") == body and "…" in text
    return ok, f"envelope={len(text)} chars, artifact={path.name} ({path.stat().st_size}B)"


async def case_l2_budget_is_spent_newest_first(root: Path) -> tuple[bool, str]:
    messages = [_tool_message(_long_body(f"t{i}"), f"c{i}") for i in range(4)]
    messages.append(_tool_message("fresh", "cf"))
    out = placeholder_old_results(
        messages, keep_recent=1, window_chars=100, window_budget=100, elide_root=root
    )
    envelopes = [m.content or "" for m in out if (m.content or "").startswith("[Previous")]
    windowed = [text for text in envelopes if "…" in text]
    ok = (
        len(envelopes) == 4
        and len(windowed) == 1
        and windowed[0].splitlines()[1].startswith("t3-HEAD")
        and all("full output: " in text for text in envelopes)
    )
    return ok, f"envelopes={len(envelopes)} windowed={len(windowed)} (newest first)"


async def case_l2_leaves_small_bodies_alone(root: Path) -> tuple[bool, str]:
    out = placeholder_old_results(
        [_tool_message("short", "c1"), _tool_message("fresh", "c2")],
        keep_recent=1,
        elide_root=root,
    )
    ok = out[0].content == "short"
    return ok, f"body kept verbatim: {out[0].content!r}"


async def case_l2_long_history_stays_under_budget(root: Path) -> tuple[bool, str]:
    cm = ContextManager(AIConfig(), root)
    out = await cm.compress_if_needed(_long_history())
    tokens = estimate_tokens(_joined_history_text(out))
    envelopes = [m for m in out if (m.content or "").startswith("[Previous")]
    ok = tokens <= AIConfig().max_context_tokens * 0.5 and bool(envelopes)
    return ok, f"est tokens={tokens} envelopes={len(envelopes)} of {len(out)} messages"


async def case_context_manager_persists_the_elided_bodies(root: Path) -> tuple[bool, str]:
    cm = ContextManager(AIConfig(max_messages=50), root)
    out = await cm.compress_if_needed(_long_history())
    files = list((root / "transcripts" / "elided").glob("*.txt"))
    windowed = [m for m in out if "…" in (m.content or "")]
    ok = bool(files) and bool(windowed)
    head = windowed[0].content.splitlines()[1][:40] if windowed else ""
    return ok, f"artifacts={len(files)} windowed={len(windowed)} head={head!r}"


async def case_approval_timeout_is_bounded_and_distinct(root: Path) -> tuple[bool, str]:
    registry = ToolRegistry()
    registry.register(_Bash())
    dispatcher = ToolDispatcher(
        registry, PermissionGate(), _SlowApproval(5.0), max_parallel=1
    )
    started = time.monotonic()
    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="."),
        deadline=Deadline.after(1.0),
    )
    elapsed = time.monotonic() - started
    result = outcomes[0].result
    ok = (
        elapsed < 2.0
        and result.error == "approval_timeout"
        and outcomes[0].halted is False
    )
    return ok, f"elapsed={elapsed:.2f}s error={result.error} halted={outcomes[0].halted}"


async def case_a_sync_approval_no_longer_freezes_the_loop(root: Path) -> tuple[bool, str]:
    registry = ToolRegistry()
    registry.register(_Bash())
    dispatcher = ToolDispatcher(
        registry,
        PermissionGate(),
        CLIApprovalCallback(input_fn=lambda _prompt: (time.sleep(1.5), "y")[1]),
        max_parallel=1,
    )
    started = time.monotonic()
    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="bash", arguments='{"command":"ls"}')],
        ToolContext(workspace="."),
        deadline=Deadline.after(0.5),
    )
    elapsed = time.monotonic() - started
    ok = elapsed < 1.0 and outcomes[0].result.error == "approval_timeout"
    return ok, f"elapsed={elapsed:.2f}s (clock 0.5s, human 1.5s)"


async def case_mcp_trust_is_server_scoped(root: Path) -> tuple[bool, str]:
    cfg = AppConfig(
        mcp=MCPConfig(
            enabled=True,
            servers=[
                MCPServerConfig(
                    name="everything", command=["npx"], auto_approve=True
                )
            ],
        )
    )
    gate = PermissionGate.from_config(cfg)
    untrusted = PermissionGate()
    ok = (
        gate.check("everything__read_file", {}) == PermissionDecision.ALLOW
        and gate.check("other__read_file", {}) == PermissionDecision.WARN
        and gate.check("bash", {"command": "rm -rf /"}) == PermissionDecision.DENY
        and untrusted.check("everything__read_file", {}) == PermissionDecision.WARN
    )
    return ok, "trusted server allowed, others warn, hard-deny intact, default unchanged"


async def case_mcp_allowlist_filters_adapters(root: Path) -> tuple[bool, str]:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    class _Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name=name, description=name, input_schema={})
                    for name in ("read_file", "write_file", "list_dir")
                ]
            )

    session = _Session()

    def factory(_server):
        @asynccontextmanager
        async def _cm():
            yield session

        return _cm()

    manager = MCPManager(
        MCPConfig(
            enabled=True,
            servers=[
                MCPServerConfig(
                    name="fs",
                    command=["x"],
                    tools=["read_file", "list_dir"],
                )
            ],
        ),
        session_factory=factory,
    )
    try:
        tools = await manager.open_all()
    finally:
        await manager.aclose()
    ok = [tool.name for tool in tools] == ["fs__read_file", "fs__list_dir"]
    return ok, f"registered {[tool.name for tool in tools]}"


OFFLINE: dict[str, CaseFn] = {
    "books_feed_the_policy": case_books_feed_the_policy,
    "close_reports_when_the_model_is_silent": case_close_reports_when_the_model_is_silent,
    "l2_envelope_is_recoverable": case_l2_envelope_is_recoverable,
    "l2_budget_is_spent_newest_first": case_l2_budget_is_spent_newest_first,
    "l2_leaves_small_bodies_alone": case_l2_leaves_small_bodies_alone,
    "l2_long_history_stays_under_budget": case_l2_long_history_stays_under_budget,
    "context_manager_persists_the_elided_bodies": case_context_manager_persists_the_elided_bodies,
    "approval_timeout_is_bounded_and_distinct": case_approval_timeout_is_bounded_and_distinct,
    "a_sync_approval_no_longer_freezes_the_loop": case_a_sync_approval_no_longer_freezes_the_loop,
    "mcp_trust_is_server_scoped": case_mcp_trust_is_server_scoped,
    "mcp_allowlist_filters_adapters": case_mcp_allowlist_filters_adapters,
}


# ------------------------------------------------------------------- live ----


async def real_mcp_tool_surface(root: Path) -> tuple[bool, str]:
    """The reproducible tool-surface measurement, on this machine."""
    from ai_coding.cli import _build_tool_registry
    from ai_coding.config.loader import ConfigManager

    manager = ConfigManager()
    manager.initialize(str(ROOT / "config.local.yaml"))
    app_cfg = manager.app
    base_registry = _build_tool_registry(app_cfg, str(ROOT), None)
    base = len(short_json(tool_schemas(base_registry)))

    async def with_tools(allowlist: list[str]) -> tuple[int, int]:
        """M2's cost model: one dispatcher schema + the per-step catalog text."""
        cfg = MCPConfig(
            enabled=True,
            servers=[
                MCPServerConfig(
                    name="many",
                    command=[sys.executable, str(ROOT / "scripts" / "mcp_many_tools_server.py")],
                    timeout=MCPTimeout(startup=30_000),
                    tools=allowlist,
                )
            ],
        )
        mcp = MCPManager(cfg)
        try:
            await mcp.open_all()
            registry = _build_tool_registry(app_cfg, str(ROOT), None)
            registry.register(MCPCallTool(mcp))
            schemas = len(short_json(tool_schemas(registry)))
            catalog = len(build_mcp_catalog(mcp.available()))
            return schemas + catalog, len(mcp.available()[0][1]) if mcp.available() else 0
        finally:
            await mcp.aclose()

    all_cost, count = await with_tools([])
    few_cost, few_count = await with_tools(["tool_3", "tool_7"])
    ok = few_cost < all_cost and count == 13 and few_count == 2
    return ok, (
        f"no MCP {base} → {count} tools {all_cost} → {few_count} tools {few_cost} "
        f"({few_cost / all_cost:.0%} of the full server), catalog+schema chars per step"
    )


async def real_turn_still_works(root: Path) -> tuple[bool, str]:
    """One real model turn through the whole composition root."""
    from ai_coding.cli import _build_loop, _build_tool_registry, _make_recorder
    from ai_coding.config.loader import ConfigManager
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService

    config_path = root / "config.local.yaml"
    if not config_path.is_file():
        return True, "no config.local.yaml — skipped (report only)"
    manager = ConfigManager()
    manager.initialize(str(config_path))
    app_cfg = manager.app
    app_cfg.ai.max_tool_iterations = 3
    from ai_coding.ai.factory import build_engine_from_config

    engine = build_engine_from_config(app_cfg)
    recorder = _make_recorder(app_cfg, str(root), None)
    registry = _build_tool_registry(
        app_cfg, str(root), None, user_prompt=CLIUserPromptCallback()
    )
    sessions = SessionService(root / "sessions")
    loop = _build_loop(
        engine,
        sessions,
        registry,
        app_cfg,
        str(root),
        None,
        observer=recorder,
        trace=recorder,
    )
    try:
        answer = await loop.process_input(_session(), "用一句话说明这个仓库是做什么的。")
    finally:
        await loop.aclose()
    outcome = loop.last_outcome
    tools_chars = getattr(loop.reporter, "tools_chars", 0)
    ok = bool(answer) and outcome is not None and tools_chars > 0
    return ok, (
        f"{len(answer)} chars, status={outcome.status.value if outcome else '?'}, "
        f"tools_chars={tools_chars}"
    )


LIVE: dict[str, CaseFn] = {
    "mcp_tool_surface": real_mcp_tool_surface,
    "turn_still_works": real_turn_still_works,
}


# ------------------------------------------------------------------- main ----


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
    for name, case in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if name in LIVE:
                root = Path(args.config).resolve().parent
            try:
                ok, detail = await case(root)
            except Exception as exc:  # noqa: BLE001 — a case must not kill the script
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name}: {detail}")
        passed += ok
        failed += not ok

    print(SEP)
    print(f"SUMMARY {passed}/{passed + failed} passed (real cases report, they are not graded)")
    if not args.real:
        return 1 if failed else 0
    offline = [name for name in cases if name in OFFLINE]
    return 1 if failed and offline else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
