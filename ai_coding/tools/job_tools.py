"""D-1: the two things a model needs after it starts something long.

``job_list`` answers "what is still running"; ``job_kill`` answers "stop that".
Both belong to a family: see ``history_search_tool.py`` for why the corpus of a
workspace is not a session's private property, and ``core/jobs.py`` for the
lifecycle.

dsh's job family has a third tool, ``job_output(job_id, wait, timeout_ms)``, and
it was left out on purpose. A cursor, a blocking wait and a timeout is the
heaviest part of that surface, and two cheaper answers already cover the
questions it answers: the completion notice carries the tail of the log, and
every row here carries three lines of it. What is genuinely missing, and is
recorded as a gap rather than pretended away, is *blocking* -- a model that needs
a result before it can answer ends its turn and reads the answer in the notice.

The other thing every message here is careful about is that a job in this
workspace may belong to another session: there is no fencing (dsh kills a
session's jobs when it stops; these outlive the session by design), so a row
from an earlier run is labelled as inherited rather than presented as the
model's own work.
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.jobs import (
    LIST_TAIL_LINES,
    NOTICE_TAIL_LINES,
    STATUS_RUNNING,
    WAIT_CANCELLED,
    WAIT_TIMEOUT,
    JobIdentityUnverifiable,
    JobRegistry,
)
from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["JobListTool", "JobKillTool", "JobOutputTool"]

#: Settled rows in one listing. The listing is a status board, not a log.
_TIMEOUT_GRACE = 5.0
_SETTLED_ROWS = 20
_ROW_CHARS = 160
_TAIL_CHARS = 120


def _one_line(text: str, limit: int = _ROW_CHARS) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _registry(ctx: ToolContext | None) -> JobRegistry | None:
    """The registry, or ``None`` where jobs were never wired.

    ``None`` is a fact worth reporting rather than an empty answer: it means the
    tool stack has no job table at all, which is not the same as "no jobs".
    """
    context = resolve_ctx(ctx)
    registry = getattr(context, "jobs", None)
    return registry if isinstance(registry, JobRegistry) else None


def _no_registry(name: str) -> ToolResult:
    return ToolResult(
        success=False,
        output=(
            f"error: {name} has no job registry attached here "
            f"(this tool stack was built without background jobs)"
        ),
        error="no_jobs",
    )


class JobListTool(BaseTool):
    """What is running, what finished, and where its output is."""

    parallel_safe = True
    #: Reading a status board changes nothing.
    mutates_world = False

    @property
    def name(self) -> str:
        return "job_list"

    @property
    def description(self) -> str:
        return (
            "List the background jobs in this workspace: id, command, status, how "
            "long, where the output is, and the last few lines each one wrote. Use "
            "it to check on something you started with bash "
            "run_in_background, and to find the id of a job that finished or that "
            "an earlier session left running. Jobs here belong to the workspace, "
            "not to this conversation, so one may be a dev server or a build "
            "somebody else started."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        registry = _registry(ctx)
        if registry is None:
            return _no_registry(self.name)

        jobs = registry.list()
        if not jobs:
            return ToolResult(success=True, output="no background job in this workspace")

        running = [job for job in jobs if job.status == STATUS_RUNNING]
        settled = [job for job in jobs if job.status != STATUS_RUNNING]
        older = max(0, len(settled) - _SETTLED_ROWS)
        shown = running + settled[:_SETTLED_ROWS]

        lines: list[str] = []
        for job in shown:
            owner = "inherited from an earlier run" if job.adopted else job.origin_session[:12]
            origin = f"{job.id} [{owner}]"
            head = f"{origin} {job.status} pid {job.pid} · {_one_line(job.command)}"
            detail = job.detail or ""
            if detail:
                head += f" · {detail}"
            lines.append(head)
            lines.append(f"    log: {job.log_path}")
            tail = registry.read_tail(job, LIST_TAIL_LINES)
            if tail:
                lines.extend(f"    | {row[:_TAIL_CHARS]}" for row in tail.splitlines())
        if older:
            lines.append(f"({older} older finished job(s) not shown; the ledger still holds them)")
        return ToolResult(
            success=True,
            output="\n".join(lines),
            data={
                "jobs": [
                    {
                        "id": job.id,
                        "status": job.status,
                        "pid": job.pid,
                        "log_path": job.log_path,
                        "adopted": job.adopted,
                    }
                    for job in jobs[:_SETTLED_ROWS]
                ],
                "shown": len(shown),
                "older_finished": older,
            },
        )


class JobKillTool(BaseTool):
    """Stop a background job by id."""

    parallel_safe = True
    #: It ends a process: a side effect on the world, so plan mode keeps its
    #: hands off it (``bash`` itself is unavailable there for the same reason).
    mutates_world = True

    @property
    def name(self) -> str:
        return "job_kill"

    @property
    def description(self) -> str:
        return (
            "Stop a running background job by the id job_list or the bash call "
            "that started it. Give a short reason: it is recorded and read back "
            "to you, so 'the build is superseded' beats 'killed'. Killing a job "
            "that already finished reports how it ended instead of pretending. "
            "Jobs from an earlier run can be stopped only when this platform can "
            "prove the pid is still that job."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job id, as job_list or the bash call reported it.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why it is being stopped; recorded and shown back to you.",
                },
            },
            "required": ["job_id"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        registry = _registry(ctx)
        if registry is None:
            return _no_registry(self.name)
        job_id = str(params.get("job_id", "") or "").strip()
        if not job_id:
            return ToolResult(
                success=False,
                output="error: 'job_id' is required (job_list shows the ids)",
                error="empty_job_id",
            )
        reason = str(params.get("reason", "") or "").strip() or "no reason given"
        try:
            job = registry.kill(job_id, reason=reason)
        except KeyError:
            return ToolResult(
                success=False,
                output=(
                    f"error: no background job {job_id!r} in this workspace; "
                    f"job_list shows the ones that exist"
                ),
                error="unknown_job",
            )
        except JobIdentityUnverifiable as exc:
            return ToolResult(success=False, output=f"error: {exc}", error="unverifiable_job")

        if job.status != "killed":
            return ToolResult(
                success=True,
                output=(
                    f"job {job_id} was already {job.status}"
                    f"{f' ({job.detail})' if job.detail else ''}; nothing to stop"
                ),
                data={"job_id": job_id, "status": job.status},
            )
        return ToolResult(
            success=True,
            output=f"job {job_id} stopped · {job.detail} · log: {job.log_path}",
            data={"job_id": job_id, "status": job.status, "reason": reason},
        )


#: Ceiling on one wait, matching the turn clock: a tool call that may hold a
#: turn may not hold it forever.  dsh caps at ten minutes too
#: (``maxWaitTimeoutMs``, tool-jobs/src/index.ts:61).
MAX_JOB_WAIT_SECONDS = 600
#: dsh's default wait (``waitTimeoutMs``): long enough for a test suite, short
#: enough that the model does not sit on a turn for a whole build.
DEFAULT_JOB_WAIT_SECONDS = 30
#: A peek reads a bounded tail and returns; the dispatcher only needs a ceiling.
_PEEK_SECONDS = 5.0


class JobOutputTool(BaseTool):
    """Read a job's output, and optionally hold the turn until it ends."""

    parallel_safe = True
    #: Reading is not a side effect; ``wait`` blocks, it does not act.
    mutates_world = False

    @property
    def name(self) -> str:
        return "job_output"

    @property
    def description(self) -> str:
        return (
            "Read one background job: its status, its exit code, and the tail of "
            "its output. Pass wait: true to hold this turn until the job ends or "
            "your timeout runs out -- use it only when you genuinely cannot "
            "continue without the result, because it spends the turn. A wait that "
            "times out leaves the job running; it never stops it. For more than "
            "the last few lines, read the log path with the read tool."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job id, as job_list reports it."},
                "wait": {
                    "type": "boolean",
                    "description": (
                        "Hold this turn until the job finishes or the timeout "
                        "expires. Defaults to false."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        f"How long to wait, default {DEFAULT_JOB_WAIT_SECONDS}s, "
                        f"capped at {MAX_JOB_WAIT_SECONDS}s."
                    ),
                },
            },
            "required": ["job_id"],
        }

    @property
    def max_duration_seconds(self) -> float:
        """The widest wait this tool will honour, so the dispatcher cannot cut
        the model's wait short and call it a timeout."""
        return float(MAX_JOB_WAIT_SECONDS) + _TIMEOUT_GRACE

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        registry = _registry(ctx)
        if registry is None:
            return _no_registry(self.name)
        context = resolve_ctx(ctx)
        job_id = str(params.get("job_id", "") or "").strip()
        if not job_id:
            return ToolResult(
                success=False,
                output="error: 'job_id' is required (job_list shows the ids)",
                error="empty_job_id",
            )
        job = registry.get(job_id)
        if job is None:
            return ToolResult(
                success=False,
                output=f"error: no background job {job_id!r} in this workspace",
                error="unknown_job",
            )

        wait = params.get("wait") is True
        lines = [f"{job.id} {job.status} pid {job.pid} · {_one_line(job.command)}"]
        if job.detail:
            lines.append(f"    {job.detail}")
        tail = registry.read_tail(job, NOTICE_TAIL_LINES)
        if tail:
            lines.extend(f"    | {row}" for row in tail.splitlines())
        body = "\n".join(lines)

        if not wait:
            return ToolResult(
                success=True,
                output=body + f"\n    (not waiting; log: {job.log_path})",
                data={"job_id": job.id, "status": job.status, "waited": False},
            )

        if job.adopted:
            # Nothing in this process watches an inherited job, so its `done` can
            # never be set here: waiting would burn the full timeout to learn
            # nothing. Saying so beats a wait that always times out.
            return ToolResult(
                success=False,
                output=(
                    f"error: job {job_id} was started by an earlier run and this "
                    f"process does not watch it, so waiting here would never be "
                    f"woken. Its status is above; re-check with job_list, or read "
                    f"the log directly."
                ),
                error="adopted_job",
            )

        effective = DEFAULT_JOB_WAIT_SECONDS
        if params.get("timeout_seconds") is not None:
            try:
                effective = min(
                    max(1, int(params["timeout_seconds"])), MAX_JOB_WAIT_SECONDS
                )
            except (TypeError, ValueError):
                effective = DEFAULT_JOB_WAIT_SECONDS
        outcome = await registry.wait(
            job, timeout=effective, cancel=context.kill_event, kill_on_cancel=False
        )
        data = {
            "job_id": job.id,
            "status": job.status,
            "waited": True,
            "timeout_seconds": effective,
        }
        if outcome == WAIT_CANCELLED:
            return ToolResult(
                success=False,
                output=(
                    "stopped waiting (cancelled by the user); the job was NOT "
                    "killed and is still running"
                ),
                error="cancelled",
                data=data,
            )
        if outcome == WAIT_TIMEOUT:
            return ToolResult(
                success=True,
                output=(
                    f"{body}\n    still running after waiting {effective}s -- it "
                    f"was NOT killed. Keep working on something else, or stop it "
                    f"with job_kill."
                ),
                data={**data, "still_running": True},
            )
        settled_tail = registry.read_tail(job, NOTICE_TAIL_LINES)
        settled_lines = [f"{job.id} {job.status} pid {job.pid} · {_one_line(job.command)}"]
        if job.detail:
            settled_lines.append(f"    {job.detail}")
        if settled_tail:
            settled_lines.extend(f"    | {row}" for row in settled_tail.splitlines())
        return ToolResult(
            success=True,
            output="\n".join(settled_lines) + f"\n    (log: {job.log_path})",
            data={**data, "exit_code": job.exit_code},
        )
