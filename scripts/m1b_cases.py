#!/usr/bin/env python3
"""M1b acceptance cases — the agent table, run against the real units.

Every case drives the real machinery: a real ``build_world`` (config → engine
→ tools → MCP → loop), a real session file, a real filesystem.  The only fakes
are the *model* and the *network*, because a real provider would make the
script non-deterministic.

The point of these cases is not "the unit works" — the unit tests already say
that.  It is: **does the whole thing close the loop?**  A posture change has
to survive the command layer, the session file, a process restart, the tool
catalogue, the permission verdict and the wire; an agent's rules have to hold
for the child and not leak into the parent; and the things that are supposed
to be one fact have to stay one fact.

    .venv/bin/python scripts/m1b_cases.py
    .venv/bin/python scripts/m1b_cases.py --only the_posture_survives_a_restart
    .venv/bin/python scripts/m1b_cases.py --list

The groups are:
  * the contract   — the file carries one truth; the derived mode agrees
  * the loop       — switch → commit → history → event → disk → restart
  * the queue      — a flip asked for mid-turn waits, and survives a crash
  * the authority  — the agent's rules bind the child, and only the child
  * the catalogue  — what the model may call matches what it may do
  * the wire       — what the frontend is told matches what happened
  * the drift      — the things that must not become two truths
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

from ai_coding.agents.registry import builtin_catalog, load_agents  # noqa: E402
from ai_coding.core.events import AgentSwitched  # noqa: E402
from ai_coding.domain.stance import PostureResult  # noqa: E402
from ai_coding.security.permission_gate import PermissionDecision  # noqa: E402
from ai_coding.security.permissions import Effect, Rule  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


# ---- the fixtures -----------------------------------------------------------


def _config(root: Path, *, default_agent: str = "") -> Path:
    """A config that names a model but never reaches one."""
    path = root / "config.yaml"
    body = (
        "models:\n"
        "  m:\n"
        "    name: m\n"
        "    base_url: http://127.0.0.1:1/v1\n"
        "    api_key: k\n"
        "default_model: m\n"
        "ai:\n"
        "  stop_policy: natural\n"
        "  max_tool_iterations: 2\n"
    )
    if default_agent:
        body += f"  default_agent: {default_agent}\n"
    path.write_text(body, encoding="utf-8")
    return path


async def _world(root: Path, *, default_agent: str = "") -> Any:
    """A real world, built the way the TUI builds it."""
    from ai_coding.server.world import build_world

    cfg = _config(root, default_agent=default_agent)
    return await build_world(str(cfg), str(root), None)


class _Recorder:
    """An observer that keeps everything, the way the bridge keeps events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


async def _command(world: Any, session: Any, text: str) -> _Recorder:
    """Run one slash command through the real dispatcher."""
    from rich.console import Console

    from ai_coding.app.commands import handle_input
    from ai_coding.app.console import DialogueConsole

    recorder = _Recorder()
    console = DialogueConsole(Console(record=True, width=120))
    await handle_input(
        world.loop, text, session, world.sessions, console, recorder
    )
    return recorder


class _Busy:
    """A turn lock that is held — the state a posture flip has to wait for."""

    def locked(self) -> bool:
        return True


def _hold_the_turn(loop: Any) -> Any:
    """Make the loop look busy, and hand back the lock to restore."""
    original = loop._turn_lock
    loop._turn_lock = _Busy()
    return original


# ---- the contract ------------------------------------------------------------


async def the_session_file_carries_one_posture_truth(root: Path) -> tuple[bool, str]:
    """The file holds ``agent`` and nothing else about the posture.

    A second key would be a second fact about the same thing, and two facts
    about one thing drift.  ``mode`` is derived, so it is not written.
    """
    from ai_coding.domain.session import SessionData

    session = SessionData("s", "t", "c", "l")
    session.agent = "plan"
    session.readonly = True
    data = session.to_dict()
    if "mode" in data:
        return False, f"the file still writes a derived mode: {sorted(data)}"
    if data.get("agent") != "plan":
        return False, f"the agent was not written: {data.get('agent')!r}"
    if data.get("readonly") is not True:
        return False, f"the overlay was not written: {data.get('readonly')!r}"
    return True, "agent + readonly, no mirrored mode"


async def the_derived_mode_agrees_with_the_agent(root: Path) -> tuple[bool, str]:
    """``mode`` is a projection of ``agent``, and it cannot be written."""
    from ai_coding.domain.session import SessionData

    session = SessionData("s", "t", "c", "l")
    if session.mode != "normal":
        return False, f"a fresh session is not normal: {session.mode!r}"
    session.agent = "plan"
    if session.mode != "plan":
        return False, f"the plan agent did not project: {session.mode!r}"
    session.agent = "reviewer"
    if session.mode != "normal":
        return False, f"a custom agent projected as plan: {session.mode!r}"
    try:
        session.mode = "plan"  # type: ignore[misc]
    except AttributeError:
        return True, "derived, and not writable"
    return False, "the derived mode accepted an assignment"


async def an_old_file_still_reads_as_the_plan_agent(root: Path) -> tuple[bool, str]:
    """A file written before the agent table still means "plan"."""
    from ai_coding.domain.session import SessionData

    data = SessionData("s", "t", "c", "l").to_dict()
    data["mode"] = "plan"
    loaded = SessionData.from_dict(data)
    if loaded.agent != "plan":
        return False, f"an old plan file read back as {loaded.agent!r}"
    if loaded.mode != "plan":
        return False, f"an old plan file projects {loaded.mode!r}"
    return True, "the old mode key migrates on the reading side"


# ---- the loop ----------------------------------------------------------------


async def a_switch_lands_in_the_file_and_the_history(root: Path) -> tuple[bool, str]:
    """One command, four consequences: field, history, event, disk."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)

        recorder = await _command(world, session, "/agents plan")
        if (session.agent, session.readonly) != ("plan", True):
            return False, f"the session is {session.agent!r}/{session.readonly!r}"
        if len(session.agent_history) != 1:
            return False, f"the history has {len(session.agent_history)} rows"
        switched = recorder.of(AgentSwitched)
        if len(switched) != 1:
            return False, f"the observer saw {len(switched)} switch events"
        if (switched[0].from_agent, switched[0].to_agent) != ("build", "plan"):
            return False, f"the event says {switched[0].from_agent}→{switched[0].to_agent}"

        reloaded = world.sessions.load(session.session_id)
        if reloaded.agent != "plan":
            return False, f"the file reads back as {reloaded.agent!r}"
        if len(reloaded.agent_history) != 1:
            return False, "the history did not reach the file"
        return True, "field + history + event + file all agree"
    finally:
        await world.aclose()


async def the_posture_survives_a_restart(root: Path) -> tuple[bool, str]:
    """A second process sees the same agent — the file is the truth."""
    world = await _world(root)
    session = world.sessions.create()
    session.agent = "plan"
    session.readonly = True
    world.sessions.save(session)
    await world.aclose()

    # A *fresh* world over the same directory: nothing carried in memory.
    world2 = await _world(root)
    try:
        reloaded = world2.sessions.load(session.session_id)
        if reloaded.agent != "plan":
            return False, f"a new process reads {reloaded.agent!r}"
        if reloaded.mode != "plan":
            return False, f"a new process projects {reloaded.mode!r}"
        return True, "the agent survived the restart"
    finally:
        await world2.aclose()


async def a_readonly_change_leaves_no_agent_history(root: Path) -> tuple[bool, str]:
    """The overlay is orthogonal: turning it on is not a switch of agent."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        await _command(world, session, "/readonly on")
        if session.readonly is not True:
            return False, "the overlay did not turn on"
        if session.agent != "build":
            return False, f"the overlay moved the agent to {session.agent!r}"
        if session.agent_history:
            return False, "the overlay wrote an agent-history row"
        return True, "the overlay moved alone"
    finally:
        await world.aclose()


# ---- the queue ---------------------------------------------------------------


async def a_flip_asked_for_mid_turn_waits_for_the_boundary(root: Path) -> tuple[bool, str]:
    """A turn in flight does not change under the operator's feet."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        loop = world.loop

        original = _hold_the_turn(loop)
        try:
            result = loop.request_posture(
                session, agent="plan", readonly=True, reason="/plan"
            )
        finally:
            loop._turn_lock = original

        if result is not PostureResult.QUEUED:
            return False, f"the request returned {result.value}"
        if (session.agent, session.readonly) != ("build", False):
            return False, f"the live session moved to {session.agent!r}"
        if not session.pending_posture:
            return False, "nothing was queued"
        return True, "queued, and the live session did not move"
    finally:
        await world.aclose()


async def a_queued_flip_survives_a_crash(root: Path) -> tuple[bool, str]:
    """The sidecar is what the in-memory copy cannot be: durable."""
    world = await _world(root)
    session = world.sessions.create()
    world.sessions.save(session)
    loop = world.loop

    original = _hold_the_turn(loop)
    try:
        loop.request_posture(session, agent="plan", readonly=True, reason="/plan")
    finally:
        loop._turn_lock = original
    await world.aclose()  # the "crash": the process goes away, the file stays

    world2 = await _world(root)
    try:
        reloaded = world2.sessions.load(session.session_id)
        if not reloaded.pending_posture:
            return False, "the queued flip did not come back"
        if reloaded.pending_posture.get("agent") != "plan":
            return False, f"the queued flip came back as {reloaded.pending_posture}"
        world2.loop._apply_pending_posture(reloaded)
        if (reloaded.agent, reloaded.readonly) != ("plan", True):
            return False, f"it landed as {reloaded.agent!r}/{reloaded.readonly!r}"
        if reloaded.pending_posture is not None:
            return False, "the queue was not cleared"
        sidecar = (
            Path(world2.sessions.root) / "pending-posture" / f"{session.session_id}.json"
        )
        if sidecar.exists():
            return False, "the sidecar survived the commit"
        return True, "queued → crash → recovered → landed → cleared"
    finally:
        await world2.aclose()


async def the_agent_history_is_append_only(root: Path) -> tuple[bool, str]:
    """The history grows; it is never rewritten."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        await _command(world, session, "/agents plan")
        first = list(session.agent_history)
        await _command(world, session, "/agents build")
        if len(session.agent_history) != 2:
            return False, f"the history has {len(session.agent_history)} rows, expected 2"
        if session.agent_history[:1] != first:
            return False, "the first row was rewritten"
        if session.agent_history[1]["from"] != "plan":
            return False, f"the second row starts at {session.agent_history[1]['from']!r}"
        return True, "two rows, the first untouched"
    finally:
        await world.aclose()


# ---- the authority -----------------------------------------------------------


async def the_agent_rules_bind_the_child_and_not_the_parent(root: Path) -> tuple[bool, str]:
    """A sub-agent speaks as its own agent; the parent keeps its own."""
    world = await _world(root)
    try:
        child = world.sessions.create()
        child.agent = "explore"
        parent = world.sessions.create()
        child_rules = world.loop._agent_rules(child)
        parent_rules = world.loop._agent_rules(parent)

        if not child_rules:
            return False, "the explore agent has no rules"
        denial = world.loop.gate.check(
            "write", {"path": "x.py"}, agent_rules=child_rules
        )
        if denial.decision is not PermissionDecision.DENY:
            return False, f"explore allowed a write: {denial.decision}"
        allowed = world.loop.gate.check(
            "read", {"path": "x.py"}, agent_rules=child_rules
        )
        if allowed.decision is not PermissionDecision.ALLOW:
            return False, f"explore refused a read: {allowed.decision}"
        # The parent is not explore; it must not inherit that narrowing.
        parent_write = world.loop.gate.check(
            "write", {"path": "x.py"}, agent_rules=parent_rules
        )
        if parent_write.decision is PermissionDecision.DENY:
            return False, "the parent inherited the child's narrowing"
        return True, "child narrowed, parent not"
    finally:
        await world.aclose()


async def the_child_speaks_as_its_target_agent(root: Path) -> tuple[bool, str]:
    """A sub-agent's **tool list** is the observable proof of its identity.

    The table having an ``explore`` entry proves nothing: a child whose
    session said "build" would carry no agent layer, and its tool list would
    still contain write/edit/bash.  This case closes that hole — it is the
    one that a mutation of the child's agent field slips past.
    """
    from ai_coding.core.subagent import SubAgentRunner
    from ai_coding.tools.builder import build_tool_registry

    seen_tools: list[list[dict]] = []

    class _SpyEngine:
        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            seen_tools.append(list(tools))
            from ai_coding.domain.loop import StepResult

            return StepResult(text="done", finish_reason="stop")

        async def aclose(self) -> None:
            return None

    from ai_coding.config.models import AIConfig, AppConfig

    app_cfg = AppConfig(
        models={}, default_model="", ai=AIConfig(stop_policy="natural", max_tool_iterations=1)
    )
    runner = SubAgentRunner(
        app_cfg,
        engine_factory=_SpyEngine,
        build_tools=lambda wd: build_tool_registry(
            app_cfg, wd or str(root), None, include_subagent=False
        ),
        workspace=str(root),
        timeout_seconds=30.0,
        agents=builtin_catalog(),
    )
    result = await runner.run("look", isolate=False, agent="explore")
    if result.status != "completed":
        return False, f"the child did not run: {result.status} {result.error}"
    if not seen_tools:
        return False, "the child never called the model"
    names = {
        tool.get("function", {}).get("name")
        for tool in seen_tools[0]
        if isinstance(tool, dict)
    }
    if "read" not in names:
        return False, f"the child lost its read tool: {sorted(names)}"
    for hidden in ("write", "edit", "bash"):
        if hidden in names:
            return False, f"{hidden} was offered to an explore child: {sorted(names)}"
    return True, f"the child carried the explore surface ({len(names)} tools)"
async def the_agent_deny_beats_a_session_allow(root: Path) -> tuple[bool, str]:
    """A session ``allow`` cannot reopen what the agent denied."""
    world = await _world(root)
    try:
        explore_session = world.sessions.create()
        explore_session.agent = "explore"
        explore = world.loop._agent_rules(explore_session)
        session_allow = (Rule(action="write", resource="*", effect=Effect.ALLOW),)
        decision = world.loop.gate.check(
            "write",
            {"path": "x.py"},
            session_rules=session_allow,
            agent_rules=explore,
        )
        if decision.decision is not PermissionDecision.DENY:
            return False, f"the session allow won: {decision.decision}"
        return True, "the agent's deny is absolute"
    finally:
        await world.aclose()


async def the_agent_allow_beats_the_base_tier(root: Path) -> tuple[bool, str]:
    """The agent's ``allow`` overrides the base tier's ``ask``.

    The veto layer only speaks about *denies*, so the agent's allows live in
    the chain — and their position is what decides them.  The base tier asks
    for ``bash``; an agent that allows ``bash`` must not be asked anyway.
    This is the half of the agent layer that the deny cases cannot see.
    """
    world = await _world(root)
    try:
        allow_bash = (Rule(action="bash", resource="*", effect=Effect.ALLOW),)
        decision = world.loop.gate.check(
            "bash", {"command": "ls"}, agent_rules=allow_bash
        )
        if decision.decision is not PermissionDecision.ALLOW:
            return False, f"the agent's allow did not win over the base ask: {decision.decision}"
        # And the base tier still asks when there is no agent layer.
        plain = world.loop.gate.check("bash", {"command": "ls"})
        if plain.decision is not PermissionDecision.WARN:
            return False, f"the base tier stopped asking: {plain.decision}"
        return True, "the agent's allow wins the base tier, and the base still asks"
    finally:
        await world.aclose()


async def the_catalogue_hides_what_the_agent_denied(root: Path) -> tuple[bool, str]:
    """``hides`` must consult the agent layer **on its own**.

    This is not redundancy: the chain resolves last-match-wins, so a session
    ``allow`` would *un*-hide a tool the agent denied.  The chain's copy of
    the agent layer and the veto's copy would then disagree — and the model
    would be offered a tool whose call is refused.  The standalone lookup is
    the catalogue face of the same veto, and the two must agree.
    """
    world = await _world(root)
    try:
        explore = world.sessions.create()
        explore.agent = "explore"
        rules = world.loop._agent_rules(explore)
        # A scoped denial: the tool stays, the call is refused.
        scoped = (Rule(action="write", resource="*.py", effect=Effect.DENY),)
        if world.loop.gate.hides("write", agent_rules=scoped) is not False:
            return False, "a scoped denial hid the whole tool"
        # A whole-action denial: the tool goes.
        whole = (Rule(action="write", resource="*", effect=Effect.DENY),)
        if world.loop.gate.hides("write", agent_rules=whole) is not True:
            return False, "a whole-action denial did not hide the tool"
        if world.loop.gate.hides("write", agent_rules=rules) is not True:
            return False, "the explore agent's write is still offered"

        # The decisive one: a session ``allow`` must not put the tool back in
        # the catalogue.  The chain would say "keep it" (last match wins);
        # the veto's face says "hide it".  If the standalone copy were gone,
        # the model would be offered a tool whose every call is refused.
        session_allow = (Rule(action="write", resource="*", effect=Effect.ALLOW),)
        if world.loop.gate.hides(
            "write", session_rules=session_allow, agent_rules=whole
        ) is not True:
            return False, "a session allow put a denied tool back in the catalogue"
        return True, (
            "scoped denial keeps the tool, whole denial hides it, "
            "and a session allow cannot put it back"
        )
    finally:
        await world.aclose()


# ---- the catalogue -----------------------------------------------------------


async def the_catalogue_matches_what_the_agent_may_do(root: Path) -> tuple[bool, str]:
    """What the model may *call* is what the agent may *do*.

    A tool the agent denied must not be in the schema list, and a tool it
    allowed must be — otherwise the model is told about a tool it cannot use,
    or not told about one it can.
    """
    world = await _world(root)
    try:
        session = world.sessions.create()
        session.agent = "explore"
        rules = world.loop._agent_rules(session)
        tools = world.loop._surface_tools("normal", False, (), rules)
        names = {tool.name for tool in tools}

        for hidden in ("write", "edit", "bash"):
            if hidden in names:
                return False, f"{hidden} is still offered to explore"
        for allowed in ("read", "glob", "grep"):
            if allowed not in names:
                return False, f"{allowed} was withheld from explore"
        for name in names:
            decision = world.loop.gate.check(
                name, {"path": "x", "pattern": "*"}, agent_rules=rules
            )
            if decision.decision is PermissionDecision.DENY:
                return False, f"{name} is offered but refused"
        return True, f"catalogue and verdict agree ({len(names)} tools)"
    finally:
        await world.aclose()


async def a_broken_agent_file_is_skipped_with_its_reason(root: Path) -> tuple[bool, str]:
    """A file that does not read is skipped whole — and says why."""
    agents_dir = root / ".ai-coding" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "broken.md").write_text(
        "---\nid: broken\nmode: sideways\n---\nbody\n", encoding="utf-8"
    )
    (agents_dir / "good.md").write_text(
        "---\nid: good\ndescription: Fine\n---\nbody\n", encoding="utf-8"
    )

    from ai_coding.paths import Paths

    catalog = load_agents(Paths.for_workspace(root).agent_roots)
    if "good" not in catalog.ids():
        return False, "the good agent was lost"
    if "broken" in catalog.ids():
        return False, "the broken agent was loaded"
    if not any("broken.md" in reason for reason in catalog.skipped):
        return False, f"the reason was not kept: {catalog.skipped}"
    return True, "broken skipped with its reason, good loaded"


async def a_bad_rule_row_leaves_no_half_loaded_agent(root: Path) -> tuple[bool, str]:
    """An agent whose rules do not all read is not loaded **at all**.

    Half an agent is the dangerous state: its allow rows are live and its
    deny rows are not, so the agent is *more* capable than it was written to
    be, and nothing says so.  The file has to be skipped whole.
    """
    agents_dir = root / ".ai-coding" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "half.md").write_text(
        "---\n"
        "id: half\n"
        "permissions:\n"
        "  - {action: read, resource: \"*\", effect: allow}\n"
        "  - {action: write, resource: \"*\", effect: maybe}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    from ai_coding.paths import Paths

    catalog = load_agents(Paths.for_workspace(root).agent_roots)
    if "half" in catalog.ids():
        return False, "an agent with an unreadable rule was loaded anyway"
    if not any("half.md" in reason for reason in catalog.skipped):
        return False, f"the reason was not kept: {catalog.skipped}"
    return True, "the whole file was skipped, not half of it"


async def a_project_agent_replaces_a_builtin_whole(root: Path) -> tuple[bool, str]:
    """A nearer scope replaces the farther agent — not a patch of it."""
    agents_dir = root / ".ai-coding" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "build.md").write_text(
        "---\nid: build\nname: Builder\n---\n", encoding="utf-8"
    )
    from ai_coding.paths import Paths

    catalog = load_agents(Paths.for_workspace(root).agent_roots)
    spec = catalog.get("build")
    if spec is None:
        return False, "the builtin build disappeared"
    if spec.name != "Builder":
        return False, f"the project file did not win: {spec.name!r}"
    if spec.description != "":
        return False, "the two agents were merged instead of replaced"
    return True, "replaced whole"


# ---- the wire ---------------------------------------------------------------


async def the_wire_carries_the_switch_and_the_agent(root: Path) -> tuple[bool, str]:
    """What the frontend is told matches what happened."""
    from ai_coding.core.events import AssistantMessage
    from ai_coding.server.bridge import to_wire

    wire = to_wire(
        AgentSwitched(from_agent="build", to_agent="plan", reason="/plan", ts="t")
    )
    if wire is None or wire.get("type") != "agent.switched":
        return False, f"the switch did not map: {wire}"
    data = wire.get("data", {})
    if data.get("from") != "build" or data.get("to") != "plan":
        return False, f"the wire lost an end: {data}"
    if data.get("reason") != "/plan":
        return False, f"the wire lost the reason: {data}"
    plain = to_wire(AssistantMessage(text="done"))
    if plain is None or "agent" in plain.get("data", {}):
        return False, f"an unattributed message grew an agent key: {plain}"
    tagged = to_wire(AssistantMessage(text="done", agent="plan"))
    if tagged is None or tagged["data"].get("agent") != "plan":
        return False, f"the attributed message lost its agent: {tagged}"
    return True, "switch mapped, agent attributed only when it is there"


async def the_load_response_carries_the_history(root: Path) -> tuple[bool, str]:
    """``session.load`` gives the replay its timeline."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        session.agent = "plan"
        session.agent_history = [
            {
                "from": "build",
                "to": "plan",
                "ts": "2026-10-22T10:00:00Z",
                "reason": "/plan",
            }
        ]
        world.sessions.save(session)
        reloaded = world.sessions.load(session.session_id)
        if len(reloaded.agent_history) != 1:
            return False, f"the history has {len(reloaded.agent_history)} rows"
        row = reloaded.agent_history[0]
        for key in ("from", "to", "ts", "reason"):
            if key not in row:
                return False, f"the row is missing {key!r}: {row}"
        return True, "the timeline rows carry from/to/ts/reason"
    finally:
        await world.aclose()


async def the_subagent_target_is_refused_for_a_primary(root: Path) -> tuple[bool, str]:
    """A primary agent is a session's own; it cannot be spawned."""
    world = await _world(root)
    try:
        tool = next(t for t in world.registry.all_tools if t.name == "subAgent")
        _, problem = tool._runner._resolve_target("plan")  # noqa: SLF001
        if "primary" not in problem:
            return False, f"the refusal did not say why: {problem!r}"
        spec, problem = tool._runner._resolve_target("explore")  # noqa: SLF001
        if problem or spec is None or spec.id != "explore":
            return False, f"explore was not accepted: {problem!r}"
        return True, "primary refused, subagent accepted"
    finally:
        await world.aclose()


# ---- the drift --------------------------------------------------------------


def _writes_to(attribute: str) -> list[str]:
    """Every ``session.<attribute> = ...`` in the package, by function.

    Grouped by the function that holds them: the invariant is *one writer*,
    not one line.  ``commit_posture`` writes both fields, and that is exactly
    the shape the invariant wants — one place, both axes.
    """
    import ast

    found: list[str] = []
    for path in (ROOT / "ai_coding").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == attribute
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "session"
                ):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def _enclosing_function(path: Path, line: int) -> str:
    """The name of the function that contains ``line``."""
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return "?"
    best = "?"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            if node.lineno <= line <= end:
                best = f"{node.name} ({path.relative_to(ROOT)})"
    return best


async def the_posture_has_exactly_one_write_path(root: Path) -> tuple[bool, str]:
    """``session.agent`` and ``session.readonly`` are written in one place.

    Two writers means two futures: one of them will be the one that is
    forgotten when a new route is added.
    """
    sites = _writes_to("agent") + _writes_to("readonly")
    if not sites:
        return False, "the posture is never written at all"
    functions = {
        _enclosing_function(ROOT / site.split(":")[0], int(site.split(":")[1]))
        for site in sites
    }
    if len(functions) != 1:
        return False, f"{len(functions)} writer functions: {sorted(functions)}"
    return True, f"one writer function: {sorted(functions)[0]}"


async def the_derived_mode_has_no_writers(root: Path) -> tuple[bool, str]:
    """``session.mode`` is read-only everywhere in the package."""
    writers = _writes_to("mode")
    if writers:
        return False, f"session.mode is written: {writers}"
    return True, "no writer for the derived mode"


async def one_reader_per_concern(root: Path) -> tuple[bool, str]:
    """The rule reader and the frontmatter parser each exist once.

    A second reader for the same shape is how two answers to one question
    appear — and then they disagree.
    """
    import ast

    def definitions(names: set[str]) -> list[str]:
        """Where a function is *defined* — a second one is a second answer."""
        found: list[str] = []
        for path in (ROOT / "ai_coding").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in names:
                        found.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} {node.name}()"
                        )
        return found

    rules = definitions({"rules_from_dicts", "_rule_from"})
    if not rules:
        return False, "no rule reader found at all"

    headers = definitions({"split_frontmatter", "parse_header", "_split_frontmatter"})
    if not headers:
        return False, "no frontmatter parser found"

    # The shared modules are the only places allowed to define one.
    allowed_rule = "/security/permissions.py"
    allowed_header = "/infra/frontmatter.py"
    stray_rules = [r for r in rules if allowed_rule not in r]
    stray_headers = [h for h in headers if allowed_header not in h]
    if stray_rules:
        return False, f"a second rule reader exists: {stray_rules}"
    if stray_headers:
        return False, f"a second frontmatter parser exists: {stray_headers}"

    # And they are actually *used* — a shared module nobody calls is a
    # second implementation wearing a shared name.
    def callers(names: set[str]) -> list[str]:
        found: list[str] = []
        for path in (ROOT / "ai_coding").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "attr", getattr(func, "id", ""))
                    if name in names:
                        found.append(
                            f"{path.relative_to(ROOT)}:{node.lineno} {name}()"
                        )
        return found

    rule_callers = callers({"rules_from_dicts"})
    header_callers = callers({"split_frontmatter", "parse_header"})
    if not rule_callers:
        return False, "the shared rule reader has no caller"
    if not header_callers:
        return False, "the shared frontmatter parser has no caller"
    return True, (
        f"one rule reader ({len(rule_callers)} callers), "
        f"one frontmatter parser ({len(header_callers)} callers)"
    )


async def the_agent_table_has_one_loader(root: Path) -> tuple[bool, str]:
    """The table is loaded by one module, not re-implemented elsewhere."""
    import ast

    impls: list[str] = []
    for path in (ROOT / "ai_coding").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name in ("load_agents", "builtin_catalog"):
                    impls.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}()")
    if len(impls) != 2:
        return False, f"the loader exists {len(impls)} times: {impls}"
    return True, f"one loader module: {impls}"


OFFLINE: dict[str, CaseFn] = {
    # the contract
    "the_session_file_carries_one_posture_truth": the_session_file_carries_one_posture_truth,
    "the_derived_mode_agrees_with_the_agent": the_derived_mode_agrees_with_the_agent,
    "an_old_file_still_reads_as_the_plan_agent": an_old_file_still_reads_as_the_plan_agent,
    # the loop
    "a_switch_lands_in_the_file_and_the_history": a_switch_lands_in_the_file_and_the_history,
    "the_posture_survives_a_restart": the_posture_survives_a_restart,
    "a_readonly_change_leaves_no_agent_history": a_readonly_change_leaves_no_agent_history,
    # the queue
    "a_flip_asked_for_mid_turn_waits_for_the_boundary": (
        a_flip_asked_for_mid_turn_waits_for_the_boundary
    ),
    "a_queued_flip_survives_a_crash": a_queued_flip_survives_a_crash,
    "the_agent_history_is_append_only": the_agent_history_is_append_only,
    # the authority
    "the_agent_rules_bind_the_child_and_not_the_parent": (
        the_agent_rules_bind_the_child_and_not_the_parent
    ),
    "the_child_speaks_as_its_target_agent": the_child_speaks_as_its_target_agent,
    "the_agent_deny_beats_a_session_allow": the_agent_deny_beats_a_session_allow,
    "the_agent_allow_beats_the_base_tier": the_agent_allow_beats_the_base_tier,
    "the_catalogue_hides_what_the_agent_denied": the_catalogue_hides_what_the_agent_denied,
    # the catalogue
    "the_catalogue_matches_what_the_agent_may_do": the_catalogue_matches_what_the_agent_may_do,
    "a_broken_agent_file_is_skipped_with_its_reason": (
        a_broken_agent_file_is_skipped_with_its_reason
    ),
    "a_bad_rule_row_leaves_no_half_loaded_agent": (
        a_bad_rule_row_leaves_no_half_loaded_agent
    ),
    "a_project_agent_replaces_a_builtin_whole": a_project_agent_replaces_a_builtin_whole,
    # the wire
    "the_wire_carries_the_switch_and_the_agent": the_wire_carries_the_switch_and_the_agent,
    "the_load_response_carries_the_history": the_load_response_carries_the_history,
    "the_subagent_target_is_refused_for_a_primary": (
        the_subagent_target_is_refused_for_a_primary
    ),
    # the drift
    "the_posture_has_exactly_one_write_path": the_posture_has_exactly_one_write_path,
    "the_derived_mode_has_no_writers": the_derived_mode_has_no_writers,
    "one_reader_per_concern": one_reader_per_concern,
    "the_agent_table_has_one_loader": the_agent_table_has_one_loader,
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
