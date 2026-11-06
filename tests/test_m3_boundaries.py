"""M3 boundaries (T5/T8/T9): the plan directory, the todo table, and dotenv.

Each test here pins one rule of the plan posture.  They are deliberately
written against the real units (the rule language, the gate, the tool
surface), because M3's whole point is that those three stop disagreeing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.task import TaskState
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.permissions import (
    Effect,
    Rule,
    rule_matches,
    subjects_of,
)
from ai_coding.tools.base import ToolContext

# ---- T5: a scoped allow cannot be spelled around ---------------------------


def test_a_parent_segment_cannot_escape_a_scoped_allow():
    """``a/../b`` is ``b``; a rule matching the text is not a boundary.

    Without lexical normalization ``*.ai-coding/plan/*`` matches
    ``.ai-coding/plan/../../src/app.py`` (``*`` crosses slashes), so the plan
    directory exception would allow every file in the workspace.
    """

    assert subjects_of(
        "write", {"path": ".ai-coding/plan/../../src/app.py"}
    ) == ("src/app.py",)
    rule = Rule("edit", ".ai-coding/plan/*", Effect.ALLOW)
    assert not rule_matches("write", "src/app.py", rule, posture="plan")


def test_a_clean_plan_path_still_matches():
    rule = Rule("edit", ".ai-coding/plan/*", Effect.ALLOW)
    assert rule_matches(
        "write", ".ai-coding/plan/x.md", rule, posture="plan"
    )


def test_a_leading_dot_slash_is_still_stripped():
    assert subjects_of("read", {"path": "./src/app.py"}) == ("src/app.py",)


# ---- T5: the plan agent's rules -------------------------------------------


def _plan_rules():
    from ai_coding.agents.registry import builtin_catalog

    return builtin_catalog().rules("plan")


def test_the_plan_agent_may_write_the_plan_directory():
    from ai_coding.security.permission_gate import (
        PermissionDecision,
        PermissionGate,
    )

    decision = PermissionGate().check(
        "write",
        {"path": ".ai-coding/plan/design.md"},
        agent_rules=_plan_rules(),
        posture="plan",
    )
    assert decision.decision is PermissionDecision.ALLOW


def test_the_plan_agent_may_not_write_a_workspace_file():
    from ai_coding.security.permission_gate import (
        PermissionDecision,
        PermissionGate,
    )

    decision = PermissionGate().check(
        "write",
        {"path": "src/app.py"},
        agent_rules=_plan_rules(),
        posture="plan",
    )
    assert decision.decision is PermissionDecision.DENY


def test_the_plan_agent_may_not_write_outside_the_workspace_either():
    """The plan directory exception is a place, not a way around the sandbox."""
    from ai_coding.security.permission_gate import (
        PermissionDecision,
        PermissionGate,
    )

    decision = PermissionGate().check(
        "write",
        {"path": "/tmp/elsewhere/x.md"},
        agent_rules=_plan_rules(),
        posture="plan",
    )
    assert decision.decision is PermissionDecision.DENY


def test_write_and_edit_stay_visible_under_the_plan_rules():
    """A scoped allow keeps the tool in the catalogue (opencode D06)."""
    from ai_coding.security.permission_gate import PermissionGate

    gate = PermissionGate()
    rules = _plan_rules()
    for name in ("write", "edit"):
        assert not gate.hides(name, agent_rules=rules, posture="plan"), name


def test_the_plan_rule_is_specifically_allowed_for_the_plan_directory():
    """The overlay needs to tell a scoped allow from the base ``*`` allow."""
    from ai_coding.security.permission_gate import PermissionGate

    gate = PermissionGate()
    rules = _plan_rules()
    in_dir = gate.check(
        "write",
        {"path": ".ai-coding/plan/x.md"},
        agent_rules=rules,
        posture="plan",
    )
    outside = gate.check(
        "read", {"path": "src/app.py"}, agent_rules=rules, posture="plan"
    )
    assert in_dir.specific is True
    assert outside.specific is False


def test_a_scoped_allow_makes_the_action_partially_allowed():
    from ai_coding.security.permission_gate import PermissionGate

    gate = PermissionGate()
    rules = _plan_rules()
    assert gate.partially_allowed("write", agent_rules=rules, posture="plan")
    # ``job_kill`` is mutating and has no scoped allow: it must stay pruned.
    assert not gate.partially_allowed("job_kill", agent_rules=rules, posture="plan")


# ---- T5: the real writer, through the real dispatcher --------------------


def test_the_plan_directory_is_reachable_through_the_dispatcher(tmp_path):
    import asyncio

    from ai_coding.domain.message import ToolCallRef
    from ai_coding.tools.base import ToolContext

    target = tmp_path / ".ai-coding" / "plan" / "design.md"
    dispatcher = ToolDispatcher(
        _registry_for(tmp_path), PermissionGate()
    )
    ctx = ToolContext(
        task=TaskState(),
        workspace=str(tmp_path),
        session_id="s",
        mode="plan",
        readonly=True,
        agent_rules=_plan_rules(),
    )
    outcome = asyncio.run(
        dispatcher.dispatch_batch(
            [
                ToolCallRef(
                    id="c1",
                    name="write",
                    arguments=(
                        '{"path": ".ai-coding/plan/design.md",'
                        ' "content": "# plan"}'
                    ),
                )
            ],
            ctx,
        )
    )[0]
    assert outcome.result.success is True, outcome.result.output
    assert target.read_text(encoding="utf-8") == "# plan"


def test_a_workspace_file_is_refused_and_the_reason_names_the_plan_directory(
    tmp_path,
):
    import asyncio

    from ai_coding.domain.message import ToolCallRef
    from ai_coding.tools.base import ToolContext

    dispatcher = ToolDispatcher(_registry_for(tmp_path), PermissionGate())
    ctx = ToolContext(
        task=TaskState(),
        workspace=str(tmp_path),
        session_id="s",
        mode="plan",
        readonly=True,
        agent_rules=_plan_rules(),
    )
    outcome = asyncio.run(
        dispatcher.dispatch_batch(
            [
                ToolCallRef(
                    id="c1",
                    name="write",
                    arguments='{"path": "src/app.py", "content": "x"}',
                )
            ],
            ctx,
        )
    )[0]
    assert outcome.denied is True
    assert outcome.result.detail == "denied"
    assert ".ai-coding/plan" in outcome.result.output
    assert not (tmp_path / "src" / "app.py").exists()


def _registry_for(workspace):
    from ai_coding.domain.agents import PLAN_DIR
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.write_tool import WriteTool

    registry = ToolRegistry()
    registry.register(
        WriteTool(
            Sandbox(workspace),
            FileStateTracker(),
            plan_root=Path(workspace) / PLAN_DIR,
        )
    )
    return registry


def _plan_ctx(workspace):
    return ToolContext(
        task=TaskState(),
        workspace=str(workspace),
        session_id="s",
        mode="plan",
        readonly=True,
        agent_rules=_plan_rules(),
    )


def test_the_enter_reminder_carries_the_plan_directory():
    """The reminder is the per-switch statement; it must not under-state writes."""
    from ai_coding.core.plan_reminders import PLAN_ENTER_REMINDER

    assert ".ai-coding/plan/" in PLAN_ENTER_REMINDER


# ---- Review findings: the boundary is a place, not a spelling -------------


@pytest.mark.asyncio
async def test_a_symlink_out_of_the_plan_directory_is_refused(tmp_path):
    """Lexical rules cannot see a symlink; the write must not follow one out."""
    import os

    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.registry import ToolRegistry
    from ai_coding.tools.write_tool import WriteTool

    (tmp_path / "src").mkdir()
    (tmp_path / ".ai-coding" / "plan").mkdir(parents=True)
    os.symlink(tmp_path / "src", tmp_path / ".ai-coding" / "plan" / "escape")

    registry = ToolRegistry()
    registry.register(WriteTool(Sandbox(tmp_path), FileStateTracker()))
    outcome = (
        await ToolDispatcher(
            registry, PermissionGate(workspace=str(tmp_path))
        ).dispatch_batch(
            [
                ToolCallRef(
                    id="c",
                    name="write",
                    arguments='{"path": ".ai-coding/plan/escape/pwn.txt",'
                    ' "content": "PWN"}',
                )
            ],
            _plan_ctx(tmp_path),
        )
    )[0]
    assert outcome.result.success is False
    assert not (tmp_path / "src" / "pwn.txt").exists()


@pytest.mark.asyncio
async def test_a_symlink_to_a_dotenv_is_not_an_example(tmp_path):
    """The policy names the file that is read, not the spelling that reached it."""
    import os

    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.security.sandbox import Sandbox
    from ai_coding.tools.read_tool import ReadTool
    from ai_coding.tools.registry import ToolRegistry

    (tmp_path / ".env").write_text("API_KEY=super-secret\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    os.symlink(tmp_path / ".env", tmp_path / "docs" / "config.txt")

    registry = ToolRegistry()
    registry.register(ReadTool(Sandbox(tmp_path), FileStateTracker()))
    outcome = (
        await ToolDispatcher(
            registry, PermissionGate(workspace=str(tmp_path))
        ).dispatch_batch(
            [ToolCallRef(id="c", name="read", arguments='{"path":"docs/config.txt"}')],
            ToolContext(
                task=TaskState(), workspace=str(tmp_path), session_id="s"
            ),
        )
    )[0]
    # Without an approval callback a WARN is a refusal; the point is that the
    # call is a question at all, not that the file happened to be unreadable.
    assert outcome.result.detail == "not_approved", outcome.result.output


def test_grep_over_a_dotenv_is_not_a_free_read(tmp_path):
    """grep reads file contents; the dotenv question must cover it too."""

    (tmp_path / ".env").write_text("API_KEY=super-secret\n", encoding="utf-8")
    decision = PermissionGate().check(
        "grep", {"pattern": ".*", "path": ".env"}
    )
    assert decision.decision is not PermissionDecision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "cat ../secret.txt",
        "true && cat ../secret.txt",
        "true && cd .. && cat secret.txt",
        "cat ../secret.txt > ../copied.txt",
    ],
)
def test_a_proof_cannot_leave_the_workspace_by_a_relative_path(
    tmp_path, command
):
    from ai_coding.core.acceptance import _needs_a_human

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    assert _needs_a_human(command, str(workspace))


# ---- T8: the real request surface ----------------------------------------


def _surface(mode, readonly, rules):
    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.tools.builder import build_tool_registry

    class _Loop:
        pass

    loop = _Loop()
    loop.tools = build_tool_registry(
        AppConfig(models={}, default_model="", ai=AIConfig()), ".", None
    )
    loop.gate = PermissionGate()
    return [
        tool.name
        for tool in __import__(
            "ai_coding.core.agent_loop", fromlist=["AgentLoop"]
        ).AgentLoop._surface_tools(
            loop, mode, readonly, (), tuple(rules)
        )
    ]


def test_the_plan_request_carries_no_todo_tool():
    from ai_coding.agents.registry import builtin_catalog

    names = _surface("plan", True, builtin_catalog().rules("plan"))
    assert "todo_write" not in names


def test_the_plan_request_still_carries_the_two_writers():
    from ai_coding.agents.registry import builtin_catalog

    names = _surface("plan", True, builtin_catalog().rules("plan"))
    assert "write" in names
    assert "edit" in names


def test_a_plain_readonly_session_still_carries_no_writer():
    """``/readonly`` is the human's absolute switch; M3 must not widen it."""
    names = _surface("normal", True, ())
    assert "write" not in names
    assert "edit" not in names
    assert "job_kill" not in names


def test_the_build_request_still_carries_todo_write():
    names = _surface("normal", False, ())
    assert "todo_write" in names


# ---- T9: a dotenv read is a question -------------------------------------


def _decision(path: str, *, agent=None):
    from ai_coding.security.permission_gate import PermissionGate

    gate = PermissionGate()
    rules = ()
    if agent is not None:
        from ai_coding.agents.registry import builtin_catalog

        rules = builtin_catalog().rules(agent)
    return gate.check("read", {"path": path}, agent_rules=rules)


def test_reading_a_dotenv_file_requires_approval():
    from ai_coding.security.permission_gate import PermissionDecision

    for path in (".env", "a/.env", "config/.env"):
        decision = _decision(path)
        assert decision.decision is PermissionDecision.WARN, path


def test_reading_a_dotenv_variant_requires_approval():
    from ai_coding.security.permission_gate import PermissionDecision

    for path in (".env.local", "a/.env.production", "config/.env.test"):
        decision = _decision(path)
        assert decision.decision is PermissionDecision.WARN, path


def test_the_example_file_is_allowed():
    from ai_coding.security.permission_gate import PermissionDecision

    for path in (".env.example", "a/.env.example"):
        decision = _decision(path)
        assert decision.decision is PermissionDecision.ALLOW, path


def test_ordinary_files_are_unaffected():
    from ai_coding.security.permission_gate import PermissionDecision

    for path in ("src/app.py", "README.md", "environment.py"):
        decision = _decision(path)
        assert decision.decision is PermissionDecision.ALLOW, path


def test_the_explore_agent_still_asks_for_a_dotenv():
    """The agent layer's ``read * allow`` must not erase the base's question."""
    from ai_coding.security.permission_gate import PermissionDecision

    assert (
        _decision(".env", agent="explore").decision is PermissionDecision.WARN
    )
    assert (
        _decision("src/app.py", agent="explore").decision
        is PermissionDecision.ALLOW
    )


def test_the_plan_agent_still_asks_for_a_dotenv():
    from ai_coding.security.permission_gate import PermissionDecision

    assert _decision(".env", agent="plan").decision is PermissionDecision.WARN


def test_a_project_deny_still_beats_the_dotenv_ask():
    from ai_coding.security.permission_gate import (
        PermissionDecision,
        PermissionGate,
    )
    from ai_coding.security.permissions import FileRules

    gate = PermissionGate(
        rules=FileRules(project=(Rule("read", "*.env", Effect.DENY),))
    )
    assert (
        gate.check("read", {"path": ".env"}).decision
        is PermissionDecision.DENY
    )
