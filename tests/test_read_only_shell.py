"""shell_read: one simple read-only command, default deny.

The plan posture needs ``git log`` / ``find`` / ``wc``; opening bash would open
every command.  The classifier admits one argv command from a small set and
refuses shell syntax, expansion, redirection and writing options.
"""

import pytest

from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.security.read_only_shell import read_only_problem
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.shell_read_tool import ShellReadTool


class _Engine:
    async def step(self, *args, **kwargs):  # pragma: no cover - never runs
        return StepResult(text="ok", finish_reason="stop")

    async def aclose(self):
        return None


class _Deferred(BaseTool):
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return self._name

    @property
    def params_json_schema(self):
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):
        return None


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -5",
        "git status --short",
        "git diff --stat",
        "git branch -a",
        "git rev-parse HEAD",
        "ls -la",
        "wc -l app.py",
        "find . -name '*.py'",
        "tree -L 2",
        "du -sh .",
    ],
)
def test_read_only_commands_are_allowed(command):
    assert read_only_problem(command) == ""


@pytest.mark.parametrize(
    "command",
    [
        "git branch -D main",
        "git branch new-branch",
        "git -C /tmp show HEAD",
        "git log --output=/tmp/x",
        "git checkout main",
        "find . -delete",
        "find . -exec rm {} ;",
        "find . -fprint /tmp/x",
        "ls; rm -rf /",
        "ls && rm -rf /",
        "echo x > f",
        "cat $(pwd)",
        "cat `pwd`",
        "VAR=x ls",
        "bash -c 'rm -rf /'",
        "python -c 'print(1)'",
        "sed -i 's/a/b/' f",
        "sort -o out f",
        "head -1 ~/.ssh/id_rsa",
        "find ..",
        "tree -o out.txt",
    ],
)
def test_writing_or_expanding_commands_are_refused(command):
    assert read_only_problem(command) != ""


def test_the_tool_returns_a_machine_code_for_a_refused_command():
    tool = ShellReadTool()

    result = tool.precheck({"command": "rm -rf /"})

    assert result is not None and result.success is False
    assert result.error == "read_only_shell_required"


@pytest.mark.asyncio
async def test_an_outside_path_is_a_sandbox_violation():
    tool = ShellReadTool()
    ctx = ToolContext(workspace="/tmp/inside")

    result = await tool.execute({"command": "ls /etc"}, ctx)

    assert result.success is False and result.error == "sandbox_violation"


def test_the_tool_is_plan_only_and_read_only():
    tool = ShellReadTool()

    assert tool.plan_only is True
    assert tool.mutates_world is False


def test_the_plan_surface_carries_shell_read_and_build_does_not():
    registry = ToolRegistry()
    registry.register(_Deferred("read"))
    registry.register(ShellReadTool())
    loop = AgentLoop(
        _Engine(),
        SessionService.__new__(SessionService),
        registry,
        workspace=".",
        settings=AIConfig(stop_policy="natural", max_tool_iterations=3),
    )

    plan_names = {tool.name for tool in loop._surface_tools("plan", True)}
    build_names = {tool.name for tool in loop._surface_tools("normal", False)}

    assert "shell_read" in plan_names
    assert "shell_read" not in build_names
