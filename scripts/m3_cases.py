#!/usr/bin/env python3
"""M3 acceptance cases — the plan's boundaries, against the real world.

Four claims, each with a shape that can be broken:

* **T5**: the plan directory is the only writable place, and a proof naming
  a place outside the tree never reaches the review card;
* **T8**: the plan request carries no ``todo_write``, while build keeps it;
* **T9**: reading a dotenv file asks, and the example file does not;
* **T11**: a submitted plan survives reload because the tool call is durable.

The cases drive a real ``build_world``, real session files and a real
filesystem.  The only fakes are the model and the network.

    .venv/bin/python scripts/m3_cases.py
    .venv/bin/python scripts/m3_cases.py --only the_plan_directory_is_the_only_writable_place
    .venv/bin/python scripts/m3_cases.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.agents.registry import builtin_catalog  # noqa: E402
from ai_coding.core.dispatch import ToolDispatcher  # noqa: E402
from ai_coding.domain.message import ToolCallRef  # noqa: E402
from ai_coding.domain.task import TaskState  # noqa: E402
from ai_coding.security.file_state_tracker import FileStateTracker  # noqa: E402
from ai_coding.security.permission_gate import (  # noqa: E402
    PermissionDecision,
    PermissionGate,
)
from ai_coding.security.sandbox import Sandbox  # noqa: E402
from ai_coding.tools.base import ToolContext  # noqa: E402
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


def _registry(root: Path, gate: PermissionGate) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(WriteTool(Sandbox(root), FileStateTracker()))
    return registry


# ---- T5: the plan directory ----------------------------------------------


async def the_plan_directory_is_the_only_writable_place(
    root: Path,
) -> tuple[bool, str]:
    """The real tool writes one path and refuses the other."""
    gate = PermissionGate()
    dispatcher = ToolDispatcher(_registry(root, gate), gate)
    ctx = _plan_ctx(root)
    outcomes = await dispatcher.dispatch_batch(
        [
            ToolCallRef(
                id="plan",
                name="write",
                arguments=json.dumps(
                    {"path": ".ai-coding/plan/design.md", "content": "# plan"}
                ),
            ),
            ToolCallRef(
                id="src",
                name="write",
                arguments=json.dumps({"path": "src/app.py", "content": "x"}),
            ),
        ],
        ctx,
    )
    plan_out, src_out = outcomes
    target = root / ".ai-coding" / "plan" / "design.md"
    if not target.is_file() or target.read_text(encoding="utf-8") != "# plan":
        return False, f"the plan file was not written: {plan_out.result.output!r}"
    if src_out.result.success or (root / "src" / "app.py").exists():
        return False, "a workspace file was written"
    if ".ai-coding/plan" not in src_out.result.output:
        return False, f"the refusal did not name the plan directory: {src_out.result.output!r}"
    return True, "plan written, workspace refused"


async def an_outside_verify_never_reaches_the_card(root: Path) -> tuple[bool, str]:
    """A proof the shell will run elsewhere is refused before the human sees it."""
    from ai_coding.security.user_prompt import UserAnswer
    from ai_coding.tools.present_plan_tool import PresentPlanTool

    class _Prompt:
        def __init__(self) -> None:
            self.calls = 0

        async def ask(self, *a: Any, **k: Any) -> UserAnswer:
            self.calls += 1
            return UserAnswer(answered=True, text='{"choice":"approve"}')

    prompt = _Prompt()
    ctx = _plan_ctx(root)
    result = await PresentPlanTool(prompt).execute(
        {
            "plan": "# p",
            "items": [{"content": "read a secret", "verify": "cat /etc/passwd"}],
        },
        ctx,
    )
    if result.success:
        return False, "the plan was approved"
    if prompt.calls:
        return False, "the human was asked"
    if ctx.task.armed:
        return False, "the task was armed"
    return True, "refused before review"


# ---- T8: the surface ------------------------------------------------------


async def the_plan_request_carries_no_todo_tool(root: Path) -> tuple[bool, str]:
    """The real loop surfaces: plan has no todo, build does."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        session.agent = "plan"
        session.readonly = True
        plan_names = [t.name for t in world.loop._surface_tools(
            "plan", True, (), world.loop._agent_rules(session)
        )]
        build_names = [t.name for t in world.loop._surface_tools("normal", False)]
        if "todo_write" in plan_names:
            return False, "the plan request carried todo_write"
        if "todo_write" not in build_names:
            return False, "the build request lost todo_write"
        if "write" not in plan_names or "edit" not in plan_names:
            return False, f"the plan request lost its writers: {plan_names}"
        return True, f"plan carries {len(plan_names)} tools, no todo"
    finally:
        await world.aclose()


# ---- T9: dotenv -----------------------------------------------------------


async def a_dotenv_read_asks_and_an_example_does_not(root: Path) -> tuple[bool, str]:
    """The real gate: the secret asks, the documentation does not."""
    gate = PermissionGate()
    rules = builtin_catalog().rules("plan")
    secret = gate.check("read", {"path": ".env"}, agent_rules=rules)
    nested = gate.check("read", {"path": "config/.env.local"}, agent_rules=rules)
    example = gate.check("read", {"path": ".env.example"}, agent_rules=rules)
    ordinary = gate.check("read", {"path": "src/app.py"}, agent_rules=rules)
    if secret.decision is not PermissionDecision.WARN:
        return False, f"env was {secret.decision}"
    if nested.decision is not PermissionDecision.WARN:
        return False, f"nested variant was {nested.decision}"
    if example.decision is not PermissionDecision.ALLOW:
        return False, f"example was {example.decision}"
    if ordinary.decision is not PermissionDecision.ALLOW:
        return False, f"ordinary file was {ordinary.decision}"
    return True, "secret asks, documentation and code do not"


# ---- T11: the plan survives reload ---------------------------------------


async def a_submitted_plan_can_be_read_back_after_reload(
    root: Path,
) -> tuple[bool, str]:
    """A real session file, reloaded, still holds the plan it was given."""
    world = await _world(root)
    try:
        session = world.sessions.create()
        session.add_message(
            _assistant_plan_call(
                "# 计划\n正文",
                [{"content": "建目录", "verify": "test -d hotel"}],
            )
        )
        world.sessions.save(session)
        reloaded = world.sessions.load(session.session_id)
        if reloaded is None:
            return False, "the session did not reload"
        calls = [
            call
            for message in reloaded.messages
            for call in (message.tool_calls or ())
            if call.name == "present_plan"
        ]
        if len(calls) != 1:
            return False, f"expected one plan call, found {len(calls)}"
        arguments = json.loads(calls[0].arguments)
        if arguments["plan"] != "# 计划\n正文":
            return False, f"the plan changed: {arguments['plan']!r}"
        if arguments["items"][0]["verify"] != "test -d hotel":
            return False, "the proof changed"
        return True, "the submitted plan survived reload"
    finally:
        await world.aclose()


async def a_symlink_out_of_the_plan_directory_is_refused(
    root: Path,
) -> tuple[bool, str]:
    """The plan rule is a place, not a spelling: a symlink must not escape."""
    gate = PermissionGate(workspace=str(root))
    (root / "src").mkdir(exist_ok=True)
    plan_dir = root / ".ai-coding" / "plan"
    plan_dir.mkdir(parents=True, exist_ok=True)
    link = plan_dir / "escape"
    if not link.exists():
        link.symlink_to(root / "src")
    decision = gate.check(
        "write",
        {"path": ".ai-coding/plan/escape/pwn.txt"},
        agent_rules=builtin_catalog().rules("plan"),
        posture="plan",
    )
    if decision.decision is not PermissionDecision.DENY:
        return False, f"the symlinked write was {decision.decision.value}"
    return True, "the resolved target is outside the plan directory"


async def grep_over_a_dotenv_is_a_question(root: Path) -> tuple[bool, str]:
    gate = PermissionGate(workspace=str(root))
    decision = gate.check("grep", {"pattern": ".*", "path": ".env"})
    example = gate.check("grep", {"pattern": ".*", "path": ".env.example"})
    if decision.decision is not PermissionDecision.WARN:
        return False, f"grep .env was {decision.decision.value}"
    if example.decision is not PermissionDecision.ALLOW:
        return False, f"grep example was {example.decision.value}"
    return True, "grep asks for the secret, not for the example"


async def a_proof_cannot_use_a_relative_escape(root: Path) -> tuple[bool, str]:
    from ai_coding.core.acceptance import _needs_a_human

    ws = root / "ws"
    ws.mkdir()
    (root / "secret.txt").write_text("secret", encoding="utf-8")
    for command in (
        "cat ../secret.txt",
        "true && cd .. && cat secret.txt",
        "cat ../secret.txt > ../copied.txt",
    ):
        if not _needs_a_human(command, str(ws)):
            return False, f"accepted: {command}"
    return True, "relative escapes are refused"


def _assistant_plan_call(plan: str, items: list[dict[str, Any]]) -> Any:
    from ai_coding.domain.message import ChatMessage, ToolCallRef

    return ChatMessage(
        role="assistant",
        content=None,
        timestamp="t",
        tool_calls=[
            ToolCallRef(
                id="call_plan",
                name="present_plan",
                arguments=json.dumps({"plan": plan, "items": items}),
            )
        ],
    )


CASES: dict[str, CaseFn] = {
    "the_plan_directory_is_the_only_writable_place": (
        the_plan_directory_is_the_only_writable_place
    ),
    "an_outside_verify_never_reaches_the_card": an_outside_verify_never_reaches_the_card,
    "the_plan_request_carries_no_todo_tool": the_plan_request_carries_no_todo_tool,
    "a_dotenv_read_asks_and_an_example_does_not": (
        a_dotenv_read_asks_and_an_example_does_not
    ),
    "a_submitted_plan_can_be_read_back_after_reload": (
        a_submitted_plan_can_be_read_back_after_reload
    ),
    "a_symlink_out_of_the_plan_directory_is_refused": (
        a_symlink_out_of_the_plan_directory_is_refused
    ),
    "grep_over_a_dotenv_is_a_question": grep_over_a_dotenv_is_a_question,
    "a_proof_cannot_use_a_relative_escape": a_proof_cannot_use_a_relative_escape,
}


#: One-line breakages, applied at runtime.  A case that still passes after one
#: of these is a case that would not have caught the defect it claims to.
MUTATIONS: dict[str, Callable[[], None]] = {}


def _mutate_plan_allow_away() -> None:
    """Drop the plan-directory exception: the first case must fail."""
    from ai_coding.agents import registry

    original = registry.builtin_catalog

    def patched(*, strict_readonly: bool = False):
        catalog = original(strict_readonly=strict_readonly)
        plan = catalog.specs["plan"]
        catalog.specs["plan"] = type(plan)(
            id=plan.id,
            name=plan.name,
            description=plan.description,
            kind=plan.kind,
            hidden=plan.hidden,
            body=plan.body,
            permissions=tuple(
                row
                for row in plan.permissions
                if row.get("resource") != ".ai-coding/plan/*"
            ),
            source=plan.source,
            path=plan.path,
        )
        return catalog

    registry.builtin_catalog = patched


def _mutate_verify_gate_away() -> None:
    """Drop the proof check: the outside-verify case must fail."""
    from ai_coding.tools import present_plan_tool

    original = present_plan_tool.outside_workspace_paths

    def prose_only(texts, workspace, *, extra_roots=(), from_shell=False):
        if from_shell:
            return []
        return original(texts, workspace, extra_roots=extra_roots, from_shell=False)

    present_plan_tool.outside_workspace_paths = prose_only


def _mutate_todo_rule_away() -> None:
    """Put todo_write back on the plan surface: the surface case must fail."""
    from ai_coding.agents import registry

    registry._PLAN_SOFT_DENY = tuple(
        name for name in registry._PLAN_SOFT_DENY if name != "todo_write"
    )


def _mutate_dotenv_rule_away() -> None:
    """Remove the dotenv question: the dotenv case must fail."""
    from ai_coding.security import permission_gate

    permission_gate.PermissionGate._base_rules = (
        lambda self: tuple(
            [
                permission_gate.Rule(
                    action=name, resource="*", effect=permission_gate.Effect.ALLOW
                )
                for name in sorted(self._allow_tools)
            ]
            + [
                permission_gate.Rule(
                    action=name, resource="*", effect=permission_gate.Effect.ASK
                )
                for name in sorted(self._warn_tools)
            ]
        )
    )


def _mutate_plan_call_loss() -> None:
    """Lose the call when writing the session: the reload case must fail.

    The defect this stands in for is a serializer that drops ``toolCalls``
    (the field the plan's only durable home lives in).
    """
    from ai_coding.domain import session as session_module

    original = session_module._message_to_dict

    def stripped(message):
        data = original(message)
        data.pop("toolCalls", None)
        return data

    session_module._message_to_dict = stripped


MUTATIONS.update(
    {
        "plan_allow": _mutate_plan_allow_away,
        "verify_gate": _mutate_verify_gate_away,
        "todo_rule": _mutate_todo_rule_away,
        "dotenv_rule": _mutate_dotenv_rule_away,
        "plan_call": _mutate_plan_call_loss,
    }
)




async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="run one case by name")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    parser.add_argument(
        "--mutate",
        choices=("none", *MUTATIONS),
        default="none",
        help="break one decision on purpose; the run must fail",
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
