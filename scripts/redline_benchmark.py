#!/usr/bin/env python3
"""M-D2 red line benchmark -- regression detection, scored against a frozen corpus.

Why a benchmark and not more parametrized tests: the corpus in
``security/redline_corpus.py`` is a **golden snapshot**.  Every row carries the
verdict the implementation answered on the day the corpus was written, copied
out of a run rather than inferred from the label.  So a change that lets a
dangerous command through, or starts refusing ordinary work, shows up here as a
moved verdict -- not as a test the author remembered to update.

The honest framing: this cannot prove the filter is safe.  GuardFall showed
that a filter reading the raw command string cannot win in principle, and the
``known_miss`` rows are that hole, listed rather than hidden.  What the
benchmark *can* do is make the two directions comparable: no new false negative
inside the corpus, and no new false positive, with the known gaps counted so a
later fix is visible as a number dropping to zero.

    python scripts/redline_benchmark.py          # metrics table, exit 0/1
    python scripts/redline_benchmark.py --quiet  # the table only

Exit codes: 0 when every verdict matches its frozen value, FN and FP are both
zero, and the corpus still meets its size floors; 1 otherwise.  CI runs it, so
a red build here is a red build, not a note.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.security.bash_redlines import (  # noqa: E402
    bash_red_line_with_fallback,
)
from ai_coding.security.redline_corpus import (  # noqa: E402
    CORPUS,
    DANGEROUS,
    KNOWN_FP,
    KNOWN_MISS,
    MIN_CORPUS,
    MIN_DANGEROUS,
    MIN_KNOWN_MISS,
    MIN_SAFE,
    MIN_UNPARSEABLE,
    SAFE,
    UNPARSEABLE_DENY,
    UNPARSEABLE_WARN,
)

#: The classes that must refuse: a literal red line, and a line the grammar
#: cannot read that carries one.
MUST_DENY = DANGEROUS + UNPARSEABLE_DENY

#: The class that must not refuse: ordinary work, and text that only looks like
#: a red line.
MUST_ALLOW = SAFE


def judge(command: str) -> str:
    """The one judge for a bash command (the merged reader)."""
    return bash_red_line_with_fallback(command)


def rule_of(verdict: str) -> str:
    """The rule a verdict came from, without its command-shaped tail.

    ``recursive root deletion (rm -rf /)`` and ``recursive root deletion
    ('rm -rf /')`` are the same rule reached by two readings, and the table
    below has to say so rather than counting them apart.
    """
    if not verdict:
        return ""
    return verdict.split(" (", 1)[0]


class Metrics:
    """Everything the benchmark measured, in one place."""

    def __init__(self) -> None:
        self.total = len(CORPUS)
        self.mismatches: list[tuple[str, str, str]] = []
        self.by_class: Counter[str] = Counter()
        self.by_rule: Counter[str] = Counter()
        for case in CORPUS:
            verdict = judge(case.command)
            self.by_class[case.label] += 1
            if verdict:
                self.by_rule[rule_of(verdict)] += 1
            if verdict != case.verdict:
                self.mismatches.append((case.command, case.verdict, verdict))
        # Counted from the *verdict*, not the label: closing a hole moves the
        # number, which is the only way a fix is visible here.
        self.known_miss_still_open = [
            case.command for case in KNOWN_MISS if judge(case.command) == ""
        ]
        self.known_fp_still_open = [
            case.command for case in KNOWN_FP if judge(case.command) != ""
        ]
        # FN/FP are computed from the *verdict*, so they stay meaningful even
        # when the golden comparison above has already failed.
        self.fn = sum(1 for case in MUST_DENY if judge(case.command) == "")
        self.fp = sum(1 for case in MUST_ALLOW if judge(case.command) != "")
        self.problems = self._corpus_problems()

    def _corpus_problems(self) -> list[str]:
        """A corpus thinned to nothing would pass vacuously; refuse that."""
        problems: list[str] = []
        if len(DANGEROUS) < MIN_DANGEROUS:
            problems.append(f"dangerous = {len(DANGEROUS)}, need >= {MIN_DANGEROUS}")
        unparseable = len(UNPARSEABLE_DENY) + len(UNPARSEABLE_WARN)
        if unparseable < MIN_UNPARSEABLE:
            problems.append(f"unparseable = {unparseable}, need >= {MIN_UNPARSEABLE}")
        if len(SAFE) < MIN_SAFE:
            problems.append(f"safe = {len(SAFE)}, need >= {MIN_SAFE}")
        if len(KNOWN_MISS) < MIN_KNOWN_MISS:
            problems.append(f"known_miss = {len(KNOWN_MISS)}, need >= {MIN_KNOWN_MISS}")
        # ``KNOWN_MISS`` is empty by design now (§C closed all 16), so the
        # known_miss counter above is retired -- the golden comparison over
        # ``DANGEROUS`` is what catches a verdict regressing to "".  A floor on
        # the *total* corpus keeps "someone deleted the rows" from passing.
        if len(CORPUS) < MIN_CORPUS:
            problems.append(f"corpus = {len(CORPUS)}, need >= {MIN_CORPUS}")
        commands = [case.command for case in CORPUS]
        if len(set(commands)) != len(commands):
            problems.append("duplicate corpus commands")
        return problems

    @property
    def ok(self) -> bool:
        return not (self.mismatches or self.fn or self.fp or self.problems)


def format_metrics(metrics: Metrics) -> str:
    """The Markdown table, ready to paste into a PR or the data contracts."""
    golden = metrics.total - len(metrics.mismatches)
    lines = [
        "# M-D2 red line benchmark",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 语料总数 | {metrics.total} |",
        f"| golden 一致 | {golden}/{metrics.total} |",
        f"| FN（dangerous + unparseable_deny 未拦） | {metrics.fn} |",
        f"| FP（safe 误拦） | {metrics.fp} |",
        f"| known_miss（仍漏报，按判定输出） | {len(metrics.known_miss_still_open)} |",
        f"| known_fp（仍误报，按判定输出） | {len(metrics.known_fp_still_open)} |",
        "",
        "## 分类计数",
        "",
        "| 类别 | 条数 |",
        "|---|---|",
    ]
    for label in (
        "dangerous",
        "unparseable_deny",
        "unparseable_warn",
        "safe",
        "known_miss",
        "known_fp",
    ):
        lines.append(f"| {label} | {metrics.by_class[label]} |")

    lines += [
        "",
        "## 按规则分类的命中",
        "",
        "| 规则 | 命中数 |",
        "|---|---|",
    ]
    for rule, count in sorted(metrics.by_rule.items(), key=lambda row: (-row[1], row[0])):
        lines.append(f"| {rule} | {count} |")

    if metrics.known_miss_still_open:
        lines += [
            "",
            "## known_miss（缺口可见，修好即降到 0）",
            "",
        ]
        for command in metrics.known_miss_still_open:
            lines.append(f"- `{command}`")

    if metrics.mismatches:
        lines += [
            "",
            "## 与冻结值不符（回归检测的本体）",
            "",
            "| 命令 | 冻结值 | 现在 |",
            "|---|---|---|",
        ]
        for command, frozen, now in metrics.mismatches:
            lines.append(f"| `{command}` | `{frozen}` | `{now}` |")

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
        print("red line benchmark OK" if metrics.ok else "red line benchmark FAILED")
    return 0 if metrics.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
