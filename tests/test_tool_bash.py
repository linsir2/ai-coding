"""TDD (M2-1): bash tool."""

import os
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


# ---- D-1: the same command, without waiting for it -------------------------


def test_bash_schema_advertises_the_background_switch():
    """An unwritten knob is a knob the model cannot find."""
    schema = BashTool().params_json_schema
    assert "run_in_background" in schema["properties"]
    assert "run_in_background" not in schema.get("required", [])


def test_bash_description_says_it_will_be_notified_and_not_to_poll():
    """Every reference harness spends a sentence on this, and Claude Code learned
    it the hard way: a model that polls a job is a model that lies about waiting."""
    description = BashTool().description
    assert "notified" in description
    assert "poll" in description


@pytest.mark.asyncio
async def test_a_background_call_returns_an_id_and_a_log_path_at_once(tmp_path):
    """The whole contract: it returns while the command is still running."""
    from ai_coding.core.jobs import JobRegistry

    registry = JobRegistry(root=tmp_path / "jobs")
    ctx = ToolContext(jobs=registry, workspace=str(tmp_path))

    result = await BashTool().execute(
        {"command": "sleep 30", "run_in_background": True}, ctx
    )

    assert result.success is True
    assert result.data["job_id"].startswith("job_")
    assert str(tmp_path / "jobs") in result.output
    assert "notified" in result.output or "notify" in result.output
    registry.kill(result.data["job_id"], reason="test over")


@pytest.mark.asyncio
async def test_the_timeout_knob_does_not_apply_to_a_background_command(tmp_path):
    """A background call that inherited the 60s foreground default would be the
    quietest bug in this feature: the model asked for a job and got a kill."""
    from ai_coding.core.jobs import JobRegistry

    registry = JobRegistry(root=tmp_path / "jobs")
    ctx = ToolContext(jobs=registry, workspace=str(tmp_path))

    result = await BashTool().execute(
        {"command": "sleep 5", "run_in_background": True, "timeout_seconds": 1}, ctx
    )

    import time

    time.sleep(1.5)
    job = next(row for row in registry.list() if row.id == result.data["job_id"])
    try:
        assert job.status == "running", "the per-call timeout must not reach a background job"
    finally:
        registry.kill(job.id, reason="test over")


@pytest.mark.asyncio
async def test_a_background_call_without_a_registry_says_so_rather_than_blocking(tmp_path):
    """No registry means no background. Running it in the foreground anyway
    would be the quiet version of the bug this project keeps refusing: the model
    asked for a job, believes it has one, and is actually blocking a turn on it."""
    ctx = ToolContext(workspace=str(tmp_path))

    result = await BashTool().execute(
        {"command": "echo foreground-fallback", "run_in_background": True}, ctx
    )

    assert result.success is False
    assert result.error == "no_jobs"
    assert "foreground" in result.output, "the model needs to know what to do instead"


# ---- D-1b: every bash call is a job (dsh's shape) -------------------------
#
# dsh registers every call as a job and the foreground call merely waits on its
# own; when the wait runs out the job stays alive and the model gets the id. That
# is only cheap there because the job *is* the executor. The payoff here is the
# same one they bought it for: a 9-minute build that hits the timeout is not
# thrown away.


def _jobs(tmp_path: Path):
    from ai_coding.core.jobs import JobRegistry

    return JobRegistry(root=tmp_path / "jobs")


def _ctx_with_jobs(tmp_path: Path, registry, kill_event=None):
    return ToolContext(
        jobs=registry, workspace=str(tmp_path), kill_event=kill_event, call_id="c1"
    )


@pytest.mark.asyncio
async def test_a_command_that_finishes_in_time_leaves_no_ledger_and_no_log(tmp_path):
    """dsh removes the record when a foreground call settles before its wait ends
    (``tool-bash/src/index.ts``). One `ls` per keystroke must not leave a file
    behind, and job_list must not become a second transcript."""
    registry = _jobs(tmp_path)
    ctx = _ctx_with_jobs(tmp_path, registry)

    result = await BashTool().execute({"command": "echo quick"}, ctx)

    assert result.success is True
    assert "quick" in result.output
    assert registry.list() == []
    assert not list((tmp_path / "jobs").glob("job_*"))


@pytest.mark.asyncio
async def test_a_command_that_times_out_is_promoted_and_keeps_running(tmp_path):
    """The whole point: the timeout stops waiting, not the work."""
    registry = _jobs(tmp_path)
    ctx = _ctx_with_jobs(tmp_path, registry)

    result = await BashTool(timeout_seconds=1).execute({"command": "sleep 30"}, ctx)

    assert result.success is True, "a promoted job is not a failure"
    job_id = result.data["job_id"]
    running = [job for job in registry.list() if job.id == job_id]
    assert running and running[0].status == "running"
    try:
        os.kill(running[0].pid, 0)
    except (ProcessLookupError, PermissionError):
        raise AssertionError("the command was killed instead of promoted") from None
    registry.kill(job_id, reason="test over")


@pytest.mark.asyncio
async def test_a_promoted_command_names_the_job_instead_of_advising_a_retry(tmp_path):
    """The old text taught "retry with a larger timeout", which is advice for a
    harness that has no background jobs. This one has them now, so the advice
    would be worse than useless: it sends the model around the same build again."""
    registry = _jobs(tmp_path)

    result = await BashTool(timeout_seconds=1).execute(
        {"command": "sleep 30"}, _ctx_with_jobs(tmp_path, registry)
    )

    assert result.data["job_id"] in result.output
    assert "notified" in result.output or "still running" in result.output
    assert "retry with a larger timeout" not in result.output
    registry.kill(result.data["job_id"], reason="test over")


@pytest.mark.asyncio
async def test_the_foreground_result_keeps_its_exit_code_under_the_job_path(tmp_path):
    registry = _jobs(tmp_path)

    result = await BashTool().execute(
        {"command": "exit 7"}, _ctx_with_jobs(tmp_path, registry)
    )

    assert result.success is False
    assert result.error == "nonzero_exit"
    assert result.data["exit_code"] == 7


@pytest.mark.asyncio
async def test_stderr_still_reaches_the_model_under_the_job_path(tmp_path):
    registry = _jobs(tmp_path)

    result = await BashTool().execute(
        {"command": "echo oops 1>&2"}, _ctx_with_jobs(tmp_path, registry)
    )

    assert "oops" in result.output


@pytest.mark.asyncio
async def test_output_is_truncated_at_the_same_limit_as_before(tmp_path):
    """Max output is a context guard; moving the bytes to a file must not turn it
    into "however much the command printed"."""
    registry = _jobs(tmp_path)
    tool = BashTool(max_output_chars=200)

    result = await tool.execute(
        {"command": "seq 1 5000"}, _ctx_with_jobs(tmp_path, registry)
    )

    assert "truncated" in result.output
    assert len(result.output) < 400


@pytest.mark.asyncio
async def test_a_cancelled_foreground_command_ends_the_job_and_says_so(tmp_path):
    """Esc must still stop the command. Promotion makes that more important, not
    less: an uncancelled promoted command outlives the turn by design."""
    import threading

    registry = _jobs(tmp_path)
    cancel = threading.Event()
    cancel.set()  # the user hit Esc before the command produced anything

    result = await BashTool(timeout_seconds=30).execute(
        {"command": "sleep 30"}, _ctx_with_jobs(tmp_path, registry, cancel)
    )

    assert result.success is False
    assert "cancel" in result.output.lower()
    assert all(job.status != "running" for job in registry.list())


@pytest.mark.asyncio
async def test_output_streams_to_the_model_while_the_command_runs(tmp_path):
    """R8c's live tail is what the TUI shows during a long command. Writing to a
    file would silently delete it, so the tool reads its own log back out."""
    registry = _jobs(tmp_path)
    seen: list[str] = []
    ctx = ToolContext(
        jobs=registry,
        workspace=str(tmp_path),
        call_id="c1",
        emit=lambda event: seen.append(getattr(event, "text", "")),
    )

    await BashTool(timeout_seconds=30).execute(
        {"command": "echo streamed; sleep 20"}, ctx
    )

    assert any("streamed" in text for text in seen), seen


@pytest.mark.asyncio
async def test_bash_without_a_registry_keeps_the_pipe_based_foreground_path(tmp_path):
    """The seam dsh keeps too (``tool-bash/src/index.ts``: "Without a registry the
    tool is foreground-only"). A loop built without jobs must still run commands,
    and its timeout must still kill rather than promote -- there is nowhere to
    promote to."""
    cancel_free = ToolContext(workspace=str(tmp_path))
    started = time.monotonic()

    result = await BashTool(timeout_seconds=1).execute({"command": "sleep 30"}, cancel_free)

    assert result.success is False
    assert result.error == "timeout"
    assert time.monotonic() - started < 8.0


class _Notices:
    """The P0 notice queue, so a test can see what would reach the model."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def put(self, text: str) -> bool:
        self.lines.append(text)
        return True

    def pending(self) -> int:
        return len(self.lines)

    def drain(self) -> list[str]:
        taken, self.lines = self.lines, []
        return taken


@pytest.mark.asyncio
async def test_a_foreground_command_that_finishes_in_wait_sends_no_notice(tmp_path):
    """Found live: a foreground command that answered inside its wait produced
    both a ToolResult and a `[notice]` about the same command. The model read the
    same exit code twice, and every long foreground command would train it to
    distrust notices."""
    from ai_coding.core.jobs import JobRegistry

    notices = _Notices()
    registry = JobRegistry(root=tmp_path / "jobs", notices=notices)
    ctx = ToolContext(jobs=registry, workspace=str(tmp_path), call_id="c1")

    result = await BashTool(timeout_seconds=30).execute(
        {"command": "echo answered"}, ctx
    )

    assert "answered" in result.output
    assert notices.pending() == 0, notices.lines


@pytest.mark.asyncio
async def test_a_promoted_command_does_send_its_notice(tmp_path):
    """Promotion is the moment the result stops being in anyone's hands, so the
    notice has to start there -- not before, and not never."""
    from ai_coding.core.jobs import JobRegistry

    notices = _Notices()
    registry = JobRegistry(root=tmp_path / "jobs", notices=notices)
    ctx = ToolContext(jobs=registry, workspace=str(tmp_path), call_id="c1")

    result = await BashTool(timeout_seconds=1).execute({"command": "sleep 5"}, ctx)
    job_id = result.data["job_id"]

    import asyncio
    import time as _time

    deadline = _time.monotonic() + 15
    while notices.pending() == 0 and _time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    assert notices.pending() == 1, "a promoted command must announce its ending"
    assert job_id in notices.lines[0]
