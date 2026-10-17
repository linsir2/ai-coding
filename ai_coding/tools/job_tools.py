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
    STATUS_RUNNING,
    JobIdentityUnverifiable,
    JobRegistry,
)
from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["JobListTool", "JobKillTool"]

#: Settled rows in one listing. The listing is a status board, not a log.
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
