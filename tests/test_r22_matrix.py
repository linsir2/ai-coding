"""R22 batch 0: the trace↔wire matrix must not silently mis-align.

Two runs of this report must be diffable, and a row must mean the same thing in
both.  The alignment is positional, so the things that can shift it are pinned
here:

* an **auxiliary** LLM call (memory extraction, a summarizer) advertises no
  tools and assembles no ``ContextAssembled`` — pairing it would move every
  later row by one, and it can appear *between* two iterations;
* a **retry** re-sends the same body; the trace's own ``ProviderRetried`` says
  how many, and each assembled row consumes ``1 + retries`` wire rows.  Two
  legitimately identical requests are not a retry;
* a **sub-agent** emits its own events in the same turn and iteration under a
  ``scope``, and its tool calls must not be counted in the parent's row;
* a proxy file that was **appended to** (a second run, a crashed resume) leaves
  extra wire rows; the structural message-count check has to catch that instead
  of calling the row aligned.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.r22_matrix import BLOCKS, build_rows


def _event(**fields: object) -> dict:
    row = {
        "ts": "t",
        "session": "s1",
        "turn": 1,
        "span": "i1",
        "parent": "turn",
        "iter": 1,
        "evt": "X",
    }
    row.update(fields)
    return row


def _assembled(iteration: int, *, turn: int = 1, **data: object) -> dict:
    payload = {
        "iteration": iteration,
        "system_chars": 1000,
        "tools_chars": 2000,
        "history_messages": 4,
        "history_chars": 500,
        # Two non-empty blocks -> the wire body must hold 1 + 4 + 2 messages.
        "tail_blocks": [["memory", 120], ["task", 80]],
        "estimated_tokens": 900,
        "prefix_shared_chars": 0,
        "prefix_shared_ratio": 0.5,
        "digest": "d",
    }
    payload.update(data)
    return _event(evt="ContextAssembled", turn=turn, iter=iteration, data=payload)


def _write(tmp_path, rows_by_turn: dict[int, list[dict]], session: str = "s1") -> None:
    directory = tmp_path / ".ai-coding" / "traces" / session
    directory.mkdir(parents=True)
    for turn, rows in rows_by_turn.items():
        (directory / f"turn-{turn}.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )


def _wire(tmp_path, rows: list[dict]) -> Path:
    path = tmp_path / "wire.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _request(text: str, messages: int = 7) -> dict:
    """A main-loop body: 1 system + 6 others (the check below only counts)."""
    body = [{"role": "system", "content": "sys"}]
    body += [{"role": "user", "content": f"{text}-{index}"} for index in range(messages - 1)]
    return {"messages": body, "tools": [{"type": "function"}]}


def test_one_row_per_main_request_and_every_column_is_stable(tmp_path):
    _write(
        tmp_path,
        {
            1: [
                _assembled(1),
                _event(evt="ToolStarted", iter=1, data={"tool_name": "read", "call_id": "c1"}),
                _assembled(2),
                _event(evt="ToolFinished", iter=2, data={"success": False, "error": "x"}),
                _event(
                    evt="ModelStepFinished",
                    iter=2,
                    data={"index": 2, "usage": {"prompt_tokens": 1234}},
                ),
            ]
        },
    )
    wire = _wire(
        tmp_path,
        [
            {"seq": 1, "request": _request("a"), "status": 200},
            {"seq": 2, "request": _request("b"), "status": 200},
        ],
    )

    report = build_rows(tmp_path, wire)

    assert [row["aligned"] for row in report.rows] == ["yes", "yes"]
    assert report.rows[0]["tool_calls"] == 1 and report.rows[0]["tool_names"] == "read"
    assert report.rows[1]["tool_calls"] == 0 and report.rows[1]["tool_errors"] == 1
    assert report.rows[1]["prompt_tokens_actual"] == "1234"
    assert report.rows[0]["tail_chars"] == 200
    assert report.rows[0]["blk_memory"] == 120
    assert {f"blk_{name}" for name in BLOCKS} <= set(report.rows[0])


def test_an_auxiliary_call_between_two_iterations_does_not_shift_the_rows(tmp_path):
    _write(tmp_path, {1: [_assembled(1), _assembled(2)]})
    auxiliary = {
        "seq": 2,
        "request": {"messages": [{"role": "system", "content": "你是记忆抽取器"}]},
        "status": 200,
    }
    wire = _wire(
        tmp_path,
        [
            {"seq": 1, "request": _request("one"), "status": 200},
            auxiliary,
            {"seq": 3, "request": _request("two"), "status": 200},
        ],
    )

    report = build_rows(tmp_path, wire)

    assert [row["aligned"] for row in report.rows] == ["yes", "yes"]
    assert report.auxiliary == 1
    # The second row is the SECOND main request, not the auxiliary call: its
    # body hash is the third proxy row's.
    assert report.rows[1]["wire_sha"] == _sha_of_wire_row(wire, 2)


def _sha_of_wire_row(wire: Path, index: int) -> str:
    rows = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    request = rows[index]["request"]
    return hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def test_a_trace_retry_consumes_the_repeated_wire_row(tmp_path):
    _write(
        tmp_path,
        {
            1: [
                _assembled(1),
                # The engine tried again during iteration 1.
                _event(evt="ProviderRetried", iter=1, data={"kind": "server_error"}),
                _assembled(2),
            ]
        },
    )
    wire = _wire(
        tmp_path,
        [
            {"seq": 1, "request": _request("a"), "status": 500},
            {"seq": 2, "request": _request("a"), "status": 200},
            {"seq": 3, "request": _request("b"), "status": 200},
        ],
    )

    report = build_rows(tmp_path, wire)

    assert [row["aligned"] for row in report.rows] == ["yes", "yes"]
    assert report.rows[0]["wire_retries"] == 1
    assert report.rows[1]["wire_retries"] == 0


def test_two_identical_requests_without_a_retry_event_stay_two_rows(tmp_path):
    """Body equality is not evidence of a retry; only the trace event is."""
    _write(tmp_path, {1: [_assembled(1), _assembled(2)]})
    wire = _wire(
        tmp_path,
        [
            {"seq": 1, "request": _request("same"), "status": 200},
            {"seq": 2, "request": _request("same"), "status": 200},
        ],
    )

    report = build_rows(tmp_path, wire)

    assert [row["wire_retries"] for row in report.rows] == [0, 0]
    assert [row["aligned"] for row in report.rows] == ["yes", "yes"]


def test_an_appended_proxy_file_is_caught_by_the_message_count(tmp_path):
    """run 1 + run 2 in one proxy file: the counts no longer line up."""
    _write(tmp_path, {1: [_assembled(1)]})
    wire = _wire(
        tmp_path,
        [
            # A previous run's row: same shape, but 6 messages instead of 7.
            {"seq": 1, "request": _request("old", messages=6), "status": 200},
            {"seq": 2, "request": _request("new"), "status": 200},
        ],
    )

    report = build_rows(tmp_path, wire)

    assert report.rows[0]["aligned"] == "count_mismatch"
    assert report.rows[1]["aligned"] == "wire_only"


def test_a_child_scope_does_not_pollute_the_parent_row(tmp_path):
    _write(
        tmp_path,
        {
            1: [
                _assembled(1),
                _event(
                    evt="ToolStarted",
                    iter=1,
                    data={"tool_name": "read", "call_id": "p"},
                ),
                _event(
                    evt="ToolStarted",
                    iter=1,
                    scope="child",
                    data={"tool_name": "bash", "call_id": "c"},
                ),
            ]
        },
    )
    wire = _wire(tmp_path, [{"seq": 1, "request": _request("a"), "status": 200}])

    report = build_rows(tmp_path, wire)

    assert report.rows[0]["scope"] == ""
    assert report.rows[0]["tool_calls"] == 1
    assert report.rows[0]["tool_names"] == "read"


def test_turn_files_are_read_in_turn_order(tmp_path):
    _write(
        tmp_path,
        {
            2: [_assembled(1, turn=2)],
            1: [_assembled(1, turn=1)],
        },
    )
    wire = _wire(tmp_path, [{"seq": 1, "request": _request("a"), "status": 200}])

    report = build_rows(tmp_path, wire)

    assert [row["turn"] for row in report.rows] == [1, 2]


def test_two_sessions_require_an_explicit_choice(tmp_path):
    _write(tmp_path, {1: [_assembled(1)]}, session="s1")
    _write(tmp_path, {1: [_assembled(1)]}, session="s2")

    with pytest.raises(SystemExit):
        build_rows(tmp_path, None)
    # …and the explicit id still works.
    assert build_rows(tmp_path, None, "s2").session == "s2"


def test_a_truncated_trace_is_reported(tmp_path):
    _write(
        tmp_path,
        {1: [_assembled(1), _event(evt="truncated", data={"reason": "max_turn_bytes"})]},
    )
    wire = _wire(tmp_path, [{"seq": 1, "request": _request("a"), "status": 200}])

    report = build_rows(tmp_path, wire)

    assert report.truncated == 1


def test_an_empty_trace_says_so_instead_of_inventing_rows(tmp_path):
    (tmp_path / ".ai-coding" / "traces").mkdir(parents=True)

    assert build_rows(tmp_path, None).rows == []
