#!/usr/bin/env python3
"""M2 acceptance cases — the plan posture, run against the real units.

Two claims, each with a shape that can be broken:

* **T3**: the plan reminder is one thing.  It is queued when the agent
  changes, delivered at a turn boundary, survives a crash, and is
  reconciled on every request — so a compaction that ate it puts it back
  and leaving plan mode says so explicitly.
* **T6**: the plan posture is a rule, not a special case.  The rule
  layer produces exactly the tool surface and the verdicts the old
  read-only overlay produced, and the overlay keeps its own wording for
  the human's own switch.

The cases drive the real machinery: a real ``build_world``, a real
session file, a real filesystem.  The only fakes are the model and the
network.

    .venv/bin/python scripts/m2_cases.py
    .venv/bin/python scripts/m2_cases.py --only the_reminder_survives_a_crash
    .venv/bin/python scripts/m2_cases.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.agents.registry import builtin_catalog  # noqa: E402
from ai_coding.core.plan_reminders import (  # noqa: E402
    PLAN_ENTER_REMINDER,
    PLAN_LEAVE_REMINDER,
    last_reminder,
    reconcile,
    reminder_for,
)
from ai_coding.domain.agents import is_plan_agent  # noqa: E402
from ai_coding.security.permission_gate import (  # noqa: E402
    PermissionDecision,
    PermissionGate,
)
from ai_coding.security.permissions import Effect, Rule  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


# ---- the fixtures -----------------------------------------------------------


def _config(root: Path, *, extra: str = "") -> Path:
    path = root / "config.yaml"
    path.write_text(
        "models:\n"
        "  m:\n"
        "    name: m\n"
        "    base_url: http://127.0.0.1:1/v1\n"
        "    api_key: k\n"
        "default_model: m\n"
        "ai:\n"
        "  stop_policy: natural\n"
        "  max_tool_iterations: 2\n" + extra,
        encoding="utf-8",
    )
    return path


async def _world(root: Path, *, extra: str = "") -> Any:
    from ai_coding.server.world import build_world

    return await build_world(str(_config(root, extra=extra)), str(root), None)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


async def _command(world: Any, session: Any, text: str) -> _Recorder:
    from rich.console import Console

    from ai_coding.app.commands import handle_input
    from ai_coding.app.console import DialogueConsole

    recorder = _Recorder()
    console = DialogueConsole(Console(record=True, width=120))
    await handle_input(
        world.loop, text, session, world.sessions, console, recorder
    )
    return recorder


# ---- T3: the reminder --------------------------------------------------------


async def a_switch_queues_the_reminder(root: Path) -> tuple[bool, str]:
    """The posture changed, so the model has to be told — and the telling is
    queued, not delivered on the spot."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        await _command(world, session, "/agents plan")
        if session.pending_reminders != [
            {"kind": "enter", "text": PLAN_ENTER_REMINDER}
        ]:
            return False, f"the reminder was not queued: {session.pending_reminders}"
        return True, "queued"
    finally:
        await world.aclose()


async def the_reminder_reaches_the_model(root: Path) -> tuple[bool, str]:
    """Not queued in the abstract: the model sees it."""
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.loop import StepResult
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    seen: list[list[str]] = []

    class _Engine:
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            seen.append([m.content or "" for m in messages])
            return StepResult(text="ok", finish_reason="stop")

        async def aclose(self) -> None:
            return None

    sessions = SessionService(root / "sessions")
    loop = AgentLoop(
        _Engine(),
        sessions,
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=2),
        workspace=str(root),
    )
    session = sessions.create()
    sessions.save(session)

    loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    await loop.process_input(session, "plan it")

    flat = [c for row in seen for c in row]
    if PLAN_ENTER_REMINDER not in flat:
        return False, "the model never saw the reminder"
    return True, "the model saw it"


async def the_reminder_is_delivered_as_a_synthetic_message(root: Path) -> tuple[bool, str]:
    """A reminder is the harness talking, so it is marked as one."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        world.loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
        world.loop._apply_pending_reminders(session)
        delivered = [m for m in session.messages if m.content == PLAN_ENTER_REMINDER]
        if len(delivered) != 1:
            return False, f"{len(delivered)} reminders in the history"
        if not delivered[0].synthetic:
            return False, "the reminder is not synthetic"
        return True, "one synthetic message"
    finally:
        await world.aclose()


async def the_reminder_survives_a_crash(root: Path) -> tuple[bool, str]:
    """The posture landed, the reminder did not: it must still arrive."""
    world = await _world(root)
    session = world.sessions.create()
    world.sessions.save(session)
    world.loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    await world.aclose()

    world2 = await _world(root)
    try:
        reloaded = world2.sessions.load(session.session_id)
        if not reloaded.pending_reminders:
            return False, "the queued reminder did not come back"
        world2.loop._apply_pending_reminders(reloaded)
        if not any(m.content == PLAN_ENTER_REMINDER for m in reloaded.messages):
            return False, "it never arrived"
        return True, "queued → crash → recovered → delivered"
    finally:
        await world2.aclose()


async def a_compacted_reminder_is_put_back(root: Path) -> tuple[bool, str]:
    """The history no longer says plan, but the session is in plan mode."""
    text = reconcile([], is_plan=True)
    if text != PLAN_ENTER_REMINDER:
        return False, f"the reconciliation said {text!r}"
    return True, "put back"


async def leaving_plan_says_so(root: Path) -> tuple[bool, str]:
    """Not "no new instruction" — an explicit withdrawal."""
    from ai_coding.domain.message import ChatMessage

    history = [
        ChatMessage(
            role="user", content=PLAN_ENTER_REMINDER, timestamp="t", synthetic=True
        )
    ]
    text = reconcile(history, is_plan=False)
    if text != PLAN_LEAVE_REMINDER:
        return False, f"the reconciliation said {text!r}"
    return True, "withdrawn"


async def the_planning_instruction_is_synthetic(root: Path) -> tuple[bool, str]:
    """The planning round's instruction is the harness's, so it is marked."""
    from ai_coding.core.plan_runner import PLAN_MARK, plan_only

    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        session.agent = "plan"
        session.readonly = True
        await plan_only(world.loop, session, topic="build a thing")
        instructions = [
            m for m in session.messages if (m.content or "").startswith(PLAN_MARK)
        ]
        if not instructions:
            return False, "the planning instruction was not sent"
        if not instructions[0].synthetic:
            return False, "the planning instruction is not synthetic"
        return True, "synthetic"
    finally:
        await world.aclose()


async def the_humans_words_are_not_the_harnesss(root: Path) -> tuple[bool, str]:
    """One rule covers the reminder and the instruction."""
    from ai_coding.core.request_assembly import human_utterance
    from ai_coding.domain.message import ChatMessage

    messages = [
        ChatMessage(role="user", content="the real question", timestamp="t"),
        ChatMessage(
            role="user",
            content=PLAN_ENTER_REMINDER,
            timestamp="t",
            synthetic=True,
        ),
    ]
    if human_utterance(messages) != "the real question":
        return False, f"the utterance was {human_utterance(messages)!r}"
    return True, "the human's line"


# ---- T6: the rule ------------------------------------------------------------


async def the_plan_rule_produces_the_old_tool_surface(root: Path) -> tuple[bool, str]:
    """The rule layer and the old overlay must agree exactly.

    The overlay refused every ``mutates_world`` tool; the rule set has to
    refuse the same set, or the prompt and the surface start telling
    different stories.
    """
    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.tools.builder import build_tool_registry

    registry = build_tool_registry(
        AppConfig(models={}, default_model="", ai=AIConfig()), str(root), None
    )
    today = sorted(
        t.name for t in registry.all_tools if not t.plan_only and not t.mutates_world
    )
    rules = builtin_catalog().rules("plan")
    gate = PermissionGate()
    new = sorted(
        t.name
        for t in registry.all_tools
        if not t.plan_only and not gate.hides(t.name, agent_rules=rules)
    )
    if today != new:
        return False, (
            f"the surfaces differ: "
            f"only-old={set(today) - set(new)} only-new={set(new) - set(today)}"
        )
    return True, f"the same {len(today)} tools"


async def the_plan_rule_produces_the_old_verdicts(root: Path) -> tuple[bool, str]:
    """The same six tools, denied for the same reason."""
    gate = PermissionGate()
    rules = builtin_catalog().rules("plan")
    for tool in ("write", "edit", "bash", "subAgent", "webfetch", "websearch"):
        decision = gate.check(tool, {"path": "x", "task": "t"}, agent_rules=rules)
        if decision.decision is not PermissionDecision.DENY:
            return False, f"{tool} was {decision.decision}"
    for tool in ("read", "grep", "glob"):
        decision = gate.check(tool, {"path": "x", "pattern": "*"}, agent_rules=rules)
        if decision.decision is not PermissionDecision.ALLOW:
            return False, f"{tool} was {decision.decision}"
    return True, "the same six denied, the readers allowed"


async def the_readonly_overlay_keeps_its_own_wording(root: Path) -> tuple[bool, str]:
    """``/readonly on`` is the human's switch and keeps its text (G9)."""
    from ai_coding.core.dispatch import ToolDispatcher
    from ai_coding.domain.message import ToolCallRef, ToolResult
    from ai_coding.domain.task import TaskState
    from ai_coding.security.approval import ApprovalResult
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.tools.base import BaseTool, ToolContext
    from ai_coding.tools.registry import ToolRegistry

    class _Tool(BaseTool):
        mutates_world = True

        @property
        def name(self) -> str:
            return "write"

        @property
        def description(self) -> str:
            return "fake"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, params, ctx=None):  # type: ignore[override]
            return ToolResult(success=True, output="ok")

    class _Approval:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        async def approve(self, tool_name, params, reason):
            self.calls.append(tool_name)
            return ApprovalResult.YES

    registry = ToolRegistry()
    registry.register(_Tool())
    approval = _Approval()
    dispatcher = ToolDispatcher(registry, PermissionGate(), approval, max_parallel=4)
    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="write", arguments="{}")],
        ToolContext(task=TaskState(goal="g"), workspace=str(root), readonly=True),
    )
    result = outcomes[0].result
    if result.error != "readonly_denied":
        return False, f"the error code is {result.error!r}"
    if "只读" not in result.output:
        return False, "the wording moved"
    if approval.calls:
        return False, "an approval was spent on a refused call"
    return True, "the overlay still answers in its own words"


async def the_strict_tier_denies_more_but_keeps_the_exits(root: Path) -> tuple[bool, str]:
    """A stricter posture must not close the plan's exits."""
    soft = builtin_catalog(strict_readonly=False).rules("plan")
    strict = builtin_catalog(strict_readonly=True).rules("plan")
    soft_denied = {r.action for r in soft if r.effect is Effect.DENY}
    strict_denied = {r.action for r in strict if r.effect is Effect.DENY}
    if not strict_denied > soft_denied:
        return False, "the strict tier denies nothing more"
    for exit_tool in ("present_plan", "finish_research"):
        if exit_tool in strict_denied:
            return False, f"the strict tier closed {exit_tool}"
    return True, "more denied, both exits open"


async def the_alias_table_covers_the_actions(root: Path) -> tuple[bool, str]:
    """``deny edit *`` has to reach write as well as edit."""
    gate = PermissionGate()
    rules = (Rule(action="edit", resource="*", effect=Effect.DENY),)
    for tool in ("write", "edit"):
        if gate.hides(tool, agent_rules=rules) is not True:
            return False, f"{tool} was not hidden by the alias"
    if gate.hides("read", agent_rules=rules) is not False:
        return False, "read was hidden by an edit rule"
    return True, "edit covers write and edit, and nothing else"


async def the_reminder_and_the_rule_agree_on_the_posture(root: Path) -> tuple[bool, str]:
    """One posture, two faces: the reminder says it, the rule enforces it."""
    if is_plan_agent("plan") is not True:
        return False, "the plan agent is not the plan agent"
    if reminder_for("build", "plan") != PLAN_ENTER_REMINDER:
        return False, "entering plan does not produce the enter reminder"
    if reminder_for("plan", "build") != PLAN_LEAVE_REMINDER:
        return False, "leaving plan does not produce the leave reminder"
    if last_reminder([]) is not None:
        return False, "an empty history has a reminder"
    return True, "the same posture, said and enforced"


async def the_assembler_reconciles_the_reminder(root: Path) -> tuple[bool, str]:
    """The reconciliation is wired, not just written.

    The pure function says *what* is missing; only the assembler decides
    to ask.  A reminder that is computed and never asked for is a
    reminder nobody gets.
    """
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.domain.loop import StepResult
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.registry import ToolRegistry

    seen: list[list[str]] = []

    class _Engine:
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            seen.append([m.content or "" for m in messages])
            return StepResult(text="ok", finish_reason="stop")

        async def aclose(self) -> None:
            return None

    sessions = SessionService(root / "sessions")
    loop = AgentLoop(
        _Engine(),
        sessions,
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=2),
        workspace=str(root),
    )
    session = sessions.create()
    sessions.save(session)
    # The session is in plan mode, but the history never says so -- the
    # state a compaction leaves behind.
    session.agent = "plan"
    session.readonly = True

    await loop.process_input(session, "what now?")

    flat = [c for row in seen for c in row]
    if PLAN_ENTER_REMINDER not in flat:
        return False, "the assembler never asked for the reminder"
    return True, "the assembler put it back"


async def the_rule_speaks_before_the_overlay(root: Path) -> tuple[bool, str]:
    """Two refusals, and the more specific one answers.

    The plan agent's rule names what was refused; the overlay is the
    human's own switch and keeps its own wording.  When both apply, the
    rule speaks — otherwise the model reads a reason for a rule it did
    not break.
    """
    from ai_coding.core.dispatch import ToolDispatcher
    from ai_coding.domain.message import ToolCallRef, ToolResult
    from ai_coding.domain.task import TaskState
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.tools.base import BaseTool, ToolContext
    from ai_coding.tools.registry import ToolRegistry

    class _Tool(BaseTool):
        mutates_world = True

        @property
        def name(self) -> str:
            return "write"

        @property
        def description(self) -> str:
            return "fake"

        @property
        def params_json_schema(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, params, ctx=None):  # type: ignore[override]
            return ToolResult(success=True, output="ok")

    registry = ToolRegistry()
    registry.register(_Tool())
    dispatcher = ToolDispatcher(registry, PermissionGate(), None, max_parallel=4)
    rules = builtin_catalog().rules("plan")
    outcomes = await dispatcher.dispatch_batch(
        [ToolCallRef(id="c1", name="write", arguments="{}")],
        ToolContext(
            task=TaskState(goal="g"),
            workspace=str(root),
            readonly=True,
            agent_rules=rules,
        ),
    )
    result = outcomes[0].result
    if result.error != "denied":
        return False, f"the overlay answered instead: {result.error!r}"
    if "edit" not in result.output:
        return False, f"the rule's reason is missing: {result.output!r}"
    return True, "the rule answered"

OFFLINE: dict[str, CaseFn] = {
    # T3
    "a_switch_queues_the_reminder": a_switch_queues_the_reminder,
    "the_reminder_reaches_the_model": the_reminder_reaches_the_model,
    "the_reminder_is_delivered_as_a_synthetic_message": (
        the_reminder_is_delivered_as_a_synthetic_message
    ),
    "the_reminder_survives_a_crash": the_reminder_survives_a_crash,
    "a_compacted_reminder_is_put_back": a_compacted_reminder_is_put_back,
    "leaving_plan_says_so": leaving_plan_says_so,
    "the_planning_instruction_is_synthetic": the_planning_instruction_is_synthetic,
    "the_humans_words_are_not_the_harnesss": the_humans_words_are_not_the_harnesss,
    # T6
    "the_plan_rule_produces_the_old_tool_surface": (
        the_plan_rule_produces_the_old_tool_surface
    ),
    "the_plan_rule_produces_the_old_verdicts": the_plan_rule_produces_the_old_verdicts,
    "the_readonly_overlay_keeps_its_own_wording": (
        the_readonly_overlay_keeps_its_own_wording
    ),
    "the_strict_tier_denies_more_but_keeps_the_exits": (
        the_strict_tier_denies_more_but_keeps_the_exits
    ),
    "the_alias_table_covers_the_actions": the_alias_table_covers_the_actions,
    "the_reminder_and_the_rule_agree_on_the_posture": (
        the_reminder_and_the_rule_agree_on_the_posture
    ),
    "the_assembler_reconciles_the_reminder": the_assembler_reconciles_the_reminder,
    "the_rule_speaks_before_the_overlay": the_rule_speaks_before_the_overlay,
}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name in OFFLINE:
            print(name)
        return 0

    cases = dict(OFFLINE)
    if args.only:
        cases = {name: fn for name, fn in cases.items() if name == args.only}
        if not cases:
            print(f"no such case: {args.only}")
            return 2

    passed = failed = 0
    for name, case in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            try:
                ok, detail = await case(Path(directory))
            except Exception as exc:  # noqa: BLE001 — one broken case must not stop the run
                ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
        passed += ok
        failed += not ok

    print(SEP)
    print(f"SUMMARY {passed}/{passed + failed} passed ({len(OFFLINE)} cases)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
