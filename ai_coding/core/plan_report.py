"""The plan run's outcome record (R1), shared by both plan paths.

``PlanReport`` is what one ``/plan`` execution did: what completed, what
blocked, what stalled, and how many step-turns it spent.  The Codex path
returns one and the command layer reads ``stopped`` off it — so it lives apart
from the module that used to own it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["PlanReport"]


@dataclass
class PlanReport:
    """What one ``/plan`` execution did, for the console and the caller."""

    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    #: r24/C: items given up because a dependency failed (not user drops).
    skipped: list[str] = field(default_factory=list)
    #: None = ran to the end; otherwise why it stopped early.
    stopped: str | None = None
    #: Step-turns (``process_input`` calls) this run spent (R1-4).
    turns_used: int = 0
    #: Summed model-step usage, or None when the loop reported none (R1-4).
    tokens_used: dict[str, int] | None = None

    def render(self) -> str:
        head = f"[plan] 完成 {len(self.completed)} 步 · 用了 {self.turns_used} 回合"
        if self.tokens_used:
            head += f" / {self.tokens_used.get('total_tokens', '?')} tokens"
        parts = [head]
        if self.completed:
            parts.append("已完成：" + ", ".join(self.completed))
        if self.blocked:
            parts.append("受阻：" + ", ".join(self.blocked))
        if self.skipped:
            parts.append("跳过（依赖失败）：" + ", ".join(self.skipped))
        if self.stalled:
            parts.append("停滞（下次可重跑）：" + ", ".join(self.stalled))
        if self.stopped:
            parts.append(f"止于：{self.stopped}")
        return "\n".join(parts)
