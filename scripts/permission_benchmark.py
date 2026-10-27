#!/usr/bin/env python3
"""M-G4 permission gate benchmark -- the rule chain, against a frozen corpus.

Why this exists next to ``redline_benchmark``: that one freezes a single judge,
``bash_red_line_with_fallback``, which is only the first step of ``check()``.  The
gate is five steps -- hard tier, agent veto, the rule chain, a saved grant, the
workspace edge -- and the risk that matters lives in the other four.  This
benchmark drives the real ``check()`` over a corpus whose verdicts were copied
out of a run, so a change that reopens an agent veto, lets a grant outlive its
scope, or stops asking about an outside path shows up as a moved verdict.

The honest framing, same as the red line benchmark: this cannot prove the gate is
correct.  It makes the two directions comparable -- no new false negative inside
the corpus, no new false positive -- and counts the known gaps so a later fix is
visible as a number dropping to zero.

    python scripts/permission_benchmark.py          # metrics table, exit 0/1
    python scripts/permission_benchmark.py --quiet  # the table only

Exit codes: 0 when every verdict matches its frozen value, FN and FP are both
zero, and the corpus still meets its size floors; 1 otherwise.  CI runs it.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.security.permission_corpus import (  # noqa: E402
    ALLOW_LABELS,
    CORPUS,
    DENY_LABELS,
    FLOORS,
    KNOWN_FP,
    KNOWN_MISS,
    MIN_CORPUS,
    MIN_KNOWN_FP,
    MIN_KNOWN_MISS,
    PermissionCase,
)
from ai_coding.security.permission_gate import PermissionGate  # noqa: E402

#: The class names the report prints, in the order the chain runs.
LABELS = (
    "deny_hard_tier",
    "deny_agent_veto",
    "deny_rule_chain",
    "allow_rule_chain",
    "allow_grant_answers",
    "allow_safe",
    "warn_asks",
    "warn_outside_workspace",
    "known_miss",
    "known_fp",
)


def rule_of(reason: str) -> str:
    """The rule a verdict came from, without its resource-shaped tail.

    Mirrors ``redline_benchmark.rule_of``: ``bash command blocked: recursive
    root deletion (rm -rf /)`` and ``... ('rm -rf /')`` are the same rule.
    """
    if not reason:
        return ""
    return reason.split(" (", 1)[0]


def judge(case: PermissionCase) -> tuple[str, str]:
    """``(decision name, rule)`` for one case, from a gate built for that case.

    ``PermissionGate`` is fully keyword-only with defaults, so no app config is
    needed -- the row carries the configuration it is testing.  The name, not the
    value: ``PermissionDecision.value`` is lower case and the corpus is frozen in
    upper case.
    """
    gate = PermissionGate(
        grants=case.grants,
        denied_tools=case.denied_tools,
        allowed_tools=case.allowed_tools,
        skill_permissions=dict(case.skill_permissions),
        workspace=case.workspace,
        rules=case.rules,
    )
    decision = gate.check(
        case.tool,
        dict(case.params),
        session_rules=case.session_rules,
        agent_rules=case.agent_rules,
        posture=case.posture,
    )
    return decision.decision.name, rule_of(decision.reason)


class Metrics:
    """Everything the benchmark measured, in one place.

    ``cases`` is a parameter rather than a module global so the fn/fp counting can
    be exercised with synthetic rows: on a clean corpus both are zero, and a
    swapped pair would be invisible.
    """

    def __init__(self, cases: tuple[PermissionCase, ...] = CORPUS) -> None:
        cases = tuple(cases)  # a generator would break `len` and `set`
        self.total = len(cases)
        self.mismatches: list[tuple[PermissionCase, str, str]] = []
        self.by_class: Counter[str] = Counter()
        self.by_rule: Counter[str] = Counter()
        #: ``(case, measured verdict)`` for everything this run judged.
        self.measured: list[tuple[PermissionCase, str]] = []
        #: Rows whose disagreement is expected and counted separately.  Putting
        #: them in `mismatches` would make the benchmark permanently red, which
        #: is how a known gap stops being visible.  Membership by equality, not
        #: `id`: a rebuilt row (dataclasses.replace, JSON reload) is still the
        #: same row.
        known_gap = frozenset((*KNOWN_MISS, *KNOWN_FP))
        self.known_gap_count = sum(1 for case in cases if case in known_gap)
        for case in cases:
            verdict, rule = judge(case)
            self.measured.append((case, verdict))
            self.by_class[case.label] += 1
            # Empty rules are not a bucket: the gate's two ALLOW exits both
            # return an empty reason, so they would all pile into "".
            if rule:
                self.by_rule[rule] += 1
            if case in known_gap:
                continue
            # `rule` is compared only where the corpus pins one -- a row that
            # cares which layer decided, not just what was decided.
            if verdict != case.verdict or (case.rule and rule != case.rule):
                self.mismatches.append((case, case.verdict, verdict))

        # Counted from the label, not from the module-level tuples: a synthetic
        # row built in a test must be counted the same way as a real one.
        self.known_miss_still_open = [
            case
            for case, verdict in self.measured
            if case.label == "known_miss" and verdict != case.verdict
        ]
        self.known_fp_still_open = [
            case
            for case, verdict in self.measured
            if case.label == "known_fp" and verdict != case.verdict
        ]
        # FN/FP come from the verdict, so they stay meaningful even when the
        # golden comparison above has already failed.  Both compare the *frozen*
        # direction against what the gate said: a row frozen DENY that now allows
        # is a false negative, a row frozen ALLOW that now denies is a false
        # positive.
        self.fn = sum(
            1
            for case, verdict in self.measured
            if case.label in DENY_LABELS
            and case.verdict == "DENY"
            and verdict != "DENY"
        )
        self.fp = sum(
            1
            for case, verdict in self.measured
            if case.label in ALLOW_LABELS
            and case.verdict == "ALLOW"
            and verdict == "DENY"
        )
        self.problems = self._corpus_problems()

    def _corpus_problems(self) -> list[str]:
        """A corpus thinned to nothing would pass vacuously; refuse that.

        Floors are **per class**: an aggregate floor lets one class shrink to
        nothing while another grows past it, and the agent-veto class is the one
        whose test would then loop over an empty tuple and pass.
        """
        problems: list[str] = []
        for label, floor in FLOORS.items():
            actual = sum(1 for case in CORPUS if case.label == label)
            if actual < floor:
                problems.append(f"{label} = {actual}, need >= {floor}")
        if len(KNOWN_MISS) < MIN_KNOWN_MISS:
            problems.append(f"known_miss = {len(KNOWN_MISS)}, need >= {MIN_KNOWN_MISS}")
        if len(KNOWN_FP) < MIN_KNOWN_FP:
            problems.append(f"known_fp = {len(KNOWN_FP)}, need >= {MIN_KNOWN_FP}")
        if len(CORPUS) < MIN_CORPUS:
            problems.append(f"corpus = {len(CORPUS)}, need >= {MIN_CORPUS}")
        # The whole row, not (tool, params): the same call under a different gate
        # configuration is a different case, and the corpus leans on that.
        if len(set(CORPUS)) != len(CORPUS):
            problems.append("duplicate corpus rows")
        return problems

    @property
    def ok(self) -> bool:
        return not (self.mismatches or self.fn or self.fp or self.problems)


def format_metrics(metrics: Metrics) -> str:
    """The Markdown table, ready to paste into a PR or the data contracts."""
    golden = metrics.total - metrics.known_gap_count - len(metrics.mismatches)
    comparable = metrics.total - metrics.known_gap_count
    lines = [
        "# M-G4 permission gate benchmark",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 语料总数 | {metrics.total} |",
        f"| golden 一致 | {golden}/{comparable} |",
        f"| FN（该拒未拒） | {metrics.fn} |",
        f"| FP（普通操作误拦） | {metrics.fp} |",
        f"| known_miss（仍漏报，按判定输出） | {len(metrics.known_miss_still_open)} |",
        f"| known_fp（仍误报，按判定输出） | {len(metrics.known_fp_still_open)} |",
        "",
        "## 分类计数",
        "",
        "| 类别 | 条数 |",
        "|---|---|",
    ]
    for label in LABELS:
        lines.append(f"| {label} | {metrics.by_class[label]} |")

    lines += ["", "## 按规则分类的命中", "", "| 规则 | 命中数 |", "|---|---|"]
    for rule, count in sorted(
        metrics.by_rule.items(), key=lambda row: (-row[1], row[0])
    ):
        lines.append(f"| {rule} | {count} |")

    if metrics.known_miss_still_open:
        lines += ["", "## known_miss（缺口可见，修好即降到 0）", ""]
        for case in metrics.known_miss_still_open:
            lines.append(f"- `{case.tool}` {dict(case.params)}")

    if metrics.mismatches:
        lines += [
            "",
            "## 与冻结值不符（回归检测的本体）",
            "",
            "| 工具 | 参数 | 冻结值 | 现在 |",
            "|---|---|---|---|",
        ]
        for case, frozen, now in metrics.mismatches:
            lines.append(f"| `{case.tool}` | `{dict(case.params)}` | `{frozen}` | `{now}` |")

    if metrics.problems:
        lines += ["", "## 语料规模", ""]
        lines += [f"- {problem}" for problem in metrics.problems]

    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="print the table only, without the trailing verdict line",
    )
    args = parser.parse_args(argv)

    metrics = Metrics()
    print(format_metrics(metrics))
    if not args.quiet:
        print(
            "permission gate benchmark OK"
            if metrics.ok
            else "permission gate benchmark FAILED"
        )
    return 0 if metrics.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
