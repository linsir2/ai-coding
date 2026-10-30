"""Parallel research fan-out (B0).

A fan-out is a small orchestration layer, not a second sub-agent runtime: it
hands one brief to N `explore` children through the existing
:class:`~ai_coding.core.subagent.SubAgentRunner`, waits for their packets, and
turns them into one deterministic report.  The runner owns the semaphore,
timeout and usage; this module owns the question shape, the read-only window,
and the merge.  Children run in the *live* working tree, never a HEAD worktree:
they are readonly, and research must see the code the user is editing.

Why a separate module rather than code inside the tool: the plan executor's
research wave (C3) needs exactly the same fan-out, and a pure function over
questions is testable without a dispatcher.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.subagent import SUCCESS_STATUSES, ParentContext, SubagentResult

__all__ = [
    "CHILD_TIMEOUT_GRACE",
    "RESEARCH_SCHEMA",
    "ResearchFanOut",
    "ResearchPacket",
    "ResearchQuestion",
    "render_report",
    "total_usage",
]

#: A child returns exactly these keys; anything else is ignored, and the raw
#: conclusion is kept as a fallback so a bad brace never loses the work.
RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_files": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_files"],
}

SUMMARY_MAX = 600
FILES_MAX = 8
FINDINGS_MAX = 5
MERGED_MAX = 8_000

#: Room above each child's own turn budget for ledger write, the packet and the
#: small setup before its first model call.  The tool multiplies it by the
#: number of semaphore lanes (see ``ParallelResearchTool.max_duration_seconds``);
#: it is one constant so the two ceilings cannot disagree.
CHILD_TIMEOUT_GRACE = 15.0


@dataclass(frozen=True)
class ResearchQuestion:
    """One independent question and where the child should look."""

    id: str
    question: str
    scope: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchPacket:
    """What one child found, in the shape the tool returns."""

    id: str
    status: str
    summary: str = ""
    key_files: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    usage: dict[str, int] | None = None
    error: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "summary": self.summary,
            "key_files": list(self.key_files),
            "findings": list(self.findings),
            "usage": dict(self.usage) if self.usage is not None else None,
            "error": self.error,
            "truncated": self.truncated,
        }


class ResearchFanOut:
    """Run N read-only children concurrently and return their packets."""

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    async def run(
        self,
        questions: list[ResearchQuestion],
        *,
        shared_context: str = "",
        parent: ParentContext | None = None,
        observer: Any = None,
        parent_span: str = "",
    ) -> list[ResearchPacket]:
        """Run every question in parallel; results keep the question order."""
        tasks = [
            self._run_one(
                question,
                shared_context=shared_context,
                parent=parent,
                observer=observer,
                parent_span=parent_span,
            )
            for question in questions
        ]
        return list(await asyncio.gather(*tasks))

    async def _run_one(
        self,
        question: ResearchQuestion,
        *,
        shared_context: str,
        parent: ParentContext | None,
        observer: Any,
        parent_span: str,
    ) -> ResearchPacket:
        brief = _brief(question, shared_context)
        try:
            result = await self._runner.run(
                brief,
                f"research:{question.id}",
                # A read-only child has nothing to isolate *from*: `readonly`
                # already removes every mutating tool, and a worktree would
                # hand it the HEAD snapshot instead of the working tree -- so
                # research about code the user has not committed yet would
                # answer "this file does not exist" (observed on a real run).
                isolate=False,
                parent=parent,
                parent_span=parent_span,
                observer=observer,
                output_schema=RESEARCH_SCHEMA,
                agent="explore",
                context_mode="task_only",
                readonly=True,
            )
        except Exception as exc:  # noqa: BLE001 — one child must not sink the fan-out
            return ResearchPacket(
                id=question.id,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
        return _packet(question.id, result)


def _brief(question: ResearchQuestion, shared_context: str) -> str:
    """The child's whole world in ``task_only`` mode."""
    lines = ["你是只读调研子代理（explore）。不要写文件、不要运行命令。"]
    if shared_context:
        lines.append(f"背景/约束：{shared_context}")
    lines.append(f"问题：{question.question}")
    if question.scope:
        lines.append("优先范围：" + ", ".join(question.scope))
    lines.append(
        "只返回 JSON：summary（结论）、key_files（仓库相对路径）、findings（要点列表）。"
    )
    return "\n".join(lines)


def _packet(question_id: str, result: SubagentResult) -> ResearchPacket:
    status = str(result.status or "error")
    facts = result.facts if isinstance(result.facts, dict) else {}
    summary = str(facts.get("summary") or result.conclusion or "").strip()
    cut = False
    if len(summary) > SUMMARY_MAX:
        summary = summary[: SUMMARY_MAX - 1] + "…"
        cut = True
    raw_files = facts.get("key_files") or []
    key_files: tuple[str, ...] = ()
    if isinstance(raw_files, list):
        kept = tuple(str(path) for path in raw_files if str(path).strip())
        key_files = kept[:FILES_MAX]
        cut = cut or len(kept) > FILES_MAX
    raw_findings = facts.get("findings") or []
    findings: tuple[str, ...] = ()
    if isinstance(raw_findings, list):
        kept_findings = tuple(str(item) for item in raw_findings if str(item).strip())
        findings = kept_findings[:FINDINGS_MAX]
        cut = cut or len(kept_findings) > FINDINGS_MAX
    # A success that stopped at its iteration ceiling is not an error line: the
    # exit reason is only a *cause* when the packet itself failed.
    reason = str(result.error or result.exit_reason or "")
    return ResearchPacket(
        id=question_id,
        status=status,
        summary=summary,
        key_files=key_files,
        findings=findings,
        usage=dict(result.usage) if isinstance(result.usage, dict) else None,
        error="" if status in SUCCESS_STATUSES else reason,
        # B0 review: a cap this module applied is a truncation the reader must
        # see.  Before, only the child's own ``truncated`` flag was carried, so
        # a 600-char summary and a dropped third finding looked complete.
        truncated=bool(result.truncated) or cut,
    )


def render_report(packets: list[ResearchPacket]) -> str:
    """One readable markdown report; files are deduped across questions."""
    lines: list[str] = []
    seen_files: list[str] = []
    for packet in packets:
        head = f"## {packet.id} · {packet.status}"
        if packet.truncated:
            head += "（已截断）"
        lines.append(head)
        if packet.summary:
            lines.append(packet.summary)
        for finding in packet.findings:
            lines.append(f"- {finding}")
        if packet.error:
            lines.append(f"错误：{packet.error}")
        for path in packet.key_files:
            if path not in seen_files:
                seen_files.append(path)
    if seen_files:
        lines.append("相关文件：")
        lines.append(", ".join(seen_files))
    report = "\n".join(lines)
    if len(report) > MERGED_MAX:
        report = report[: MERGED_MAX - 1] + "…"
    return report


def total_usage(packets: list[ResearchPacket]) -> dict[str, int] | None:
    """Key-wise sum of every reported usage; ``None`` when nobody reported."""
    total: dict[str, int] | None = None
    for packet in packets:
        if not packet.usage:
            continue
        if total is None:
            total = {}
        for key, value in packet.usage.items():
            try:
                total[key] = total.get(key, 0) + int(value)
            except (TypeError, ValueError):
                continue
    return total
