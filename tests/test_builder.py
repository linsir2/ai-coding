"""TDD (M4): builder — assemble SDK tool stacks for main and sub-agents.

The main stack (``include_subagent=True``) exposes ``subAgent``; the stack each
sub-agent builds internally (via ``build_subagent_runner``) must NOT re-expose
``subAgent`` to avoid unbounded recursion.  All checks are offline.
"""

from types import SimpleNamespace

import pytest

from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.tools.builder import build_subagent_runner, build_tool_stack


def _cfg(ws: str) -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            bash=SimpleNamespace(enabled=True, timeout_seconds=60),
            read=SimpleNamespace(enabled=True, max_file_size=1 << 20),
            write=SimpleNamespace(enabled=True),
            edit=SimpleNamespace(enabled=True),
            glob=SimpleNamespace(enabled=True),
        ),
        ai=SimpleNamespace(subagent_worktree_isolation=True),
    )


def _names(sdk_tools) -> set[str]:
    return {t.name for t in sdk_tools}


def test_main_stack_exposes_subagent(tmp_path):
    cfg = _cfg(str(tmp_path))

    sdk = build_tool_stack(cfg, str(tmp_path), None, include_subagent=True)

    names = _names(sdk)
    assert "subAgent" in names
    assert "bash" in names
    assert "read" in names


def test_subagent_internal_stack_excludes_nested_subagent(tmp_path):
    cfg = _cfg(str(tmp_path))
    runner = build_subagent_runner(cfg, str(tmp_path), None)

    inner = runner._build_tools(None)  # the sub-agent's own tools (main workdir)
    names = _names(inner)

    assert "subAgent" not in names  # no recursion
    assert "read" in names  # file tools still available to the sub-agent


# ---- WARN-tier approval wiring (regression for non-interactive usability) ----

async def _needs(stack, name, params):
    fn = next(t for t in stack if t.name == name)
    return await fn.needs_approval(None, params, "c1")


@pytest.mark.asyncio
async def test_default_stack_autoapproves_warn_tools(tmp_path):
    cfg = _cfg(str(tmp_path))
    stack = build_tool_stack(cfg, str(tmp_path), None)

    # WARN-tier tools (write/edit/bash) are usable by default (non-interactive).
    assert await _needs(stack, "write", {"path": "f"}) is False
    assert await _needs(stack, "edit", {"path": "f"}) is False
    assert await _needs(stack, "bash", {"command": "echo hi"}) is False
    # ALLOW-tier tools unaffected
    assert await _needs(stack, "read", {"path": "f"}) is False


@pytest.mark.asyncio
async def test_default_stack_still_denies_dangerous_bash(tmp_path):
    cfg = _cfg(str(tmp_path))
    stack = build_tool_stack(cfg, str(tmp_path), None)

    # Hard-deny bash (sudo / rm -rf /) stays blocked even under auto-approve.
    assert await _needs(stack, "bash", {"command": "sudo ls"}) is True
    assert await _needs(stack, "bash", {"command": "rm -rf /"}) is True


class _AlwaysNo(ApprovalCallback):
    async def approve(self, tool_name, params, reason):
        return ApprovalResult.NO


@pytest.mark.asyncio
async def test_explicit_no_approval_blocks_warn_tools(tmp_path):
    cfg = _cfg(str(tmp_path))
    stack = build_tool_stack(cfg, str(tmp_path), None, approval=_AlwaysNo())

    assert await _needs(stack, "write", {"path": "f"}) is True
    assert await _needs(stack, "read", {"path": "f"}) is False  # ALLOW unaffected
