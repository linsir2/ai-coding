"""A sub-agent must not run with fewer guards than its parent.

``AgentLoop(guards=None)`` does not mean "no guards configured" -- the
dispatcher turns it into ``GuardChain([])``, which allows every call.  A
sub-agent used to be built exactly that way, so it inherited the workspace
isolation of a worktree and none of the policy: a bash command the user had
explicitly put on ``bash_deny`` ran anyway, because the red line lived in the
parent's chain and the child never received it.

The tests drive a real ``SubAgentRunner`` and a real ``AgentLoop`` through a real
``ToolDispatcher``.  Only the model is faked -- the point is who *holds* the
guards, and that cannot be observed by inspecting objects.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.config.models import AppConfig, GuardConfig
from ai_coding.core.guard import build_guards
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.model_config import ModelConfig
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


def _ref(call_id: str, name: str, **params: Any) -> ToolCallRef:
    """What a real engine returns: a ref whose arguments are still JSON text."""
    import json

    return ToolCallRef(id=call_id, name=name, arguments=json.dumps(params))


class _OneCallEngine:
    """Emits one tool call, then a final answer."""

    def __init__(self, call: ToolCallRef) -> None:
        self._call = call
        # Count our own passes: the message-count heuristic ("< 3 means first
        # pass") broke the day a tail block was added (13 §2.4 environment).
        self._passes = 0
        self.model_config = ModelConfig(name="m", base_url="https://x/v1", api_key="k")
        self.on_retry = None
        self.on_fallback = None

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self._passes += 1
        if self._passes == 1:
            return StepResult(
                text=None,
                tool_calls=(self._call,),
                finish_reason="tool_calls",
                usage=None,
                reasoning=None,
            )
        # React to the tool result, which is where a guard's refusal arrives:
        # ``dispatch._denied`` puts the verdict in ``data.reason`` and the text in
        # ``output``.  Only that message is quoted -- the conclusion is truncated
        # at 4000 chars, and quoting the whole history pushes the refusal out.
        tool_msg = next(
            (m for m in reversed(messages) if getattr(m, "role", "") == "tool"), None
        )
        said = str(getattr(tool_msg, "content", "") or "") if tool_msg else ""
        return StepResult(
            text=f"done: {said}",
            tool_calls=(),
            finish_reason="stop",
            usage=None,
            reasoning=None,
        )

    async def aclose(self) -> None:
        return None


class _RecordingTool(BaseTool):
    """A tool that records whether it was ever allowed to run."""

    parallel_safe = False

    def __init__(self, tool_name: str) -> None:
        self._name = tool_name
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"test {self._name}"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}, "path": {"type": "string"}},
        }

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> Any:
        self.calls.append(params)
        from ai_coding.domain.message import ToolResult

        return ToolResult(success=True, output="ok")


def _runner(tmp_path, guard_cfg: GuardConfig, tool: BaseTool, call: ToolCallRef):
    app = AppConfig(
        default_model="m",
        models={"m": ModelConfig(name="m", base_url="https://x/v1", api_key="k")},
        guard=guard_cfg,
    )

    def build_tools(workdir: str | None) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(tool)
        return registry

    return SubAgentRunner(
        app,
        engine_factory=lambda: _OneCallEngine(call),
        build_tools=build_tools,
        workspace=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_a_subagent_inherits_the_configured_bash_red_lines(tmp_path):
    """The gap itself: a line the user wrote into ``bash_deny`` was enforced in
    the parent and silently ignored in the child."""
    bash = _RecordingTool("bash")
    runner = _runner(
        tmp_path,
        GuardConfig(duplicate_threshold=0, bash_deny=["rm -rf"]),
        bash,
        _ref("c1", "bash", command="rm -rf /"),
    )

    packet = await runner.run("clean up", name="t")

    assert bash.calls == [], "the red line must stop the command before it runs"
    # And the refusal has to reach the model, or the child just retries the same
    # call: ``_denied`` puts the guard's reason in the tool result the next
    # model turn reads.
    assert "rm -rf" in str(packet)


@pytest.mark.asyncio
async def test_a_subagent_runs_commands_that_are_not_denied(tmp_path):
    """The other half: guards must not be so eager that ordinary work stops."""
    bash = _RecordingTool("bash")
    runner = _runner(
        tmp_path,
        GuardConfig(duplicate_threshold=0, bash_deny=["rm -rf"]),
        bash,
        _ref("c1", "bash", command="echo hi"),
    )

    await runner.run("say hi", name="t")

    assert bash.calls == [{"command": "echo hi"}]


@pytest.mark.asyncio
async def test_a_subagent_without_configured_guards_is_untouched(tmp_path):
    """Everything off is a legitimate configuration and must not be treated as
    "the chain is missing": the fix adds enforcement, not friction."""
    bash = _RecordingTool("bash")
    runner = _runner(
        tmp_path,
        GuardConfig(duplicate_threshold=0),
        bash,
        _ref("c1", "bash", command="echo hi"),
    )

    await runner.run("say hi", name="t")

    assert bash.calls == [{"command": "echo hi"}]


@pytest.mark.asyncio
async def test_path_strictness_reaches_the_subagent_too(tmp_path):
    """``strict_paths`` is about a shell reaching outside the workspace, and a
    sub-agent's workspace is the worktree -- not the whole filesystem.  It only
    reads bash commands by design (``PathEscapeGuard.check`` returns ALLOW for
    anything else); the file tools are confined by ``Sandbox`` instead."""
    bash = _RecordingTool("bash")
    runner = _runner(
        tmp_path,
        GuardConfig(duplicate_threshold=0, strict_paths=True),
        bash,
        _ref("c1", "bash", command="cat /etc/passwd"),
    )

    packet = await runner.run("read outside", name="t")

    assert bash.calls == [], "a strict child must not reach outside its worktree"
    assert "/etc/passwd" in str(packet), "and the refusal has to say which path"


@pytest.mark.asyncio
async def test_the_default_config_still_gives_the_child_its_duplicate_budget(tmp_path):
    """``duplicate_threshold`` defaults to 2, so the default child is guarded."""
    read = _RecordingTool("read")

    class _Repeater(_OneCallEngine):
        def __init__(self) -> None:
            super().__init__(_ref("c", "read", path="/a"))

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            return StepResult(
                text=None,
                tool_calls=(_ref("c", "read", path="/a"),),
                finish_reason="tool_calls",
                usage=None,
                reasoning=None,
            )

    app = AppConfig(
        default_model="m",
        models={"m": ModelConfig(name="m", base_url="https://x/v1", api_key="k")},
        guard=GuardConfig(),
    )

    def build_tools(workdir):
        registry = ToolRegistry()
        registry.register(read)
        return registry

    runner = SubAgentRunner(
        app,
        engine_factory=_Repeater,  # the class itself is the zero-arg factory
        build_tools=build_tools,
        workspace=str(tmp_path),
    )
    runner._timeout = 20.0

    await runner.run("loop forever", name="t")

    assert len(read.calls) < 5, f"duplicate guard did not engage: {len(read.calls)} calls"


# ---- build_guards itself ---------------------------------------------------


def test_build_guards_reads_the_same_config_the_parent_uses():
    chain = build_guards(
        AppConfig(
            default_model="m",
            models={},
            guard=GuardConfig(
                duplicate_threshold=3, bash_deny=["a"], bash_confirm=["b"], strict_paths=True
            ),
        )
    )

    assert chain is not None
    # 13 §2.2: the failed-repeat guard is a permanent first member now.
    assert set(chain.names) == {
        "failed_repeat", "duplicate_call", "bash_policy", "path_escape",
    }


def test_build_guards_seeds_only_the_dormant_failed_repeat_guard():
    """13 §2.2: the chain is never ``None`` and never empty — the failed-repeat
    guard is a permanent first member, dormant until the ladder's L2 arms it.
    Every other switch has to be off explicitly (``duplicate_threshold``
    defaults to 2)."""
    cfg = AppConfig(default_model="m", models={}, guard=GuardConfig(duplicate_threshold=0))

    assert build_guards(cfg).names == ("failed_repeat",)


def test_the_default_config_still_enables_the_duplicate_guard():
    """Not a test of the fix, but of the assumption above: a default config is
    not an empty one, and quietly changing that would alter every deployment."""
    cfg = AppConfig(default_model="m", models={}, guard=GuardConfig())

    chain = build_guards(cfg)

    assert chain is not None
    assert chain.names == ("failed_repeat", "duplicate_call")


def test_every_subagent_gets_its_own_duplicate_call_budget():
    """Sharing one chain would make a parent call count against the child: the
    refusal names a call the child never made, which the model cannot act on."""
    cfg = AppConfig(default_model="m", models={}, guard=GuardConfig(duplicate_threshold=1))

    a = build_guards(cfg)
    b = build_guards(cfg)

    # 13 §2.2: index 0 is the (also fresh) failed-repeat guard now; address the
    # duplicate counter by name so chain order can never hide a shared budget.
    dup_a = next(g for g in a._guards if getattr(g, "name", "") == "duplicate_call")
    dup_b = next(g for g in b._guards if getattr(g, "name", "") == "duplicate_call")
    dup_a._counts["x"] = 9

    assert not dup_b._counts.get("x")


def test_a_runner_assembles_its_chain_at_construction():
    """Built once and reset between runs, never rebuilt per run: rebuilding
    would make the threshold meaningless mid-turn."""
    app = AppConfig(default_model="m", models={}, guard=GuardConfig(bash_deny=["x"]))
    runner = SubAgentRunner(
        app,
        engine_factory=lambda: None,
        build_tools=lambda w: ToolRegistry(),
        workspace=".",
    )

    assert runner._guards is not None
    assert "bash_policy" in runner._guards.names
