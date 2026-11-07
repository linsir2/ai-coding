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
    assert result.detail == "read_only_shell_required"


@pytest.mark.parametrize(
    "extra",
    [
        {"network_permissions": "inherit", "justification": "needs the network"},
        {"network_permissions": "off"},
        {"justification": "just a note"},
    ],
)
def test_shell_read_refuses_every_network_key_in_precheck(extra):
    """X-01a: the read-only shell is not a way to obtain the host network.

    Refusing at ``precheck`` means the dispatcher never spends an approval on a
    call the execution layer will not honour.
    """
    tool = ShellReadTool()

    result = tool.precheck({"command": "ls", **extra})

    assert result is not None and result.success is False
    assert result.detail == "read_only_shell_required"
    assert "network" in result.output


def test_an_ordinary_plan_mode_inspection_is_still_allowed():
    """The guard is keyed on the network fields, not on shell_read itself."""
    tool = ShellReadTool()

    assert tool.precheck({"command": "ls"}) is None


@pytest.mark.asyncio
async def test_execute_never_passes_the_host_network_to_the_backend(tmp_path):
    """Even a direct ``execute`` (tests, other assemblies) cannot inherit.

    The dispatcher/gate half is not involved here: this is the execution-layer
    guarantee that ``_requested_network`` always answers ``None``.
    """
    from ai_coding.security.process_sandbox import ProcessSandboxPolicy

    class _Backend:
        name = "capture"
        tier = "full"
        network_mode = "deny"
        networks: list[str | None] = []

        def available(self) -> bool:
            return True

        def gap(self) -> str:
            return ""

        def wrap(self, argv, roots, *, network=None):  # noqa: ANN001
            self.networks.append(network)
            return list(argv)

    backend = _Backend()
    tool = ShellReadTool(
        policy=ProcessSandboxPolicy(mode="enforce"),
        backend=backend,
        roots=[tmp_path],
    )

    result = await tool.execute(
        {
            "command": "ls",
            "network_permissions": "inherit",
            "justification": "local dev server",
        },
        ToolContext(workspace=str(tmp_path)),
    )

    assert result.success is True, result.output
    assert "inherit" not in backend.networks, (
        "shell_read must never ask for the host network"
    )


@pytest.mark.asyncio
async def test_an_outside_path_is_a_sandbox_violation():
    tool = ShellReadTool()
    ctx = ToolContext(workspace="/tmp/inside")

    result = await tool.execute({"command": "ls /etc"}, ctx)

    assert result.success is False and result.detail == "sandbox_violation"


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
