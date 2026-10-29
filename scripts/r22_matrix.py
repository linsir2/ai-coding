#!/usr/bin/env python3
"""R22 batch 0c: join one run's trace with its recorded wire traffic.

The two halves answer different questions and neither is enough alone:

* the **trace** (``<ws>/.ai-coding/traces/<session>/turn-*.jsonl``) says what the
  runtime assembled — one ``ContextAssembled`` per main-loop iteration, with the
  character count of every tail block, the history size, the estimated tokens,
  the prefix-cache ratio, and what that iteration then did;
* the **proxy log** (``scripts/recording_proxy.py --out``) says what actually
  went over the wire — status code, request body, real prompt tokens.

They carry no common key, so the join is **positional over the main requests**,
with three filters that keep the positions honest:

* a wire row **without** ``tools`` is an auxiliary call (memory extraction, a
  summarizer).  It assembles no ``ContextAssembled``, so it is dropped and
  counted; pairing it would shift every later row;
* a **retry** re-sends the same body.  The trace says how many times with
  ``ProviderRetried`` (one per attempt, under the iteration that retried), so
  each assembled row *consumes* ``1 + retries`` wire rows.  Counting from the
  trace rather than from equal bodies matters: two legitimately identical
  requests are not a retry;
* a **sub-agent** assembles its own context (with a ``scope``) and its tool
  events land in the same turn and iteration, so scoped events are aligned like
  any other and tool counts are filtered by the same scope.

Every row is then checked structurally: the wire body must hold exactly
``1 system + history_messages + non-empty tail blocks`` messages.  A mismatch
says ``count_mismatch`` instead of pretending the row is aligned — a run that
was appended to the same proxy file would otherwise pair run 1's rows with
run 2's events.

Usage::

    python scripts/r22_matrix.py --workspace /workspace/eval-workspace/long-r21c \
        --proxy /tmp/record/s2.jsonl [--session <id>] [--csv docs/r22-evidence/s2.csv]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The tail blocks in assembly order (``core/request_assembly.py``).  A fixed
#: list, not "whatever appeared": two runs must be diffable column by column,
#: and a block that was absent has to read as ``0`` rather than as a missing
#: column.
BLOCKS = (
    "environment",
    "memory",
    "read_ledger",
    "mcp_catalog",
    "task",
    "recommended_skills",
    "referenced_files",
    "error_recovery",
    "anchor",
    "hook",
    "digest",
    "budget",
)

COLUMNS = (
    "request",
    "aligned",
    "session",
    "turn",
    "iteration",
    "scope",
    "estimated_tokens",
    "prompt_tokens_actual",
    "system_chars",
    "tools_chars",
    "history_msgs",
    "history_chars",
    "tail_chars",
    "prefix_ratio",
    *(f"blk_{name}" for name in BLOCKS),
    "wire_status",
    "wire_messages",
    "wire_chars",
    "wire_sha",
    "wire_retries",
    "tool_calls",
    "tool_names",
    "tool_errors",
)


@dataclass
class MatrixReport:
    """The rows plus the facts that belong to the whole run, not to one row."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    auxiliary: int = 0
    truncated: int = 0
    session: str = ""


def _session_dir(root: Path, wanted: str) -> Path:
    """The one session directory this report is about.

    ``wanted`` wins when given.  A workspace with several sessions refuses to
    guess: alternating turn files would make "the newest directory" flip
    between them, and a report that silently mixes two runs is worse than one
    that asks for ``--session``.
    """
    if wanted:
        directory = root / wanted
        if not directory.is_dir():
            raise SystemExit(f"no such session under {root}: {wanted}")
        return directory
    candidates = [path for path in root.iterdir() if path.is_dir()] if root.is_dir() else []
    if len(candidates) > 1:
        names = ", ".join(sorted(path.name for path in candidates))
        raise SystemExit(
            f"{len(candidates)} sessions under {root} ({names}); pass --session <id>"
        )
    return candidates[0] if candidates else root


def _turn_files(session: Path) -> list[Path]:
    """Turn files in turn order (``turn-1``, ``turn-2``, …)."""

    def key(path: Path) -> int:
        number = path.stem.removeprefix("turn-")
        return int(number) if number.isdigit() else 0

    return sorted(session.glob("turn-*.jsonl"), key=key)


def _read_events(session: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _turn_files(session):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _read_proxy(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return sorted(out, key=lambda row: int(row.get("seq") or 0))


def _main_requests(entries: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Wire rows of the main loop; auxiliary calls are dropped and counted."""
    kept: list[dict[str, Any]] = []
    auxiliary = 0
    for entry in entries:
        request = entry.get("request") or {}
        if not request.get("tools"):
            auxiliary += 1
            continue
        kept.append(entry)
    return kept, auxiliary


def _retries_per_iter(events: Iterable[dict[str, Any]]) -> dict[tuple[int, int, str], int]:
    """How many retries the trace recorded for each ``(turn, iter, scope)``."""
    counts: dict[tuple[int, int, str], int] = {}
    for row in events:
        if row.get("evt") != "ProviderRetried":
            continue
        key = (
            int(row.get("turn") or 0),
            int(row.get("iter") or 0),
            str(row.get("scope") or ""),
        )
        counts[key] = counts.get(key, 0) + 1
    return counts


def _wire_facts(entry: dict[str, Any]) -> tuple[str, int, int, str]:
    """``(status, messages, chars, sha)`` for one recorded request.

    ``chars`` is the serialised ``messages`` array only — ``tools`` are counted
    separately (``tools_chars``), so the two never double-count each other.
    ``sha`` is what lets a row be traced back to the exact request in the proxy
    file: it is stable for the same body, which is also how a retry is
    recognised when the trace is missing its ``ProviderRetried``.
    """
    request = entry.get("request") or {}
    messages = request.get("messages") or []
    text = json.dumps(messages, ensure_ascii=False)
    digest = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return (str(entry.get("status", "")), len(messages), len(text), digest)


def _blocks(data: dict[str, Any]) -> dict[str, int]:
    counts = dict.fromkeys(BLOCKS, 0)
    for name, chars in data.get("tail_blocks") or []:
        if name in counts:
            counts[name] = int(chars or 0)
    return counts


def _for_iteration(
    rows: Iterable[dict[str, Any]],
    *,
    turn: int,
    iteration: int,
    scope: str,
) -> tuple[int, str, int, str]:
    """``(calls, names, errors, prompt_tokens)`` for one iteration and scope."""
    calls = 0
    names: list[str] = []
    errors = 0
    prompt_tokens = ""
    for row in rows:
        if row.get("turn") != turn or row.get("iter") != iteration:
            continue
        if str(row.get("scope") or "") != scope:
            continue
        event = row.get("evt")
        data = row.get("data") or {}
        if event == "ToolStarted":
            calls += 1
            names.append(str(data.get("tool_name", "")))
        elif event == "ToolFinished":
            if data.get("success") is False or data.get("error"):
                errors += 1
        elif event == "ModelStepFinished":
            usage = data.get("usage") or {}
            if isinstance(usage, dict) and "prompt_tokens" in usage:
                prompt_tokens = str(usage["prompt_tokens"])
    return calls, " ".join(name for name in names if name), errors, prompt_tokens


def _row_from_event(event: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    data = event.get("data") or {}
    scope = str(event.get("scope") or "")
    row: dict[str, Any] = dict.fromkeys(COLUMNS, "")
    row["session"] = event.get("session", "")
    row["turn"] = event.get("turn", "")
    row["iteration"] = event.get("iter", "")
    row["scope"] = scope
    row["estimated_tokens"] = data.get("estimated_tokens", "")
    row["system_chars"] = data.get("system_chars", "")
    row["tools_chars"] = data.get("tools_chars", "")
    row["history_msgs"] = data.get("history_messages", "")
    row["history_chars"] = data.get("history_chars", "")
    blocks = _blocks(data)
    row["tail_chars"] = sum(blocks.values())
    for name, chars in blocks.items():
        row[f"blk_{name}"] = chars
    ratio = data.get("prefix_shared_ratio")
    row["prefix_ratio"] = f"{float(ratio):.3f}" if isinstance(ratio, (int, float)) else ""
    calls, names, errors, prompt_tokens = _for_iteration(
        events,
        turn=int(event.get("turn") or 0),
        iteration=int(event.get("iter") or 0),
        scope=scope,
    )
    row["tool_calls"] = calls
    row["tool_names"] = names
    row["tool_errors"] = errors
    row["prompt_tokens_actual"] = prompt_tokens
    # What the body must hold: the system message, the history, and one message
    # per non-empty tail block (``request_assembly`` appends them as messages).
    row["_expected_messages"] = (
        1 + int(data.get("history_messages") or 0) + sum(1 for chars in blocks.values() if chars)
    )
    return row


def build_rows(
    workspace: Path, proxy: Path | None, session: str = ""
) -> MatrixReport:
    root = workspace / ".ai-coding" / "traces"
    session_dir = _session_dir(root, session)
    events = _read_events(session_dir)
    assembled = [row for row in events if row.get("evt") == "ContextAssembled"]
    retries = _retries_per_iter(events)
    wire, auxiliary = _main_requests(_read_proxy(proxy))
    report = MatrixReport(
        auxiliary=auxiliary,
        truncated=sum(1 for row in events if row.get("evt") == "truncated"),
        session=session_dir.name,
    )

    cursor = 0
    for event in assembled:
        row = _row_from_event(event, events)
        key = (
            int(event.get("turn") or 0),
            int(event.get("iter") or 0),
            str(event.get("scope") or ""),
        )
        wanted = 1 + retries.get(key, 0)
        taken = wire[cursor : cursor + wanted]
        cursor += len(taken)
        row["request"] = len(report.rows) + 1
        row["wire_retries"] = max(0, len(taken) - 1)
        if taken:
            status, messages, chars, digest = _wire_facts(taken[0])
            row["wire_status"] = status
            row["wire_messages"] = messages
            row["wire_chars"] = chars
            row["wire_sha"] = digest
            row["aligned"] = (
                "yes" if messages == row["_expected_messages"] else "count_mismatch"
            )
        else:
            row["aligned"] = "unaligned"
        del row["_expected_messages"]
        report.rows.append(row)

    # Wire rows the trace never assembled — a run appended to the same proxy
    # file, or a request the trace lost.  Surfaced rather than dropped.
    for extra in wire[cursor:]:
        row = dict.fromkeys(COLUMNS, "")
        row["request"] = len(report.rows) + 1
        row["aligned"] = "wire_only"
        status, messages, chars, digest = _wire_facts(extra)
        row["wire_status"] = status
        row["wire_messages"] = messages
        row["wire_chars"] = chars
        row["wire_sha"] = digest
        report.rows.append(row)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", required=True, help="scenario workspace (holds .ai-coding/traces)"
    )
    parser.add_argument("--proxy", default="", help="recording_proxy JSONL for the same run")
    parser.add_argument("--session", default="", help="session id (required if several exist)")
    parser.add_argument("--csv", default="", help="write CSV here instead of stdout")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    proxy = Path(args.proxy).expanduser().resolve() if args.proxy else None
    report = build_rows(workspace, proxy, args.session)
    rows = report.rows
    if not rows:
        print(
            f"no rows: {workspace}/.ai-coding/traces has no ContextAssembled event "
            "(is tracing enabled, and did this run build a recorder?)",
            file=sys.stderr,
        )
        return 1

    if args.csv:
        target = Path(args.csv).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} row(s) -> {target}", file=sys.stderr)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    if report.auxiliary:
        print(
            f"{report.auxiliary} auxiliary call(s) dropped (no tools: memory/summarizer)",
            file=sys.stderr,
        )
    odd = [row for row in rows if row["aligned"] != "yes"]
    if odd:
        counts: dict[str, int] = {}
        for row in odd:
            counts[str(row["aligned"])] = counts.get(str(row["aligned"]), 0) + 1
        detail = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        print(f"{len(odd)}/{len(rows)} row(s) not aligned ({detail})", file=sys.stderr)
    if report.truncated:
        print(
            "WARNING: the trace hit max_turn_bytes and stopped — rows after the "
            "marker are missing, not absent from the run",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
