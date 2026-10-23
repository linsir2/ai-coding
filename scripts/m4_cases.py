#!/usr/bin/env python3
"""M4d closing cases — the ten real-world proofs, against the real chain.

Ten claims from 30-M4 §7.6, each driven through the production path
(``SessionData`` / ``AgentLoop`` / ``ToolDispatcher`` / ``WriteTool`` /
``ReadTool`` / ``GrepTool`` / the server handler table).  The only fakes are
the model and the network, exactly like ``scripts/m3_cases.py``: a scripted
engine stands in for the model where a child must run, and no test double
stands in for the gate, the sandbox, the dispatcher, or the handlers.

    .venv/bin/python scripts/m4_cases.py
    .venv/bin/python scripts/m4_cases.py --only a_workspace_grep_does_not_return_a_dotenv
    .venv/bin/python scripts/m4_cases.py --list
    .venv/bin/python scripts/m4_cases.py --mutate rg_excludes --only <case>

Exit code is non-zero when any case fails.  Each ``--mutate X`` breaks exactly
the decision its case claims to pin; a case that still passes under its
mutation is a case that would not have caught the defect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.agents.registry import builtin_catalog  # noqa: E402
from ai_coding.core.dispatch import ToolDispatcher  # noqa: E402
from ai_coding.domain.message import ToolCallRef  # noqa: E402
from ai_coding.domain.task import TaskState  # noqa: E402
from ai_coding.security.approval import (  # noqa: E402
    ApprovalCallback,
    ApprovalResult,
    AutoApproveCallback,
)
from ai_coding.security.file_state_tracker import FileStateTracker  # noqa: E402
from ai_coding.security.permission_gate import PermissionGate  # noqa: E402
from ai_coding.security.sandbox import Sandbox  # noqa: E402
from ai_coding.tools.base import ToolContext  # noqa: E402
from ai_coding.tools.grep_tool import GrepTool  # noqa: E402
from ai_coding.tools.read_tool import ReadTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402
from ai_coding.tools.write_tool import WriteTool  # noqa: E402

CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


def _config(root: Path) -> Path:
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
        "  max_tool_iterations: 2\n",
        encoding="utf-8",
    )
    return path


async def _world(root: Path) -> Any:
    from ai_coding.server.world import build_world

    return await build_world(str(_config(root)), str(root), None)


def _plan_ctx(root: Path, **over: Any) -> ToolContext:
    base: dict[str, Any] = {
        "task": TaskState(goal="g"),
        "workspace": str(root),
        "session_id": "s",
        "mode": "plan",
        "readonly": True,
        "agent_rules": builtin_catalog().rules("plan"),
    }
    base.update(over)
    return ToolContext(**base)


def _normal_ctx(root: Path, **over: Any) -> ToolContext:
    base: dict[str, Any] = {
        "task": TaskState(goal="g"),
        "workspace": str(root),
        "session_id": "s",
        "mode": "normal",
        "readonly": False,
    }
    base.update(over)
    return ToolContext(**base)


class _HumanApproval(ApprovalCallback):
    is_human = True

    def __init__(self, answers: list[ApprovalResult]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    async def approve(self, tool_name, params, reason):  # type: ignore[no-untyped-def]
        self.asked.append(tool_name)
        return self.answers.pop(0)


# ---- 1: swapped symlink -----------------------------------------------


async def a_swapped_symlink_cannot_write_outside_the_plan_directory(
    root: Path,
) -> tuple[bool, str]:
    """check-time resolve passes, execute-time fd refuses; src/ stays clean.

    Cooperative simulation (not a kernel-level race proof): the swap is a
    single-threaded callback flip.  It proves the fd chain does not follow
    an already-moved directory, not that the flip window is race-free.
    """
    from ai_coding.domain.agents import PLAN_DIR

    workspace = root / "ws"
    workspace.mkdir()
    plan_root = workspace / PLAN_DIR
    plan_root.mkdir(parents=True)
    outside = workspace / "src"
    outside.mkdir()
    sandbox = Sandbox(workspace)
    original_resolve = sandbox.resolve
    swapped = False

    def resolve_then_swap(path: str | Path) -> Path:
        nonlocal swapped
        resolved = original_resolve(path)
        if not swapped:
            swapped = True
            # The attacker wins the race: the plan directory itself becomes a
            # link to src/ between the gate check and the tool execute.
            plan_root.rmdir()
            plan_root.symlink_to(outside)
        return resolved

    sandbox.resolve = resolve_then_swap  # type: ignore[method-assign]
    gate = PermissionGate(workspace=str(workspace))
    registry = ToolRegistry()
    registry.register(WriteTool(sandbox, FileStateTracker(), plan_root=plan_root))
    dispatcher = ToolDispatcher(registry, gate)
    ctx = _plan_ctx(workspace)
    # Gate check happens inside dispatch_batch's _prepare; the swap fires on
    # the first resolve, so execute runs after the directory moved.
    outcomes = await dispatcher.dispatch_batch(
        [
            ToolCallRef(
                id="c1",
                name="write",
                arguments=json.dumps(
                    {"path": ".ai-coding/plan/pwn.txt", "content": "escaped"}
                ),
            )
        ],
        ctx,
    )
    outcome = outcomes[0]
    leaked = outside / "pwn.txt"
    if outcome.result.success:
        return False, f"the swapped write succeeded: {outcome.result.output!r}"
    if leaked.exists():
        return False, f"the swap escaped: {leaked.read_text()!r}"
    return True, f"refused after swap ({outcome.result.output[:80]!r}); src/ clean"


# ---- 2: workspace grep skips .env (rg path) ----------------------------


async def a_workspace_grep_does_not_return_a_dotenv(
    root: Path,
) -> tuple[bool, str]:
    """.env is filtered on the ripgrep path; ordinary files still surface."""
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / "a.txt").write_text("TOKEN=ok\n", encoding="utf-8")
    (root / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
    tool = GrepTool(Sandbox(root), use_ripgrep=True)
    result = await tool.execute({"pattern": "TOKEN"})
    if not result.success:
        return False, f"grep failed: {result.output!r}"
    if ".env:" in result.output or ".env:1" in result.output:
        # ".env.example" legitimately contains ".env"; check the bare name.
        lines = result.output.splitlines()
        bare = [ln for ln in lines if ln.startswith(".env:")]
        if bare:
            return False, f".env leaked: {bare[0]!r}"
    if "a.txt:1:" not in result.output:
        return False, f"ordinary file lost: {result.output!r}"
    if ".env.example:1:" not in result.output:
        return False, f"example wrongly hidden: {result.output!r}"
    return True, "rg tree search: .env absent, a.txt + .env.example present"


# ---- 3: workspace grep skips every credential (python path) -------------


async def a_workspace_grep_does_not_return_any_credential(
    root: Path,
) -> tuple[bool, str]:
    """The full vocabulary is filtered on the Python fallback path."""
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / ".npmrc").write_text("TOKEN=npm\n", encoding="utf-8")
    (root / "id_rsa").write_text("TOKEN=key\n", encoding="utf-8")
    (root / "ok.txt").write_text("TOKEN=ok\n", encoding="utf-8")
    tool = GrepTool(Sandbox(root), use_ripgrep=False)
    result = await tool.execute({"pattern": "TOKEN"})
    if not result.success:
        return False, f"grep failed: {result.output!r}"
    for leaked in (".env:", ".npmrc:", "id_rsa:"):
        if leaked in result.output:
            return False, f"credential leaked: {leaked} in {result.output!r}"
    if "ok.txt:1:" not in result.output:
        return False, f"ordinary file lost: {result.output!r}"
    return True, "python tree search: .env/.npmrc/id_rsa absent, ok.txt present"


# ---- 4: credential symlink ----------------------------------------------


async def a_credential_symlink_is_not_exposed_by_grep(
    root: Path,
) -> tuple[bool, str]:
    """config.txt -> .env is judged by its resolved target, not its name."""
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (root / "config.txt").symlink_to(root / ".env")
    (root / "ok.txt").write_text("TOKEN=ok\n", encoding="utf-8")
    tool = GrepTool(Sandbox(root), use_ripgrep=False)
    result = await tool.execute({"pattern": "TOKEN"})
    if not result.success:
        return False, f"grep failed: {result.output!r}"
    if "config.txt:" in result.output:
        return False, f"symlink alias leaked: {result.output!r}"
    if ".env:" in result.output:
        return False, f".env leaked: {result.output!r}"
    if "ok.txt:1:" not in result.output:
        return False, f"ordinary file lost: {result.output!r}"
    return True, "config.txt->.env absent by resolve(); ok.txt present"


# ---- 5: approved credential grep ----------------------------------------


async def an_approved_credential_grep_still_returns_it(
    root: Path,
) -> tuple[bool, str]:
    """Unapproved is refused; a human YES opens this one call; auto-YES does not."""
    (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    tool_direct = GrepTool(Sandbox(root), use_ripgrep=False)
    refused = await tool_direct.execute({"pattern": "TOKEN", "path": ".env"})
    if refused.error != "credential_approval_required":
        return False, f"explicit .env without approval was {refused.output!r}"
    # The read path shares the same vocabulary + approval bit (L2 is one rule).
    read_refused = await ReadTool(Sandbox(root), FileStateTracker()).execute(
        {"path": ".env"}
    )
    if read_refused.error != "credential_approval_required":
        return False, f"explicit read .env without approval was {read_refused.output!r}"

    gate = PermissionGate(workspace=str(root))
    registry = ToolRegistry()
    registry.register(GrepTool(Sandbox(root), use_ripgrep=False))
    human = _HumanApproval([ApprovalResult.YES])
    dispatcher = ToolDispatcher(registry, gate, human)
    approved = (
        await dispatcher.dispatch_batch(
            [ToolCallRef(id="c1", name="grep", arguments='{"pattern":"TOKEN","path":".env"}')],
            _normal_ctx(root),
        )
    )[0]
    if not approved.result.success or "TOKEN=secret" not in approved.result.output:
        return False, f"human-approved grep failed: {approved.result.output!r}"

    gate2 = PermissionGate(workspace=str(root))
    registry2 = ToolRegistry()
    registry2.register(GrepTool(Sandbox(root), use_ripgrep=False))
    auto = ToolDispatcher(registry2, gate2, AutoApproveCallback())
    auto_out = (
        await auto.dispatch_batch(
            [ToolCallRef(id="c1", name="grep", arguments='{"pattern":"TOKEN","path":".env"}')],
            _normal_ctx(root),
        )
    )[0]
    # AutoApproveCallback.is_human is False, so the dispatcher never sets the
    # one-call flag: the tool still refuses.  This is the approval-source
    # half of the case (human opens, automation does not).
    if auto_out.result.error != "credential_approval_required":
        return False, f"auto-approval opened a credential: {auto_out.result.output!r}"
    if "TOKEN=secret" in auto_out.result.output:
        return False, "auto-approval leaked the secret"
    return True, "unapproved refused; human YES reads; auto YES still refused"


# ---- 6: python one-liner -------------------------------------------------


async def a_python_one_liner_cannot_read_outside(
    root: Path,
) -> tuple[bool, str]:
    """The obvious literal inside -c is a path; dynamics/URLs are not claimed."""
    from ai_coding.core.acceptance import _needs_a_human

    ws = root / "ws"
    ws.mkdir()
    (root / "secret.txt").write_text("secret", encoding="utf-8")
    (ws / "inside.txt").write_text("inside", encoding="utf-8")
    double_q = _needs_a_human(
        'python -c "print(open(\'../secret.txt\').read())"', str(ws)
    )
    single_q = _needs_a_human(
        "python -c 'print(open(\"../secret.txt\").read())'", str(ws)
    )
    if not double_q:
        return False, "double-quoted open('../secret.txt') not detected"
    if not single_q:
        return False, "single-quoted literal not detected"
    # Boundary promises: ordinary text is not a path, dynamics are not claimed.
    for innocent in ("grep 'a/b' f", "pytest tests/unit", "curl https://x.dev/a/b"):
        if _needs_a_human(innocent, str(ws)):
            return False, f"false positive: {innocent!r}"
    dynamic = 'python -c "name=\'secret.txt\'; print(open(\'../\' + name).read())"'
    if _needs_a_human(dynamic, str(ws)):
        return False, "dynamic concatenation claimed closed (known limitation)"
    inside = _needs_a_human('python -c "open(\'./inside.txt\')"', str(ws))
    if inside:
        return False, f"in-workspace literal refused: {inside!r}"
    return True, f"outside refused ({double_q[:40]!r}); dynamics honestly empty"


# ---- 7/8: TUI list+switch, mid-turn queued -------------------------------


class _IpcClient:
    """Minimal symmetric endpoint client over a Unix socket (test parity)."""

    def __init__(self, socket_path: Path) -> None:
        from pylsp_jsonrpc.endpoint import Endpoint
        from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

        self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.conn.connect(str(socket_path))
        self.events: list[dict] = []
        writer = JsonRpcStreamWriter(self.conn.makefile("wb"))
        self.endpoint = Endpoint(
            {
                "event": self._on_event,
                "approval.request": self._on_approval,
                "user_prompt.ask": self._on_ask,
            },
            writer.write,
        )
        reader = JsonRpcStreamReader(self.conn.makefile("rb"))
        self._thread = threading.Thread(
            target=reader.listen, args=(self.endpoint.consume,), daemon=True
        )
        self._thread.start()

    def _on_event(self, params: dict) -> None:
        self.events.append(params or {})

    def _on_approval(self, params: dict) -> str:
        return "yes"

    def _on_ask(self, params: dict) -> str:
        return "answer"

    def request(self, method: str, params: dict | None = None, timeout: float = 10.0):  # type: ignore[no-untyped-def]
        return self.endpoint.request(method, params).result(timeout)

    def close(self) -> None:
        try:
            self.conn.close()
        except OSError:
            pass


async def the_tui_can_list_and_switch_agents(root: Path) -> tuple[bool, str]:
    """Real world + real socket IPC: agents lists primaries, select commits."""
    from ai_coding.server.handlers import make_handlers
    from ai_coding.server.service import IpcService

    world = await _world(root)
    try:
        loop = asyncio.get_running_loop()
        service = IpcService(root / "ipc.sock", loop)
        service.start()
        service.dispatcher.update(make_handlers(world, service))
        client = _IpcClient(root / "ipc.sock")
        try:
            if not service.wait_connected(timeout=5.0):
                return False, "ipc client never connected"
            hello = client.request("initialize")
            if not hello.get("sessionId"):
                return False, f"initialize gave no session: {hello!r}"
            listed = client.request("session.agents")
            rows = listed.get("agents", [])
            ids = [row.get("id") for row in rows]
            if "build" not in ids or "plan" not in ids:
                return False, f"primary list missing build/plan: {ids!r}"
            for row in rows:
                if row.get("kind") != "primary":
                    return False, f"non-primary in switch list: {row!r}"
                if set(row) != {"id", "name", "kind", "source", "description"}:
                    return False, f"row shape drifted: {sorted(row)!r}"
            if ids != sorted(ids):
                return False, f"list not sorted: {ids!r}"
            selected = client.request("session.agent.select", {"id": "plan"})
            if selected != {"ok": True, "status": "committed"}:
                return False, f"select did not commit: {selected!r}"
            stats = client.request("session.stats")
            if stats.get("agent") != "plan":
                return False, f"stats.agent is {stats.get('agent')!r}, want 'plan'"
            refused_sub = client.request("session.agent.select", {"id": "general"})
            if refused_sub != {"ok": False, "reason": "not_primary"}:
                return False, f"subagent id not refused: {refused_sub!r}"
            refused_unknown = client.request("session.agent.select", {"id": "nope"})
            if refused_unknown != {"ok": False, "reason": "unknown_agent"}:
                return False, f"unknown id not refused: {refused_unknown!r}"
            return True, f"agents={ids} select=committed stats.agent=plan"
        finally:
            client.close()
            service.close()
    finally:
        await world.aclose()


async def a_mid_turn_agent_is_queued_not_dropped(root: Path) -> tuple[bool, str]:
    """A busy loop queues the flip; the boundary commit lands it."""
    from ai_coding.domain.stance import PostureResult

    world = await _world(root)
    try:
        session = world.sessions.create()
        world.sessions.save(session)
        loop = world.loop
        if (session.agent, session.readonly) != ("build", False):
            return False, f"fresh session is {session.agent!r}/{session.readonly!r}"

        class _Busy:
            def locked(self) -> bool:
                return True

        original = loop._turn_lock
        loop._turn_lock = _Busy()  # type: ignore[attr-defined]
        try:
            result = loop.request_posture(
                session, agent="plan", readonly=True, reason="agent.select"
            )
        finally:
            loop._turn_lock = original  # type: ignore[attr-defined]
        if result is not PostureResult.QUEUED:
            return False, f"mid-turn request returned {result!r}, want QUEUED"
        if (session.agent, session.readonly) != ("build", False):
            return False, "live session moved mid-turn"
        if not session.pending_posture or session.pending_posture.get("agent") != "plan":
            return False, f"nothing queued: {session.pending_posture!r}"
        # Turn close drains the queue through the one write path.
        loop._apply_pending_posture(session)
        if (session.agent, session.readonly) != ("plan", True):
            return False, f"queued flip landed as {session.agent!r}/{session.readonly!r}"
        if session.pending_posture is not None:
            return False, "queue not cleared after commit"
        return True, "mid-turn QUEUED; boundary commit landed plan/readonly"
    finally:
        await world.aclose()


async def a_mid_turn_agent_switch_is_queued_not_dropped(root: Path) -> tuple[bool, str]:
    """Alias of the §7.6 spelling (brief omits 'switch_'); same proof."""
    return await a_mid_turn_agent_is_queued_not_dropped(root)


# ---- 9/10: child ownership + inherited deny -------------------------------


def _child_runner(root: Path, engine: Any, *, agents: Any = None) -> Any:
    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.core.subagent import SubAgentRunner
    from ai_coding.tools.builder import build_tool_registry

    app_cfg = AppConfig(
        models={}, default_model="", ai=AIConfig(stop_policy="natural", max_tool_iterations=4)
    )
    return SubAgentRunner(
        app_cfg,
        engine_factory=lambda: engine,
        build_tools=lambda wd: build_tool_registry(
            app_cfg, wd or str(root), None, include_subagent=False
        ),
        workspace=str(root),
        timeout_seconds=30.0,
        settings=AIConfig(stop_policy="natural", max_tool_iterations=4),
        agents=agents if agents is not None else builtin_catalog(),
    )


class _ScriptedEngine:
    """The model stand-in: replays tool calls, then stops.  Only the model."""

    def __init__(self, steps: list[Any]) -> None:
        self._steps = list(steps)

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        from ai_coding.domain.loop import StepResult

        if self._steps:
            return self._steps.pop(0)
        return StepResult(text="done", finish_reason="stop")

    async def aclose(self) -> None:
        return None


async def the_child_cannot_write_the_parent_task_list(
    root: Path,
) -> tuple[bool, str]:
    """The child writes its own TaskState; the parent's dict is byte-identical."""
    from ai_coding.domain.loop import StepResult

    parent_task = TaskState(goal="parent goal")
    parent_task.add("parent item", item_id="1")
    before = parent_task.to_dict()
    engine = _ScriptedEngine(
        [
            StepResult(
                tool_calls=[
                    ToolCallRef(
                        id="call-1",
                        name="todo_write",
                        arguments='{"action":"add","content":"child item"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    result = await _child_runner(root, engine).run(
        "child work", isolate=False, agent="general"
    )
    todos = [dict(t) for t in (result.todos or ())]
    contents = [t.get("content") for t in todos]
    if contents != ["child item"]:
        return False, f"child todos missing: {contents!r} status={result.status!r}"
    if parent_task.to_dict() != before:
        return False, "parent TaskState changed"
    return True, "child todos=['child item']; parent TaskState unchanged"


async def the_child_inherits_the_parent_deny(root: Path) -> tuple[bool, str]:
    """Parent session deny reaches the delegated write; the file never lands."""
    from ai_coding.domain.loop import StepResult
    from ai_coding.domain.subagent import ParentContext
    from ai_coding.security.permissions import Effect, Rule

    engine = _ScriptedEngine(
        [
            StepResult(
                tool_calls=[
                    ToolCallRef(
                        id="call-1",
                        name="write",
                        arguments='{"path":"blocked.txt","content":"x"}',
                    )
                ],
                finish_reason="tool_calls",
            ),
            StepResult(text="done", finish_reason="stop"),
        ]
    )
    parent_rules = (Rule(action="write", resource="*", effect=Effect.DENY),)
    result = await _child_runner(root, engine).run(
        "write it",
        isolate=False,
        agent="general",
        parent=ParentContext(permissions=parent_rules),
    )
    target = root / "blocked.txt"
    if target.exists():
        return False, f"parent deny bypassed: {target.read_text()!r}"
    return True, f"write blocked under parent deny (child status={result.status})"


CASES: dict[str, CaseFn] = {
    "a_swapped_symlink_cannot_write_outside_the_plan_directory": (
        a_swapped_symlink_cannot_write_outside_the_plan_directory
    ),
    "a_workspace_grep_does_not_return_a_dotenv": (
        a_workspace_grep_does_not_return_a_dotenv
    ),
    "a_workspace_grep_does_not_return_any_credential": (
        a_workspace_grep_does_not_return_any_credential
    ),
    "a_credential_symlink_is_not_exposed_by_grep": (
        a_credential_symlink_is_not_exposed_by_grep
    ),
    "an_approved_credential_grep_still_returns_it": (
        an_approved_credential_grep_still_returns_it
    ),
    "a_python_one_liner_cannot_read_outside": a_python_one_liner_cannot_read_outside,
    "the_tui_can_list_and_switch_agents": the_tui_can_list_and_switch_agents,
    "a_mid_turn_agent_is_queued_not_dropped": a_mid_turn_agent_is_queued_not_dropped,
    # §7.6 spells the same case with 'switch_'; accept either name.
    "a_mid_turn_agent_switch_is_queued_not_dropped": (
        a_mid_turn_agent_switch_is_queued_not_dropped
    ),
    "the_child_cannot_write_the_parent_task_list": (
        the_child_cannot_write_the_parent_task_list
    ),
    "the_child_inherits_the_parent_deny": the_child_inherits_the_parent_deny,
}


#: One-line breakages, applied at runtime.  A case that still passes after one
#: of these is a case that would not have caught the defect it claims to.
#: M4 never reuses an m3 mutant: these cover the fd swap, the credential
#: symlink, the approval source, and the agent-switch RPC.
MUTATIONS: dict[str, Callable[[], None]] = {}


def _mutate_fd_check_away() -> None:
    """Follow the swapped symlink: the plan-write case must fail."""
    import ai_coding.security.sandbox as sandbox_module
    import ai_coding.tools.write_tool as write_module

    def fake_open_inside(root: Path, path: str | Path, *, mode: str, create: bool = False):  # type: ignore[no-untyped-def]
        target = Path(root) / str(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(target), os.O_RDWR | os.O_CREAT, 0o644)
        return sandbox_module.OpenedFile(
            fd=fd, relative_path=str(path), path=target
        )

    sandbox_module.open_inside = fake_open_inside  # type: ignore[attr-defined]
    write_module.open_inside = fake_open_inside  # type: ignore[attr-defined]


def _mutate_rg_excludes_away() -> None:
    """Drop the ripgrep credential filter: the dotenv tree case must fail."""
    import ai_coding.tools.grep_tool as grep_module

    original = grep_module.GrepTool._grep_with_rg

    async def without_rg_filter(  # type: ignore[no-untyped-def]
        self, target, pattern, *, include, literal, case_sensitive, limit,
        skip_credentials,
    ):
        # Tree search without either layer: no exclude globs, no alias
        # post-filter.  The Python fallback keeps its own filter, so only
        # the rg case (dotenv) is killed.
        return await original(
            self, target, pattern, include=include, literal=literal,
            case_sensitive=case_sensitive, limit=limit, skip_credentials=False,
        )

    grep_module.GrepTool._grep_with_rg = without_rg_filter  # type: ignore[attr-defined]


def _mutate_python_vocabulary_away() -> None:
    """Shrink the vocabulary to dotenv only: .npmrc/id_rsa must leak."""
    import ai_coding.security.credentials as credentials_module
    import ai_coding.tools.grep_tool as grep_module

    def dotenv_only(path: Any, *, workspace: Any = None) -> bool:
        text = str(path)
        base = text.rsplit("/", 1)[-1]
        if base.endswith(".env.example"):
            return False
        return base == ".env" or base.endswith(".env") or base.startswith(".env.")

    credentials_module.is_credential_path = dotenv_only  # type: ignore[attr-defined]
    grep_module.is_credential_path = dotenv_only  # type: ignore[attr-defined]


def _mutate_symlink_resolve_away() -> None:
    """Judge grep hits by visible name only: config.txt->.env must leak."""
    import ai_coding.tools.grep_tool as grep_module

    def lexical_only(self: Any, shown: Any, path: Any) -> bool:
        from ai_coding.security.credentials import is_credential_path

        return bool(is_credential_path(str(shown)))

    grep_module.GrepTool._is_credential_entry = lexical_only  # type: ignore[attr-defined]


def _mutate_approval_source_away() -> None:
    """A human YES never opens the credential: the approved-grep case must fail."""
    import ai_coding.core.dispatch as dispatch_module

    dispatch_module.credential_match = lambda *a, **k: ""  # type: ignore[attr-defined]


def _mutate_string_literal_scan_away() -> None:
    """Blind the literal scan: python -c open('../x') must be missed."""
    import ai_coding.core.acceptance as acceptance_module

    acceptance_module._path_fragments = lambda text: []  # type: ignore[attr-defined]


def _mutate_agent_list_away() -> None:
    """Hide every primary: the TUI list+switch case must fail."""
    import ai_coding.domain.agents as agents_module

    agents_module.AgentCatalog.visible = lambda self: ()  # type: ignore[attr-defined]


def _mutate_queue_away() -> None:
    """Commit even mid-turn: the queued case must fail."""
    import ai_coding.core.agent_loop as loop_module

    def always_commit(self: Any, session: Any, **kwargs: Any) -> Any:
        from ai_coding.domain.stance import PostureResult

        self.commit_posture(
            session,
            agent=kwargs.get("agent", session.agent),
            readonly=kwargs.get("readonly", session.readonly),
            reason=kwargs.get("reason", ""),
            observer=kwargs.get("observer"),
        )
        return PostureResult.COMMITTED

    loop_module.AgentLoop.request_posture = always_commit  # type: ignore[attr-defined]


def _mutate_todo_isolation_away() -> None:
    """The child can never write its list: the ownership case must fail."""
    import ai_coding.tools.todo_write_tool as todo_module
    from ai_coding.domain.message import ToolResult

    async def always_fail(self: Any, params: Any, ctx: Any = None) -> ToolResult:
        return ToolResult(success=False, output="todo unavailable")

    todo_module.TodoWriteTool.execute = always_fail  # type: ignore[attr-defined]


def _mutate_parent_deny_away() -> None:
    """Drop the parent seed: the inherited-deny case must fail."""
    import ai_coding.core.subagent as subagent_module

    original = subagent_module.SubAgentRunner.run

    async def without_parent(self: Any, task: str, name: str = "sub-agent", **kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
        kwargs["parent"] = None
        return await original(self, task, name, **kwargs)

    subagent_module.SubAgentRunner.run = without_parent  # type: ignore[attr-defined]


MUTATIONS.update(
    {
        "fd_check": _mutate_fd_check_away,
        "rg_excludes": _mutate_rg_excludes_away,
        "python_vocabulary": _mutate_python_vocabulary_away,
        "symlink_resolve": _mutate_symlink_resolve_away,
        "approval_source": _mutate_approval_source_away,
        "string_literal": _mutate_string_literal_scan_away,
        "agent_list": _mutate_agent_list_away,
        "queue": _mutate_queue_away,
        "todo_isolation": _mutate_todo_isolation_away,
        "parent_deny": _mutate_parent_deny_away,
    }
)

#: Which mutation must kill which case (one-to-one, no m3 reuse).
MUTATION_TARGETS: dict[str, str] = {
    "fd_check": "a_swapped_symlink_cannot_write_outside_the_plan_directory",
    "rg_excludes": "a_workspace_grep_does_not_return_a_dotenv",
    "python_vocabulary": "a_workspace_grep_does_not_return_any_credential",
    "symlink_resolve": "a_credential_symlink_is_not_exposed_by_grep",
    "approval_source": "an_approved_credential_grep_still_returns_it",
    "string_literal": "a_python_one_liner_cannot_read_outside",
    "agent_list": "the_tui_can_list_and_switch_agents",
    "queue": "a_mid_turn_agent_is_queued_not_dropped",
    "todo_isolation": "the_child_cannot_write_the_parent_task_list",
    "parent_deny": "the_child_inherits_the_parent_deny",
}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    parser.add_argument(
        "--mutate",
        choices=("none", *MUTATIONS),
        default="none",
        help="break one decision on purpose; the target case must fail",
    )
    args = parser.parse_args(argv)
    if args.list:
        for name in CASES:
            print(name)
        return 0
    cases = dict(CASES)
    if args.only:
        cases = {name: fn for name, fn in cases.items() if name == args.only}
        if not cases:
            print(f"no such case: {args.only}")
            return 2
    if args.mutate != "none":
        MUTATIONS[args.mutate]()
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
    print("-" * 72)
    print(f"SUMMARY {passed}/{passed + failed} passed ({len(cases)} cases)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
