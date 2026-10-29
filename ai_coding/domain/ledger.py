"""Uncertainty ledger: what the turn does not know, derived (R0-2).

No LLM declarations anywhere in this file: items are built mechanically
from recorded observations (failed acceptance verifies, tool errors,
failure streaks), and an H level only ever drops on recorded evidence —
a later success, a passing verify.  The policy gates on the flat counter
(rounds the open set did not change), never on prose.

Kinds follow the experiment report: ``epistemic`` (reducible by probing),
``aleatoric`` (noise — noted, not chased), ``preference`` (needs a human).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

Level = Literal["high", "medium", "low"]
Kind = Literal["epistemic", "aleatoric", "preference"]

#: Evidence tail per entry: full logs live in the session/trace, not here.
_EVIDENCE_TAIL = 120


@dataclass(frozen=True)
class UncertaintyItem:
    """One open question, with the evidence for its level attached."""

    id: str
    question: str
    kind: Kind = "epistemic"
    level: Level = "medium"
    evidence: tuple[str, ...] = ()
    #: Acceptance ids this question blocks (empty = blocks nothing named).
    resolves: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "kind": self.kind,
            "level": self.level,
            "evidence": list(self.evidence),
            "resolves": list(self.resolves),
        }


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= _EVIDENCE_TAIL else f"…{text[-_EVIDENCE_TAIL:]}"


def build_ledger(
    *,
    failures: Sequence[tuple[str, str, str]],
    verify: Mapping[str, tuple[bool, str]],
    failed_streak: int,
    iteration: int,
    succeeded_tools: frozenset[str],
    stall_threshold: int,
) -> list[UncertaintyItem]:
    """Derive the open set from this turn's recorded observations.

    ``failures`` are ``(tool, error, output excerpt)`` newest-last (the books
    keep them); ``verify`` maps acceptance id -> (passed, output tail).
    ``iteration`` is unused today — it anchors the signature when per-round
    questions arrive (R1-1) — and is kept explicit so the call reads as the
    turn's state, not a subset of it.
    """
    del iteration  # reserved for per-round questions; see docstring.
    items: list[UncertaintyItem] = []
    for ac_id, (passed, output) in verify.items():
        if passed:
            continue
        items.append(UncertaintyItem(
            id=f"U-ac-{ac_id}",
            question=f"验收 {ac_id} 未通过：为什么？",
            kind="epistemic",
            level="high",
            evidence=(_tail(output),) if output else (),
            resolves=(ac_id,),
        ))
    counts: dict[tuple[str, str], int] = {}
    tails: dict[tuple[str, str], str] = {}
    for tool, error, excerpt in failures:
        if tool in succeeded_tools:
            # A later success is the evidence that closes this question —
            # H only ever drops on something recorded.
            continue
        key = (tool, error)
        counts[key] = counts.get(key, 0) + 1
        tails[key] = excerpt
    for (tool, error), count in counts.items():
        hot = count >= 2 or (stall_threshold and failed_streak >= stall_threshold)
        # R21 §3: "反复失败（1 次）" was a grammatical contradiction -- one
        # failure is not a repeat.  Only the wording changes; the level rule
        # stays exactly as it was (``count == 1`` is still medium), because it
        # is what raises the question on the first sighting.
        what = "调用失败" if count == 1 else "反复失败"
        items.append(UncertaintyItem(
            id=f"U-err-{tool}-{error}",
            # The failure code stays in the item id, not in the sentence: this
            # line is human-facing (bridge.py:148 puts it in the transcript and
            # nothing injects it into the model's context), and the evidence
            # below it already carries the real output, so the code would only
            # be noise on screen (A5 §3b).
            question=f"{tool} {what}（{count} 次）：直接原因是什么？",
            kind="epistemic",
            level="high" if hot else "medium",
            evidence=(_tail(tails[tool, error]),),
        ))
    if stall_threshold and failed_streak >= stall_threshold:
        items.append(UncertaintyItem(
            id="U-stall",
            question=f"连续 {failed_streak} 轮工具全败：是缺信息，还是方向错了？",
            kind="epistemic",
            level="high",
        ))
    return items


def ledger_signature(items: Sequence[UncertaintyItem]) -> tuple[tuple[str, str], ...]:
    """Order-stable identity of the open set: sorted ``(id, level)`` pairs."""
    return tuple(sorted((item.id, item.level) for item in items))


__all__ = [
    "Kind",
    "Level",
    "UncertaintyItem",
    "build_ledger",
    "ledger_signature",
]
