"""R20 §7: measure the citation coverage of a real run.

The plan's success criterion is "every `path:line` the answer cites is somewhere
the session actually read".  That is only checkable if two things survive the
run: the ledger (``readLedger``) and the set of paths any tool named
(``seenPaths``).  Both are on the session file, so this reads them back rather
than trusting a log.

    python -m ai_coding.scripts.eval_claims \
        --session eval-workspace/i2c-T2/sessions/<id>.json \
        --answer  eval-workspace/i2c-T2/t2-result.json

The three-tier rule (decision 5b) is applied exactly as the loop applies it, so
the number this prints is the number the loop would have acted on:

* a path no tool result ever named      -> uncovered (tier 1)
* a path seen but not read              -> covered   (tier 2, unverifiable)
* a path read, cited outside the ranges -> uncovered (tier 3)
* an answer naming two or more files    -> skipped   (ambiguity guard)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_coding.domain.read_ledger import ReadMark, extract_claims, same_file
from ai_coding.domain.session import SessionData


def coverage(session: SessionData, answer: str) -> dict[str, object]:
    claims = extract_claims(answer)
    if not claims:
        return {
            "claims": 0,
            "covered": 0,
            "uncovered": 0,
            "coverage": None,
            "note": "the answer cites no path:line at all",
        }
    paths = {path for path, _, _ in claims}
    if len(paths) > 1:
        return {
            "claims": len(claims),
            "covered": 0,
            "uncovered": 0,
            "coverage": None,
            "note": f"skipped: the answer names {len(paths)} files",
        }
    path = claims[0][0]
    marks = [m for m in session.read_ledger if same_file(m.path, path)]
    if marks:
        uncovered = [
            (start, end)
            for _, start, end in claims
            if not _covered(marks, start, end)
        ]
    elif any(same_file(seen, path) for seen in session.seen_paths):
        uncovered = []
    else:
        uncovered = [(start, end) for _, start, end in claims]
    covered = len(claims) - len(uncovered)
    return {
        "claims": len(claims),
        "covered": covered,
        "uncovered": len(uncovered),
        "coverage": covered / len(claims),
        "uncovered_ranges": uncovered,
        "note": "",
    }


def _covered(marks: list[ReadMark], start: int, end: int) -> bool:
    for mark in marks:
        if mark.start <= end and start <= mark.end:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--answer", required=True, type=Path)
    args = parser.parse_args()

    session = SessionData.from_dict(json.loads(args.session.read_text()))
    answer = json.loads(args.answer.read_text())["answer"]
    result = coverage(session, answer)
    for key, value in result.items():
        print(f"{key}={value}")
    if result["coverage"] is None:
        return 2
    return 0 if result["uncovered"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
