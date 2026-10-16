"""TDD (M2-1): bash tool."""

import time
from pathlib import Path

import pytest

from ai_coding.tools.base import ToolContext
from ai_coding.tools.bash_tool import BashTool


@pytest.mark.asyncio
async def test_bash_echo():
    tool = BashTool()
    result = await tool.execute({"command": "echo hello"})
    assert result.success is True
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_bash_stderr_captured():
    tool = BashTool()
    result = await tool.execute({"command": "echo err >&2"})
    assert result.success is True
    assert "err" in result.output


@pytest.mark.asyncio
async def test_bash_nonzero_exit_still_success_with_output():
    # non-zero exit is captured in output but success reflects the command exit
    tool = BashTool()
    result = await tool.execute({"command": "exit 1"})
    assert result.success is False
    assert "exit code 1" in result.output.lower() or result.output is not None


@pytest.mark.asyncio
async def test_bash_timeout():
    tool = BashTool(timeout_seconds=1)
    result = await tool.execute({"command": "sleep 5"})
    assert result.success is False
    assert "timeout" in result.output.lower()


@pytest.mark.asyncio
async def test_bash_output_truncation():
    tool = BashTool(max_output_chars=10)
    result = await tool.execute({"command": "echo 0123456789abcdef"})
    assert result.success is True
    # output must not exceed the limit + truncation notice
    assert len(result.output) <= 100  # generous: notice text adds some
    assert "truncated" in result.output.lower()


@pytest.mark.asyncio
async def test_bash_runs_in_the_context_workspace(tmp_path: Path):
    """R2b: the description promises the workspace; the code used to ignore it."""
    tool = BashTool()
    result = await tool.execute({"command": "pwd"}, ToolContext(workspace=str(tmp_path)))
    assert result.success is True
    assert Path(result.output.strip()).resolve() == tmp_path.resolve()


@pytest.mark.asyncio
async def test_bash_falls_back_when_the_workspace_does_not_exist(tmp_path: Path):
    tool = BashTool()
    result = await tool.execute(
        {"command": "echo ok"}, ToolContext(workspace=str(tmp_path / "missing"))
    )
    assert result.success is True
    assert "ok" in result.output


@pytest.mark.asyncio
async def test_bash_timeout_holds_even_when_a_child_keeps_the_pipe(tmp_path: Path):
    """R2a/R2b: the old implementation hung here forever (docs/r2-analysis.md 1.4-A)."""
    tool = BashTool(timeout_seconds=1)
    started = time.monotonic()
    result = await tool.execute({"command": "sleep 30 & sleep 30"})
    elapsed = time.monotonic() - started
    assert result.success is False
    assert "timeout" in result.output.lower()
    assert elapsed < 8.0, f"bash tool took {elapsed:.1f}s; the timeout is not hard"


@pytest.mark.asyncio
async def test_r8_bash_emits_chunks_while_running():
    """R8c: the 2.4s of silence measured in docs/r8-analysis.md 1.4 is gone."""
    from ai_coding.core.events import ToolChunk

    seen: list[object] = []
    tool = BashTool()

    result = await tool.execute(
        {"command": "for i in 1 2 3; do echo step-$i; sleep 0.05; done"},
        ToolContext(emit=seen.append, call_id="c1", workspace="."),
    )

    chunks = [event for event in seen if isinstance(event, ToolChunk)]
    assert result.success is True
    assert "".join(chunk.text for chunk in chunks) == "step-1\nstep-2\nstep-3\n"
    assert all(chunk.call_id == "c1" and chunk.tool_name == "bash" for chunk in chunks)


@pytest.mark.asyncio
async def test_r8_cancelling_a_bash_call_kills_the_command(tmp_path: Path):
    """The whole chain: dispatcher cancel -> kill_event -> run_process -> killpg."""
    import asyncio
    import os
    import time

    from ai_coding.core.dispatch import ToolDispatcher
    from ai_coding.domain.message import CANCELLED_BY_USER, ToolCallRef
    from ai_coding.security.approval import AutoApproveCallback
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.tools.registry import ToolRegistry

    cancel = asyncio.Event()
    pid_file = tmp_path / "pid"
    registry = ToolRegistry()
    registry.register(BashTool())
    dispatcher = ToolDispatcher(
        registry, PermissionGate(), AutoApproveCallback(), cancel=cancel
    )
    call = ToolCallRef(
        id="c1",
        name="bash",
        arguments=(
            '{"command": "echo $$ > ' + str(pid_file) + '; sleep 30"}'
        ),
    )

    async def trip() -> None:
        await asyncio.sleep(0.4)
        cancel.set()

    batches = await asyncio.gather(
        dispatcher.dispatch_batch(
            [call], ToolContext(workspace=str(tmp_path)), iteration=1
        ),
        trip(),
    )

    result = batches[0][0].result
    assert result.success is False
    assert result.error == CANCELLED_BY_USER
    pgid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 3.0
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "the cancelled command is still alive"
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_bash_per_call_timeout_widens_a_slow_command():
    """模型自己加宽：默认 1s 下必死的命令，带 timeout_seconds=3 必须活。

    这是根因修复的载体 —— 以前模型手里没有这个旋钮，只能用 shell 的
    `timeout 55` 自残（见 session 5ce5b8fb turn-1 it14）。
    """
    tool = BashTool(timeout_seconds=1)
    result = await tool.execute({"command": "sleep 2", "timeout_seconds": 3})
    assert result.success is True


@pytest.mark.asyncio
async def test_bash_default_timeout_still_applies():
    """不带参的行为一个字都不能变：默认超时还是超时。"""
    tool = BashTool(timeout_seconds=1)
    result = await tool.execute({"command": "sleep 2"})
    assert result.success is False
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_bash_timeout_is_clamped_to_the_ceiling():
    """要 999999 秒只能拿到 600 秒，且 data 里能观测到实际生效值。"""
    tool = BashTool(timeout_seconds=1)
    result = await tool.execute({"command": "echo ok", "timeout_seconds": 999999})
    assert result.success is True
    assert result.data is not None
    assert result.data["timeout_seconds"] == 600


def test_bash_constructor_default_is_capped_by_max():
    """构造时给的默认也不能越过上限，否则默认与按次两套口径。"""
    assert BashTool(timeout_seconds=9999)._timeout == 600


@pytest.mark.asyncio
async def test_bash_timeout_result_teaches_self_widening():
    """超时结果必须告诉模型“怎么加宽、能加到多少”，否则循环无出口。"""
    tool = BashTool(timeout_seconds=1)
    result = await tool.execute({"command": "sleep 5"})
    assert result.success is False
    assert result.error == "timeout"
    assert "timeout_seconds" in result.output
    assert "600" in result.output


@pytest.mark.asyncio
async def test_bash_timeout_at_ceiling_suggests_splitting():
    """已经到顶（max=2s 的构造，sleep 3 真超时）就别再劝加宽，改劝拆分。

    Q2 迁移: the advice used to end with "run it in the background and poll",
    and this harness has no such mechanism -- the model was told to do something
    it cannot do, and would then sit out the iteration budget trying. The text
    now says so, and the assertion follows the text.
    """
    tool = BashTool(timeout_seconds=1, max_timeout_seconds=2)
    result = await tool.execute({"command": "sleep 3", "timeout_seconds": 999})
    assert result.success is False
    assert result.error == "timeout"
    lowered = result.output.lower()
    assert "ceiling" in lowered
    assert "split" in lowered
    # The old tail was advice the model could not follow; the replacement both
    # drops it and says why (the word itself must stay -- it is the mechanism
    # that is missing, not the sentence).
    assert "run it in the background and poll" not in lowered
    assert "no background-job mechanism" in lowered


@pytest.mark.asyncio
async def test_bash_invalid_timeout_is_rejected():
    """0、负数、字符串、布尔都不是合法秒数：显式拒绝，不静默吞掉。"""
    tool = BashTool()
    for bad in (0, -5, "abc", True):
        result = await tool.execute({"command": "echo hi", "timeout_seconds": bad})
        assert result.success is False, bad
        assert result.error == "invalid_params", bad
        assert "timeout_seconds" in result.output, bad


def test_bash_schema_advertises_per_call_timeout():
    """旋钮不写进 schema 就等于不存在：模型看不到就不会用。"""
    schema = BashTool().params_json_schema
    assert "timeout_seconds" in schema["properties"]
    assert "timeout_seconds" not in schema.get("required", [])
