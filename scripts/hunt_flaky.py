#!/usr/bin/env python3
"""Hunt intermittent suite failures: N runs, every log on disk, names printed.

A single red full-suite run is not evidence of anything except "run it again";
the evidence is a log with the failing test's name in it.  This script encodes
the recipe that finally captured one such failure after a whole session of
manual reruns (it fell on the fourth consecutive full run):

    .venv/bin/python scripts/hunt_flaky.py                # 5 full runs
    .venv/bin/python scripts/hunt_flaky.py --runs 20
    .venv/bin/python scripts/hunt_flaky.py --target tests/test_jobs.py
    .venv/bin/python scripts/hunt_flaky.py --keep-going   # don't stop at the first red

Every run writes ``<out>/run<N>.log`` in full.  On a red run the ``FAILED``
test ids are printed with the log path, and those exact ids are rerun with a
long traceback into ``<out>/detail-run<N>.log`` -- an immediate pass there is
itself the finding (intermittence confirmed, log is the only evidence).
Exit 0 iff every run was green, 1 the moment one was not.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

FAILED_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)


def _pytest_cmd(tb: str, targets: list[str]) -> list[str]:
    return [sys.executable, "-m", "pytest", "-p", "no:warnings", "--tb", tb, *targets]


def _run_to_log(cmd: list[str], log: Path) -> int:
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def _last_line(text: str) -> str:
    return next((line for line in reversed(text.splitlines()) if line.strip()), "")


def run_once(
    out_dir: Path, index: int, tb: str, targets: list[str]
) -> tuple[int, list[str], str, Path]:
    log = out_dir / f"run{index}.log"
    code = _run_to_log(_pytest_cmd(tb, targets), log)
    text = log.read_text(encoding="utf-8", errors="replace")
    return code, FAILED_RE.findall(text), _last_line(text), log


def rerun_detail(
    out_dir: Path, index: int, detail_tb: str, ids: list[str]
) -> tuple[str, str, Path]:
    """Rerun exactly the failed ids; a pass here is the intermittence, not a fix."""
    log = out_dir / f"detail-run{index}.log"
    code = _run_to_log(_pytest_cmd(detail_tb, ids), log)
    verdict = "reproduced" if code != 0 else "passed on rerun (intermittent confirmed)"
    return verdict, _last_line(log.read_text(encoding="utf-8", errors="replace")), log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the suite N times, keep every log, print failing test ids."
    )
    parser.add_argument("--runs", type=int, default=5, help="how many full runs (default 5)")
    parser.add_argument(
        "--out",
        default=None,
        help="log directory (default: <tmp>/flaky-hunt, kept across runs on purpose)",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="pytest target; repeatable (default: whatever pyproject selects)",
    )
    parser.add_argument("--tb", default="short", help="traceback style for the main runs")
    parser.add_argument("--detail-tb", default="long", help="traceback style for the rerun")
    parser.add_argument(
        "--keep-going", action="store_true", help="keep running after a red run"
    )
    args = parser.parse_args(argv)
    runs = max(1, int(args.runs))
    out_dir = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "flaky-hunt"
    out_dir.mkdir(parents=True, exist_ok=True)

    red = 0
    for index in range(1, runs + 1):
        code, failed, summary, log = run_once(out_dir, index, args.tb, list(args.target))
        print(f"run {index}/{runs}: {'green' if code == 0 else 'RED'} — {summary}")
        if code == 0:
            continue
        red += 1
        if failed:
            print(f"  failed: {', '.join(failed)}")
            verdict, detail_summary, detail_log = rerun_detail(
                out_dir, index, args.detail_tb, failed
            )
            print(f"  detail rerun: {verdict} — {detail_summary}")
            print(f"  detail log: {detail_log}")
        else:
            print("  red with no FAILED lines (crash / collection error?)")
        print(f"  log: {log}")
        if not args.keep_going:
            break

    print(f"{red} red / {runs} run(s); logs in {out_dir}")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
