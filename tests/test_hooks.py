"""TDD (R4c): hooks — user policy at fixed points in the loop.

Two properties matter more than features here: a hook may never take the turn
down, and the environment a hook runs in must not leak the model's credentials
(docs/r4-analysis.md 6.3).
"""

import json
import sys

import pytest

from ai_coding.core.hooks import (
    CommandHookRunner,
    HookDecision,
    HookPoint,
    HookSpec,
    PythonHookRunner,
)


def _spec(tmp_path, script: str, *, capability: str = "observe", point=HookPoint.TOOL_BEFORE):
    path = tmp_path / "hook.py"
    path.write_text(script, encoding="utf-8")
    return HookSpec(
        point=point,
        kind="command",
        command=[sys.executable, str(path)],
        capability=capability,
    )


def _ctx(**over):
    base = {"point": "tool.before", "tool": {"name": "bash", "params": {"command": "ls"}}}
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_a_command_hook_receives_the_context_on_stdin(tmp_path):
    spec = _spec(
        tmp_path,
        "import json,sys\n"
        "ctx = json.load(sys.stdin)\n"
        "print(json.dumps({'decision': 'allow', 'reason': ctx['tool']['name']}))\n",
    )
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert len(results) == 1
    assert results[0].decision is HookDecision.ALLOW
    assert results[0].reason == "bash"


@pytest.mark.asyncio
async def test_a_blocking_hook_carries_its_reason(tmp_path):
    spec = _spec(
        tmp_path,
        "import json,sys\n"
        "print(json.dumps({'decision': 'block', 'reason': 'git push is not allowed'}))\n",
        capability="block",
    )
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.BLOCK
    assert "git push" in results[0].reason


@pytest.mark.asyncio
async def test_a_rewriting_hook_returns_new_parameters(tmp_path):
    spec = _spec(
        tmp_path,
        "import json,sys\n"
        "print(json.dumps({'decision': 'rewrite', 'params': {'command': 'ls -la'}}))\n",
        capability="rewrite",
    )
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.REWRITE
    assert results[0].params == {"command": "ls -la"}


@pytest.mark.asyncio
async def test_a_nonzero_exit_blocks_a_blocking_hook(tmp_path):
    spec = _spec(
        tmp_path,
        "import sys\nsys.exit(3)\n",
        capability="block",
    )
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.BLOCK
    assert "3" in results[0].reason


@pytest.mark.asyncio
async def test_a_broken_observing_hook_only_warns(tmp_path):
    """An observer must never be able to stop a call."""
    spec = _spec(tmp_path, "import sys\nsys.exit(9)\n", capability="observe")
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.ALLOW
    assert results[0].failed is True


@pytest.mark.asyncio
async def test_a_missing_command_never_raises(tmp_path):
    spec = HookSpec(
        point=HookPoint.TOOL_BEFORE,
        kind="command",
        command=[str(tmp_path / "does-not-exist")],
        capability="block",
    )
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.BLOCK  # fail closed when it matters


@pytest.mark.asyncio
async def test_hook_environment_carries_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-should-never-be-visible")
    monkeypatch.setenv("SOME_TOKEN", "tok-should-never-be-visible")
    spec = _spec(
        tmp_path,
        "import json,os,sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'decision': 'allow', 'reason': ','.join(sorted(os.environ))}))\n",
    )
    runner = CommandHookRunner([spec], workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    names = results[0].reason.split(",")
    assert "DASHSCOPE_API_KEY" not in names
    assert "SOME_TOKEN" not in names
    assert "PATH" in names  # ordinary variables are still passed


@pytest.mark.asyncio
async def test_hooks_run_in_configuration_order_and_only_for_their_point(tmp_path):
    first = _spec(
        tmp_path,
        "import json,sys\nprint(json.dumps({'decision':'allow','reason':'first'}))\n",
    )
    second = HookSpec(
        point=HookPoint.TURN_END,
        kind="command",
        command=[sys.executable, "-c", "print('{\"decision\":\"allow\",\"reason\":\"second\"}')"],
    )
    runner = CommandHookRunner([first, second], workspace=str(tmp_path))

    only_before = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert [r.reason for r in only_before] == ["first"]


@pytest.mark.asyncio
async def test_stdin_is_redacted_unless_the_hook_asks_for_raw(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    script = (
        "import json,sys\n"
        "ctx = json.load(sys.stdin)\n"
        "print(json.dumps({'decision': 'allow',"
        " 'reason': ctx['tool']['params']['command']}))\n"
    )
    redacted = _spec(tmp_path, script)
    raw = _spec(tmp_path, script)
    raw = HookSpec(
        point=raw.point,
        kind="command",
        command=raw.command,
        capability="observe",
        raw=True,
    )
    ctx = {
        "point": "tool.before",
        "tool": {"name": "bash", "params": {"command": f"echo {secret}"}},
    }

    safe = await CommandHookRunner([redacted], workspace=str(tmp_path)).run(
        HookPoint.TOOL_BEFORE, ctx
    )
    raw_result = await CommandHookRunner([raw], workspace=str(tmp_path)).run(
        HookPoint.TOOL_BEFORE, ctx
    )

    assert secret not in safe[0].reason
    assert secret in raw_result[0].reason


# ---- the in-process form ---------------------------------------------------


def _plugin(tmp_path, body: str) -> str:
    plugin_dir = tmp_path / "hooks"
    plugin_dir.mkdir(exist_ok=True)
    (plugin_dir / "my_rules.py").write_text(body, encoding="utf-8")
    return str(plugin_dir)


@pytest.mark.asyncio
async def test_a_python_hook_runs_in_process_and_can_rewrite(tmp_path):
    plugin_dir = _plugin(
        tmp_path,
        "def check(ctx):\n"
        "    params = dict(ctx['tool']['params'])\n"
        "    params['command'] = 'ls -la'\n"
        "    return {'decision': 'rewrite', 'params': params}\n",
    )
    spec = HookSpec(
        point=HookPoint.TOOL_BEFORE,
        kind="python",
        module="my_rules",
        entrypoint="check",
        capability="rewrite",
    )
    runner = PythonHookRunner([spec], plugin_dir=plugin_dir, workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.REWRITE
    assert results[0].params == {"command": "ls -la"}


@pytest.mark.asyncio
async def test_a_crashing_python_hook_follows_the_capability_matrix(tmp_path):
    plugin_dir = _plugin(tmp_path, "def check(ctx):\n    raise RuntimeError('boom')\n")
    spec = HookSpec(
        point=HookPoint.TOOL_BEFORE,
        kind="python",
        module="my_rules",
        entrypoint="check",
        capability="observe",
    )
    runner = PythonHookRunner([spec], plugin_dir=plugin_dir, workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.ALLOW
    assert results[0].failed is True


@pytest.mark.asyncio
async def test_a_missing_python_module_fails_closed_for_a_blocking_hook(tmp_path):
    spec = HookSpec(
        point=HookPoint.TOOL_BEFORE,
        kind="python",
        module="nope",
        capability="block",
    )
    runner = PythonHookRunner([spec], plugin_dir=str(tmp_path), workspace=str(tmp_path))

    results = await runner.run(HookPoint.TOOL_BEFORE, _ctx())

    assert results[0].decision is HookDecision.BLOCK


@pytest.mark.asyncio
async def test_a_python_hook_that_injects_context(tmp_path):
    plugin_dir = _plugin(
        tmp_path,
        "def check(ctx):\n    return {'decision': 'inject', 'context': 'use uv, not pip'}\n",
    )
    spec = HookSpec(
        point=HookPoint.TURN_START,
        kind="python",
        module="my_rules",
        entrypoint="check",
        capability="inject",
    )
    runner = PythonHookRunner([spec], plugin_dir=plugin_dir, workspace=str(tmp_path))

    results = await runner.run(HookPoint.TURN_START, {"point": "turn.start"})

    assert results[0].decision is HookDecision.INJECT
    assert results[0].context == "use uv, not pip"


@pytest.mark.asyncio
async def test_a_hook_declared_for_an_impossible_point_is_rejected():
    from ai_coding.core.hooks import validate_spec

    spec = HookSpec(
        point=HookPoint.STEP_BEFORE,
        kind="command",
        command=["true"],
        capability="block",
    )
    with pytest.raises(ValueError, match="block"):
        validate_spec(spec)


def test_hook_specs_round_trip_from_config():
    from ai_coding.config.models import HookEventConfig, HooksConfig
    from ai_coding.core.hooks import specs_from_config

    config = HooksConfig(
        enabled=True,
        events=[
            HookEventConfig(
                point="tool.before", kind="command", command=["x"], capability="block"
            )
        ],
    )
    specs = specs_from_config(config)
    assert len(specs) == 1
    assert specs[0].point is HookPoint.TOOL_BEFORE
    assert json.dumps(specs[0].capability) == '"block"'


def test_the_default_plugin_dir_is_not_the_workspace(tmp_path, monkeypatch):
    """A python hook is ``exec_module``'d, so the default must not live in the
    repo tree: a default inside the workspace lets the repository supply the
    module that the operator's own config then runs.
    """
    from pathlib import Path

    from ai_coding.cli import _make_hooks
    from ai_coding.config.models import HookEventConfig, HooksConfig

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    hooks_dir = workspace / ".ai-coding" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "evil.py").write_text(
        "def run(ctx):\n    raise RuntimeError('PWNED')\n", encoding="utf-8"
    )

    cfg = HooksConfig(
        enabled=True,
        events=[HookEventConfig(point="turn.start", kind="python", module="evil")],
    )
    group = _make_hooks(cfg, str(workspace))

    assert len(group) == 1
    plugin_dir = Path(group._runners[0]._plugin_dir)  # type: ignore[attr-defined]
    assert not plugin_dir.is_relative_to(workspace), (
        "the default plugin dir must not be inside the repository tree"
    )
