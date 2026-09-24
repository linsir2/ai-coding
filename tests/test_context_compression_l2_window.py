"""R7b: L2 keeps a head/tail window — and nothing it drops is unrecoverable.

The measured defect (docs/r7-analysis.md 1.1): on a 70-turn tool-heavy history the
pipeline left six tool bodies, L2 replaced the oldest three with
``[Previous: used bash]``, and the 7,497 characters it dropped were gone — L3 only
persists trailing batches above 30KB, so there was no way back.  Anthropic's
context-engineering note calls tool-result clearing the cheapest form of
compaction; the half it assumes is that the artifact is still reachable.

So the contract is: the oldest results become an **envelope** (how much was
elided, where the original now lives) plus a **head/tail window** whose total cost
is bounded, and the bodies that are too small to be worth a placeholder are left
alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.core.context_compression import (
    PERSISTED_MARKER,
    ContextManager,
    _joined_history_text,
    estimate_tokens,
    placeholder_old_results,
)
from ai_coding.domain.message import ChatMessage


def _tool(body: str, call_id: str, tool: str = "bash") -> ChatMessage:
    return ChatMessage(
        role="tool", content=body, timestamp="t", tool_call_id=call_id, tool_name=tool
    )


def _long(marker: str = "long") -> str:
    """A body long enough that a placeholder is obviously smaller."""
    return f"{marker}-HEAD " + "x" * 1200 + " TAIL-marker"


# ------------------------------------------------------------------ envelope


def test_short_bodies_are_left_alone():
    out = placeholder_old_results([_tool("r1", "c1"), _tool("r2", "c2")], keep_recent=1)
    assert out[0].content == "r1"  # a placeholder would only be longer


def test_long_body_becomes_an_envelope_with_head_and_tail(tmp_path: Path):
    body = _long()
    out = placeholder_old_results(
        [_tool(body, "c1"), _tool("r2", "c2"), _tool("r3", "c3")],
        keep_recent=2,
        window_chars=50,
        elide_root=tmp_path,
    )
    text = out[0].content or ""
    assert text.startswith("[Previous: used bash")
    assert "chars elided" in text
    lines = text.splitlines()
    assert lines[1].startswith("long-HEAD")
    assert lines[-1].endswith("TAIL-marker")
    assert "…" in lines


def test_the_envelope_is_never_bigger_than_the_body(tmp_path: Path):
    body = _long()
    out = placeholder_old_results(
        [_tool(body, "c1"), _tool("r2", "c2")],
        keep_recent=1,
        window_chars=400,
        elide_root=tmp_path,
    )
    assert len(out[0].content or "") < len(body)


def test_envelope_names_the_artifact_and_the_artifact_is_the_body(tmp_path: Path):
    body = _long()
    out = placeholder_old_results(
        [_tool(body, "c1"), _tool("r2", "c2")], keep_recent=1, elide_root=tmp_path
    )
    text = out[0].content or ""
    assert "full output: " in text
    path = Path(text.split("full output: ", 1)[1].split("]")[0].strip())
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == body


def test_artifact_names_are_content_addressed(tmp_path: Path):
    body = _long()
    twice = [_tool(body, "c1"), _tool(body, "c2"), _tool("r3", "c3")]
    placeholder_old_results(twice, keep_recent=1, elide_root=tmp_path)
    assert len(list((tmp_path / "transcripts" / "elided").glob("*.txt"))) == 1


# -------------------------------------------------------------------- budget


def test_the_window_budget_is_spent_newest_first(tmp_path: Path):
    messages = [_tool(_long(f"t{i}"), f"c{i}") for i in range(4)] + [_tool("fresh", "cf")]
    out = placeholder_old_results(
        messages, keep_recent=1, window_chars=100, window_budget=100, elide_root=tmp_path
    )
    # exactly one placeholder could afford a window (the newest of the old ones);
    # the rest keep the envelope only
    windows = [m for m in out if "…" in (m.content or "")]
    assert len(windows) == 1
    assert windows[0].content.splitlines()[1].startswith("t3-HEAD")  # the newest of the old


def test_placeholders_beyond_the_budget_keep_the_envelope_only(tmp_path: Path):
    messages = [_tool(_long(f"t{i}"), f"c{i}") for i in range(4)] + [_tool("fresh", "cf")]
    out = placeholder_old_results(
        messages, keep_recent=1, window_chars=100, window_budget=100, elide_root=tmp_path
    )
    envelopes = [m.content or "" for m in out if (m.content or "").startswith("[Previous")]
    assert len(envelopes) == 4
    assert all("full output: " in text for text in envelopes)  # still recoverable
    assert sum("…" in text for text in envelopes) == 1


def test_the_most_recent_results_are_never_touched(tmp_path: Path):
    fresh = _long("fresh")
    out = placeholder_old_results(
        [_tool(_long(), "c1"), _tool(fresh, "c2")], keep_recent=1, elide_root=tmp_path
    )
    assert out[1].content == fresh


# ----------------------------------------------------------------- invariants


def test_pairing_fields_and_message_count_survive(tmp_path: Path):
    messages = [_tool(_long(), "c1"), _tool("r2", "c2"), _tool("r3", "c3")]
    out = placeholder_old_results(messages, keep_recent=2, elide_root=tmp_path)
    assert len(out) == len(messages)
    assert out[0].role == "tool"
    assert out[0].tool_call_id == "c1"
    assert out[0].tool_name == "bash"


def test_persisted_marker_results_are_skipped(tmp_path: Path):
    persisted = (
        f'<{PERSISTED_MARKER} path="transcripts/persisted/x.txt">\n'
        "preview\n</persisted-output>"
    )
    out = placeholder_old_results(
        [_tool(persisted, "c1"), _tool("r2", "c2"), _tool("r3", "c3")],
        keep_recent=2,
        elide_root=tmp_path,
    )
    assert PERSISTED_MARKER in (out[0].content or "")


def test_a_write_failure_degrades_to_a_pathless_envelope(tmp_path: Path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    out = placeholder_old_results(
        [_tool(_long(), "c1"), _tool("r2", "c2")], keep_recent=1, elide_root=blocker
    )
    text = out[0].content or ""
    assert text.startswith("[Previous: used bash")
    assert "full output" not in text
    assert "chars elided" in text


# -------------------------------------------------------------- the pipeline


class _Settings:
    max_context_tokens = 48000
    max_messages = 50
    snip_keep_head = 3
    snip_keep_tail = 20
    keep_recent_tool_results = 3
    per_result_persist_bytes = 30000
    l4_keep_tail = 6
    compress_soft_ratio = 0.8
    compress_target_ratio = 0.5
    l2_window_chars = 200
    l2_window_budget = 4000


def _long_history(turns: int = 70, body: int = 3000) -> list[ChatMessage]:
    """A tool-heavy session shaped like the R6 fixture, without importing scripts/."""
    messages: list[ChatMessage] = []
    for number in range(1, turns + 1):
        messages.append(ChatMessage(role="user", content=f"第 {number} 步", timestamp="t"))
        messages.append(
            ChatMessage(
                role="assistant",
                content=None,
                timestamp="t",
                tool_calls=[_call(number)],
            )
        )
        messages.append(_tool(f"RESULT-{number} " + "x" * body, f"c{number}"))
        messages.append(
            ChatMessage(role="assistant", content=f"第 {number} 步完成。", timestamp="t")
        )
    return messages


def _call(number: int):
    from ai_coding.domain.message import ToolCallRef

    return ToolCallRef(id=f"c{number}", name="bash", arguments='{"command":"ls"}')


@pytest.mark.asyncio
async def test_the_window_settings_reach_the_pipeline(tmp_path: Path):
    cm = ContextManager(_Settings(), tmp_path)
    out = await cm.compress_if_needed(_long_history())
    envelopes = [m for m in out if (m.content or "").startswith("[Previous")]
    assert envelopes, "the pipeline should have elided the older results"
    assert any("full output: " in (m.content or "") for m in envelopes)
    assert list((tmp_path / "transcripts" / "elided").glob("*.txt"))


@pytest.mark.asyncio
async def test_a_long_history_stays_well_under_the_context_budget(tmp_path: Path):
    cm = ContextManager(_Settings(), tmp_path)
    out = await cm.compress_if_needed(_long_history())
    assert estimate_tokens(_joined_history_text(out)) <= _Settings.max_context_tokens * 0.5
