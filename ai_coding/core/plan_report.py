"""The plan run's outcome record (R1), shared by both plan paths.

``PlanReport`` is what one ``/plan`` execution did: what completed, what
blocked, what stalled, and how many step-turns it spent.  The legacy runner
returns it, the Codex path returns an empty one, and the command layer reads
``stopped`` off either — so it lives apart from the module that used to own it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_coding.domain.plan_delivery import StepDelivery

__all__ = ["PlanReport"]


@dataclass
class PlanReport:
    """What one ``/plan`` execution did, for the console and the caller."""

    completed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    #: r24/C: items given up because a dependency failed (not user drops).
    skipped: list[str] = field(default_factory=list)
    deliveries: list[StepDelivery] = field(default_factory=list)
    #: None = ran to the end; otherwise why it stopped early.
    stopped: str | None = None
    #: Step-turns (``process_input`` calls) this run spent (R1-4).
    turns_used: int = 0
    #: Summed model-step usage, or None when the loop reported none (R1-4).
    tokens_used: dict[str, int] | None = None

    def render(self) -> str:
        # R21 §4: the headline counts every completion and names how many of
        # them were not settled step by step.  Counting only attested steps
        # (R1-5b) was meant to avoid the old lie ("完成 4 步" for 0 turns), but
        # it made the sentence disagree, on one screen, with the task card's
        # ``Todos n/m`` and with the ``已完成`` line right below it (measured:
        # "完成 1 步" beside "Todos 2/2" and "已完成：1, 2").  The qualifier is
        # what keeps the provenance visible without contradicting the list.
        unattested = sum(1 for entry in self.deliveries if not entry.attested)
        qualifier = f"（其中 {unattested} 步未经分步验收）" if unattested else ""
        head = f"[plan] 完成 {len(self.completed)} 步{qualifier}"
        # A closing Chinese bracket already separates the clauses, so the
        # separator loses its leading space when the qualifier is present.
        head += f"{'' if qualifier else ' '}· 用了 {self.turns_used} 回合"
        if self.tokens_used:
            head += f" / {self.tokens_used.get('total_tokens', '?')} tokens"
        parts = [head]
        on_claim = [entry.step_id for entry in self.deliveries if entry.proof == "on-claim"]
        if on_claim:
            parts.append(f"(含 {len(on_claim)} 步无证明完成：{', '.join(on_claim)})")
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
