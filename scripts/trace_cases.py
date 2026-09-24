#!/usr/bin/env python3
"""Observability / guard / hook acceptance cases (R4) — offline by default.

Each case is one of the claims R4 makes, checked against a real (scripted) turn
with a real workspace on disk, so the assertions are about bytes that were
actually written rather than about intentions.

    .venv/bin/python scripts/trace_cases.py                 # offline cases
    .venv/bin/python scripts/trace_cases.py --only c1,c6
    .venv/bin/python scripts/trace_cases.py --real --config config.local.yaml

The ``--real`` cases call a live model; they are the ones that can discover
provider-specific behaviour (which is how we learned DashScope does report
prompt-cache numbers, and that the tool schemas are ~5k chars of every prompt).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.ai.prompt import PromptAssembler  # noqa: E402
from ai_coding.config.models import AIConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.core.guard import DuplicateCallGuard, GuardChain  # noqa: E402
from ai_coding.core.hooks import HookDecision, HookPoint, HookResult  # noqa: E402
from ai_coding.core.trace import Redactor, TraceRecorder, TraceStore  # noqa: E402
from ai_coding.domain.loop import StepResult, TurnStatus  # noqa: E402
from ai_coding.domain.message import ToolCallRef, ToolResult  # noqa: E402
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402
from ai_coding.tools.base import BaseTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402

SEP = "-" * 64
SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"

CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


class _Engine(ChatEngine):
    """Returns pre-scripted steps; records what it was asked."""

    def __init__(self, steps: Sequence[StepResult]) -> None:
        self._steps = list(steps)
        self.requests: list[list[Any]] = []
        self.systems: list[str | None] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        self.requests.append(list(messages))
        self.systems.append(instructions)
        if not self._steps:
            raise AssertionError("the scripted engine ran out of steps")
        return self._steps.pop(0)

    async def aclose(self) -> None:
        return None


class _EchoTool(BaseTool):
    """A stand-in for bash: returns what it was asked to run."""

    parallel_safe = True

    def __init__(self, name: str = "bash") -> None:
        self._name = name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "echo back the command"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.calls.append(dict(params))
        return ToolResult(success=True, output=str(params.get("command", "")))


def _session(session_id: str = "case-session") -> SessionData:
    return SessionData(
        session_id=session_id,
        title="",
        created_time="2026-09-24T00:00:00Z",
        last_access_time="",
    )


def _calls(*refs: ToolCallRef) -> StepResult:
    return StepResult(text=None, tool_calls=list(refs), finish_reason="tool_calls")


def _ref(call_id: str, command: str, name: str = "bash") -> ToolCallRef:
    return ToolCallRef(id=call_id, name=name, arguments=json.dumps({"command": command}))


def _path_ref(call_id: str, path: str, name: str = "read") -> ToolCallRef:
    """An ALLOW-tier call, so the guard is what decides (not the approval step)."""
    return ToolCallRef(id=call_id, name=name, arguments=json.dumps({"path": path}))


def _loop(
    root: Path,
    engine: ChatEngine,
    *,
    tools: Sequence[BaseTool] = (),
    settings: AIConfig | None = None,
    recorder: TraceRecorder | None = None,
    guards: GuardChain | None = None,
    hooks: Any | None = None,
    **kwargs: Any,
) -> AgentLoop:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    store = TraceStore(root / ".ai-coding" / "traces")
    recorder = recorder if recorder is not None else TraceRecorder(store, session_id="case-session")
    from ai_coding.core.observers import Braid

    return AgentLoop(
        engine,
        SessionService(root / "sessions"),
        registry,
        prompt=PromptAssembler("You are a test assistant."),
        settings=settings or AIConfig(stop_policy="natural", max_tool_iterations=4),
        observer=Braid(recorder),
        trace=recorder,
        guards=guards,
        hooks=hooks,
        workspace=str(root),
        **kwargs,
    )


def _turn_lines(root: Path) -> list[dict[str, Any]]:
    """Every event recorded so far, in file order."""
    rows: list[dict[str, Any]] = []
    for path in sorted((root / ".ai-coding" / "traces").rglob("turn-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


# ---- offline cases ---------------------------------------------------------


async def c1_tool_spans(root: Path) -> tuple[bool, str]:
    """Three tool calls in one turn produce three spans with real durations."""
    tool = _EchoTool()
    engine = _Engine(
        [
            _calls(_ref("c1", "ls"), _ref("c2", "pwd")),
            _calls(_ref("c3", "whoami")),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    loop = _loop(root, engine, tools=[tool])

    await loop.process_input(_session(), "run three commands")

    rows = _turn_lines(root)
    started = [r for r in rows if r["evt"] == "ToolStarted"]
    finished = [r for r in rows if r["evt"] == "ToolFinished"]
    spans = {r["span"] for r in started}
    details = f"tools={[r['data']['tool_name'] for r in started]} spans={len(spans)}"
    ok = (
        len(started) == 3
        and len(finished) == 3
        and spans == {"c1", "c2", "c3"}
        and all("duration_ms" in r["data"] for r in finished)
        and all(r["parent"].startswith("i") for r in finished)
    )
    return ok, details


async def c5_secret_never_reaches_disk(root: Path) -> tuple[bool, str]:
    """A secret in the arguments and in the output must not survive on disk."""
    tool = _EchoTool()
    engine = _Engine(
        [
            _calls(_ref("c1", f"export KEY={SECRET}")),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    loop = _loop(root, engine, tools=[tool])

    await loop.process_input(_session(), "leak a key")

    hits = 0
    for path in (root / ".ai-coding").rglob("*"):
        if path.is_file() and SECRET in path.read_text(encoding="utf-8", errors="replace"):
            hits += 1
    # the control: the same secret in a file that is NOT redacted
    (root / "control.txt").write_text(SECRET, encoding="utf-8")
    control = SECRET in (root / "control.txt").read_text(encoding="utf-8")
    redacted = Redactor().redact(SECRET) != SECRET
    return (hits == 0 and control and redacted), f"trace files containing the key: {hits}"


async def c6_duplicate_guard(root: Path) -> tuple[bool, str]:
    """The third identical call is refused, and the turn still ends cleanly."""
    # ``read`` is ALLOW tier: without this the WARN/approval step would refuse the
    # calls first and the guard would never get to decide anything.
    tool = _EchoTool("read")
    engine = _Engine(
        [
            _calls(_path_ref("c1", "notes.txt")),
            _calls(_path_ref("c2", "notes.txt")),
            _calls(_path_ref("c3", "notes.txt")),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    loop = _loop(root, engine, tools=[tool], guards=GuardChain([DuplicateCallGuard(2)]))

    await loop.process_input(_session(), "keep listing")

    rows = _turn_lines(root)
    denied = [
        r for r in rows if r["evt"] == "ToolFinished" and r["data"].get("error") == "guard_denied"
    ]
    status = loop.last_outcome.status if loop.last_outcome else None
    detail = f"executed={len(tool.calls)} denied={len(denied)} status={status}"
    ok = (
        len(tool.calls) == 2
        and len(denied) == 1
        and "c1" in denied[0]["data"]["output_preview"]
        and status is not TurnStatus.ERROR
    )
    return ok, detail


async def c8_token_gate(root: Path) -> tuple[bool, str]:
    """A tiny token ceiling ends the turn with TurnStatus.BUDGET."""
    tool = _EchoTool()
    usage = {"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550}
    engine = _Engine(
        [
            StepResult(text=None, tool_calls=[_ref("c1", "ls")], usage=usage),
            StepResult(text="never reached", finish_reason="stop"),
        ]
    )
    loop = _loop(
        root,
        engine,
        tools=[tool],
        settings=AIConfig(stop_policy="natural", max_tool_iterations=4, turn_token_budget=100),
    )

    await loop.process_input(_session(), "keep going")

    status = loop.last_outcome.status if loop.last_outcome else None
    reason = loop.last_outcome.reason if loop.last_outcome else ""
    return (
        status is TurnStatus.BUDGET and reason == "turn token budget exhausted",
        f"status={status} reason={reason!r}",
    )


async def c9_degraded_is_visible(root: Path) -> tuple[bool, str]:
    """A failing enhancement is recorded and the turn still completes."""

    class _BrokenMemory:
        def index_notes(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("index unreadable")

        def render_index_block(self, notes):  # type: ignore[no-untyped-def]
            return ""

        def status(self):  # type: ignore[no-untyped-def]
            return {}

    engine = _Engine([StepResult(text="done", finish_reason="stop")])
    loop = _loop(root, engine, memory=_BrokenMemory())

    await loop.process_input(_session(), "hello")

    rows = _turn_lines(root)
    degraded = [r["data"]["component"] for r in rows if r["evt"] == "Degraded"]
    status = loop.last_outcome.status if loop.last_outcome else None
    return (
        "memory_block" in degraded and status is TurnStatus.COMPLETED,
        f"degraded={degraded} status={status}",
    )


class _BlockingHooks:
    """A hooks stand-in that refuses one tool by name."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name
        self.points: list[HookPoint] = []

    async def run(self, point: HookPoint, ctx: Any) -> list[HookResult]:
        self.points.append(point)
        if point is HookPoint.TOOL_BEFORE:
            tool = (ctx.get("tool") or {}).get("name")
            if tool == self._tool_name:
                return [
                    HookResult(
                        hook="case-hook",
                        decision=HookDecision.BLOCK,
                        reason="blocked by the acceptance case",
                    )
                ]
        return []


async def c7_hook_blocks_a_call(root: Path) -> tuple[bool, str]:
    """A blocked call never runs, is reported structurally, and the turn goes on."""
    tool = _EchoTool()
    engine = _Engine(
        [
            _calls(_ref("c1", "curl example.com")),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    hooks = _BlockingHooks("bash")
    loop = _loop(root, engine, tools=[tool], hooks=hooks)
    session = _session()

    await loop.process_input(session, "fetch a page")

    rows = _turn_lines(root)
    blocked = [
        r for r in rows if r["evt"] == "ToolFinished" and r["data"].get("error") == "hook_blocked"
    ]
    tool_messages = [m for m in session.messages if m.role == "tool"]
    ok = (
        len(blocked) == 1
        and tool.calls == []
        and len(tool_messages) == 1
        and HookPoint.TURN_END in hooks.points
    )
    return ok, f"blocked={len(blocked)} executed={len(tool.calls)} pairs={len(tool_messages)}"


# ---- real-LLM cases --------------------------------------------------------


async def real_cases(config_path: str) -> int:
    """Live cases: what the provider actually reports, and the cache prefix."""
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.tools.builder import build_tool_registry

    app_cfg = ConfigLoader.load_app(config_path)
    app_cfg.ai.max_tool_iterations = 3
    app_cfg.ai.turn_timeout_seconds = 120
    failures = 0
    with tempfile.TemporaryDirectory(prefix="trace_real_") as tmp:
        root = Path(tmp)
        (root / "sample.txt").write_text("hello from a file\n", encoding="utf-8")
        registry = build_tool_registry(app_cfg, str(root), None)
        recorder = TraceRecorder(
            TraceStore(root / ".ai-coding" / "traces"), session_id="real"
        )
        from ai_coding.core.observers import Braid

        loop = AgentLoop(
            build_engine_from_config(app_cfg),
            SessionService(root / "sessions"),
            registry,
            prompt=PromptAssembler("You are a test assistant. Be brief."),
            settings=app_cfg.ai,
            observer=Braid(recorder),
            trace=recorder,
            workspace=str(root),
        )
        try:
            await loop.process_input(_session("real"), "读取 sample.txt，一句话回答。")
        finally:
            await loop.aclose()

        rows = _turn_lines(root)
        assembled = [r["data"] for r in rows if r["evt"] == "ContextAssembled"]
        steps = [r["data"] for r in rows if r["evt"] == "ModelStepFinished"]

        print(SEP + " [c2 provider usage keys] ")
        if not steps:
            print("  FAIL  the model step produced no usage record")
            failures += 1
        else:
            first = steps[0]
            print(f"  raw usage keys: {sorted((first.get('usage') or {}).keys())}")
            print(
                f"  prompt={first['usage'].get('prompt_tokens')} "
                f"cached={first.get('cached_tokens')} latency={first.get('latency_ms')}ms"
            )
            estimate = assembled[0]["estimated_tokens"] if assembled else 0
            real = first["usage"].get("prompt_tokens") or 0
            ratio = real / estimate if estimate else 0
            ok = real > 0 and 0.5 < ratio < 3
            print(f"  calibration real/estimate = {ratio:.2f} (est={estimate})")
            print(f"  {'PASS' if ok else 'FAIL'}  the estimate is in the same ballpark")
            failures += 0 if ok else 1

        print(SEP + " [c3 cache prefix] ")
        if len(assembled) < 2:
            print("  note: only one iteration ran; re-run for the prefix check")
        else:
            share = assembled[1]["prefix_shared_ratio"]
            cached = steps[1].get("cached_tokens") if len(steps) > 1 else None
            ok = share >= 0.9
            print(f"  iteration 2 shares {share:.3f} of the request prefix")
            print(f"  provider-reported cached tokens on step 2: {cached}")
            print(f"  {'PASS' if ok else 'FAIL'}  an append-only turn keeps its prefix")
            failures += 0 if ok else 1

        print(SEP + " [c4 turn ledger] ")
        outcome = loop.last_outcome
        usage = outcome.usage if outcome else None
        ok = bool(usage) and (usage or {}).get("total_tokens", 0) > 0
        print(f"  status={outcome.status if outcome else None} usage={usage}")
        print(f"  {'PASS' if ok else 'FAIL'}  the outcome carries its own spend")
        failures += 0 if ok else 1

    print(SEP)
    print(f"SUMMARY {3 - failures}/3 real cases passed")
    return 0 if failures == 0 else 2


CASES: list[tuple[str, CaseFn]] = [
    ("c1", c1_tool_spans),
    ("c5", c5_secret_never_reaches_disk),
    ("c6", c6_duplicate_guard),
    ("c7", c7_hook_blocks_a_call),
    ("c8", c8_token_gate),
    ("c9", c9_degraded_is_visible),
]


async def main(only: str = "", config_path: str = "", real: bool = False) -> int:
    if real:
        return await real_cases(config_path or str(ROOT / "config.local.yaml"))
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    selected = [(name, fn) for name, fn in CASES if not wanted or name in wanted]
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="trace_cases_") as tmp:
        for name, fn in selected:
            root = Path(tmp) / name
            root.mkdir(parents=True, exist_ok=True)
            print(SEP + f" [{name}] ")
            try:
                ok, detail = await fn(root)
            except Exception as exc:  # noqa: BLE001 — a case reports, it does not crash
                ok, detail = False, f"raised {type(exc).__name__}: {exc}"
            print(f"  {'PASS' if ok else 'FAIL'}  {detail}")
            results.append((name, ok, detail))
    print(SEP)
    for name, ok, _detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(1 for _name, ok, _detail in results if ok)
    print(f"SUMMARY {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated case names")
    parser.add_argument("--real", action="store_true", help="run the live-LLM cases")
    parser.add_argument("--config", default="", help="config file for --real")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.only, args.config, args.real)))
