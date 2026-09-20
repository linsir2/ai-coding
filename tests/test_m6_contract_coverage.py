"""M6-1 contract coverage — fill the offline-covered gaps surfaced by the
coverage sweep (fail-closed approval paths, decision equality, tool error
branches, console rendering, abstract contracts).  All offline.

The network boundary (``llm_helpers``) and the interactive ``repl`` loop are
explicitly out of scope: the former is documented in ``docs/integration_testing.md``
and the latter's decision core is already covered by ``test_repl``.
"""

import pytest

from ai_coding.domain.message import ToolResult
from ai_coding.domain.subagent import SubagentResult, SubagentTurn
from ai_coding.security.file_state_tracker import FileState
from ai_coding.security.permission_gate import Decision, PermissionDecision, PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.base import BaseTool

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- abstract bases

def test_ai_service_abstract_execute_turn_raises():
    from ai_coding.ai.base import AIService

    assert "execute_turn" in AIService.__abstractmethods__


def test_base_tool_abstract_execute_raises():
    from ai_coding.tools.base import BaseTool

    assert "execute" in BaseTool.__abstractmethods__

    class _Noop(BaseTool):
        @property
        def name(self) -> str:
            return "noop"

        @property
        def description(self) -> str:
            return "noop"

        @property
        def params_json_schema(self) -> dict:
            return {}

    with pytest.raises(TypeError):
        _Noop()  # execute abstract -> cannot instantiate


def test_subagent_turn_has_tool_calls_detection():
    assert SubagentTurn().has_tool_calls is False
    assert SubagentTurn(text="x", tool_calls=[]).has_tool_calls is False
    assert SubagentResult(text="done").text == "done"


# ---------------------------------------------------------------- approval (fail-closed)

async def test_approval_callback_abstract_raises():
    from ai_coding.security.approval import ApprovalCallback

    with pytest.raises(NotImplementedError):
        await ApprovalCallback().approve("read", {}, "")


async def test_cli_approval_eof_fails_closed():
    from ai_coding.security.approval import ApprovalResult, CLIApprovalCallback

    def _raising(_prompt):
        raise OSError()

    cb = CLIApprovalCallback(input_fn=_raising)
    result = await cb.approve("read", {}, "")
    assert result == ApprovalResult.NO


def test_cli_approval_long_params_truncated():
    from ai_coding.security.approval import CLIApprovalCallback

    text = CLIApprovalCallback._format_prompt("bash", {"c": "x" * 300}, "reason")
    line = next(s for s in text.split("\n") if s.startswith("  params: "))
    seg = line.split("  params: ", 1)[1]
    assert seg.endswith("...")
    assert len(seg) == 120  # 117 chars + "...", capped


async def test_approve_from_enum_and_unknown():
    from ai_coding.security.approval import approve_from_gate_decision

    assert await approve_from_gate_decision(PermissionDecision.ALLOW, "read", {}, None) is True
    assert await approve_from_gate_decision(PermissionDecision.DENY, "bash", {}, None) is False
    # unknown string -> WARN, no callback -> fail-closed False
    assert await approve_from_gate_decision("bogus", "x", {}, None) is False


async def test_approve_warn_without_callback_fails_closed():
    from ai_coding.security.approval import approve_from_gate_decision

    decision = Decision(PermissionDecision.WARN, "needs human")
    assert await approve_from_gate_decision(decision, "edit", {}, None) is False


async def test_approve_warn_with_yes_callback():
    from ai_coding.security.approval import (
        ApprovalCallback,
        ApprovalResult,
        approve_from_gate_decision,
    )

    class _Yes(ApprovalCallback):
        async def approve(self, tool_name, params, reason):
            return ApprovalResult.YES

    decision = Decision(PermissionDecision.WARN, "ok")
    assert await approve_from_gate_decision(decision, "edit", {}, _Yes()) is True


# ---------------------------------------------------------------- permission gate Decision

def test_decision_equality_and_hash_repr():
    a1 = Decision(PermissionDecision.ALLOW, "x")
    a2 = Decision(PermissionDecision.ALLOW, "x")
    b = Decision(PermissionDecision.ALLOW, "y")
    d = Decision(PermissionDecision.DENY, "x")
    assert a1 == a2
    assert a1 != b
    assert a1 == PermissionDecision.ALLOW  # value equality with enum
    assert hash(a1) == hash(a2)  # same decision value hashes equal
    assert len({a1, a1}) == 1
    assert a1 != d  # different decision value, even same reason
    assert "allow" in repr(a1)
    assert "x" in repr(a1)


# ---------------------------------------------------------------- sdk adapter (safety)

def test_sdk_adapter_non_string_arguments_parsed_empty(tmp_path):
    from ai_coding.tools.sdk_adapter import tool_to_function_tool

    class _TTool(BaseTool):
        @property
        def name(self):
            return "t"

        @property
        def description(self):
            return "t"

        @property
        def params_json_schema(self):
            return {}

        async def execute(self, params):
            return ToolResult(success=True, output=f"got {params}")

    ft = tool_to_function_tool(_TTool(), PermissionGate())

    class _Ctx:
        tool_arguments = 123  # not a str / dict -> must become {}

    async def _run():
        return await ft.on_invoke_tool(_Ctx(), "id")

    import asyncio

    assert asyncio.run(_run()) == "got {}"


def test_sdk_adapter_rejects_non_registry():
    from ai_coding.tools.sdk_adapter import registry_to_sdk_tools

    with pytest.raises(TypeError):
        registry_to_sdk_tools([], PermissionGate())


# ---------------------------------------------------------------- skill tool

def test_skill_tool_metadata_and_missing_dir(tmp_path):
    from ai_coding.tools.skill_tool import SkillTool

    tool = SkillTool(tmp_path / "nope")
    assert tool.name == "skill"
    assert tool.description
    assert tool.params_json_schema
    assert tool.list_skills() == []


async def test_skill_tool_action_list_no_skills(tmp_path):
    from ai_coding.tools.skill_tool import SkillTool

    tool = SkillTool(tmp_path / "empty")
    r = await tool.execute({"action": "list"})
    assert r.success is True
    assert "(no skills available)" in r.output


async def test_skill_tool_read_requires_name_and_sanitizes(tmp_path):
    from ai_coding.tools.skill_tool import SkillTool

    (tmp_path / "empty").mkdir()
    tool = SkillTool(tmp_path / "empty")

    r1 = await tool.execute({"action": "read"})
    assert r1.success is False and "name" in r1.output.lower()

    r2 = await tool.execute({"action": "read", "name": "../escape"})
    assert r2.success is False and "invalid" in r2.output.lower()


async def test_skill_tool_read_content(tmp_path):
    from ai_coding.tools.skill_tool import SkillTool

    skill = tmp_path / "python"
    skill.mkdir()
    (skill / "SKILL.md").write_text("write clean code", encoding="utf-8")

    tool = SkillTool(tmp_path)
    r = await tool.execute({"action": "read", "name": "python"})
    assert r.success is True
    assert "write clean code" in r.output


# ---------------------------------------------------------------- edit tool branches

async def test_edit_empty_old_string_rejected(tmp_path):
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.tools.edit_tool import EditTool

    tool = EditTool(Sandbox(tmp_path), FileStateTracker())
    r = await tool.execute({"path": "f", "old_string": "", "new_string": "x"})
    assert r.success is False and "empty" in r.output


async def test_edit_sandbox_escape_rejected(tmp_path):
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.tools.edit_tool import EditTool

    (tmp_path / "f").write_text("hello", encoding="utf-8")
    tool = EditTool(Sandbox(tmp_path), FileStateTracker())
    r = await tool.execute({"path": "../outside", "old_string": "h", "new_string": "x"})
    assert r.success is False and "outside" in r.output


async def test_edit_file_not_found(tmp_path):
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.tools.edit_tool import EditTool

    tool = EditTool(Sandbox(tmp_path), FileStateTracker())
    r = await tool.execute({"path": "missing.txt", "old_string": "h", "new_string": "x"})
    assert r.success is False and "not found" in r.output


async def test_edit_replace_all(tmp_path):
    from ai_coding.tools.edit_tool import EditTool

    class _Fresh:
        def check(self, target):
            return FileState.CLEAN

        def record(self, target):
            pass

    (tmp_path / "f").write_text("a b a b", encoding="utf-8")
    tool = EditTool(Sandbox(tmp_path), _Fresh())
    r = await tool.execute({"path": "f", "old_string": "a", "new_string": "z", "replace_all": True})
    assert r.success is True and "2" in r.output


async def test_edit_read_oserror_and_write_oserror(tmp_path):
    from ai_coding.tools.edit_tool import EditTool

    class _Fresh:
        def check(self, target):
            return FileState.CLEAN

        def record(self, target):
            pass

    class _BrokenRead:
        def is_file(self):
            return True

        def read_text(self, *a, **k):
            raise PermissionError("no read")

        def write_text(self, *a, **k):
            pass

    class _BrokenWrite:
        def is_file(self):
            return True

        def read_text(self, *a, **k):
            return "a b"

        def write_text(self, *a, **k):
            raise PermissionError("no write")

    class _Sandbox:
        def __init__(self, target):
            self.target = target

        def resolve(self, _p):
            return self.target

    rr = await EditTool(_Sandbox(_BrokenRead()), _Fresh()).execute(
        {"path": "f", "old_string": "x", "new_string": "y"}
    )
    assert rr.success is False and "read error" in rr.output

    wr = await EditTool(_Sandbox(_BrokenWrite()), _Fresh()).execute(
        {"path": "f", "old_string": "a", "new_string": "z"}
    )
    assert wr.success is False and "write error" in wr.output


# ---------------------------------------------------------------- read tool branches

async def test_read_file_too_large(tmp_path):
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.tools.read_tool import ReadTool

    (tmp_path / "big").write_text("x" * 100, encoding="utf-8")
    tool = ReadTool(Sandbox(tmp_path), FileStateTracker(), max_file_size=10)
    r = await tool.execute({"path": "big"})
    assert r.success is False and "too large" in r.output


async def test_read_stat_oserror(tmp_path):
    from ai_coding.security.file_state_tracker import FileStateTracker
    from ai_coding.tools.read_tool import ReadTool

    class _BrokenStat:
        def is_file(self):
            return True

        def stat(self):
            raise PermissionError("no stat")

        def read_text(self, *a, **k):
            return "x"

    class _Sandbox:
        def __init__(self, target):
            self.target = target

        def resolve(self, _p):
            return self.target

    tool = ReadTool(_Sandbox(_BrokenStat()), FileStateTracker())
    r = await tool.execute({"path": "f"})
    assert r.success is False and "stat" in r.output


# ---------------------------------------------------------------- glob branches

async def test_glob_empty_pattern(tmp_path):
    from ai_coding.tools.glob_tool import GlobTool

    tool = GlobTool(Sandbox(tmp_path))
    r = await tool.execute({"pattern": ""})
    assert r.success is False and "empty" in r.output


async def test_glob_symbolic_link_escape_filtered(tmp_path):
    from ai_coding.tools.glob_tool import GlobTool

    # the target lives OUTSIDE the workspace (sibling dir under tmp_path.parent)
    outside = tmp_path.parent / "outside_escape_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    tool = GlobTool(Sandbox(tmp_path))
    r = await tool.execute({"pattern": "link/*.py"})

    # the escaped target must not leak into results
    assert r.success is True
    assert "secret" not in r.output or "(no matches)" in r.output


# ---------------------------------------------------------------- console rendering

def test_console_delta_info_error_rule_render():
    from rich.console import Console

    from ai_coding.ui.console import DialogueConsole

    c = Console(record=True, width=40)
    dc = DialogueConsole(c)
    dc.delta("tok")
    dc.info("note")
    dc.error("bad")
    dc.rule("H")
    text = c.export_text()
    assert "tok" in text
    assert "note" in text
    assert "bad" in text
    assert "H" in text
