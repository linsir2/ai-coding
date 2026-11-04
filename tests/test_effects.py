"""EffectLedger: the single session-owned record of what actually happened."""

from ai_coding.domain.effects import EffectLedger, FileReceipt
from ai_coding.domain.read_ledger import ReadMark
from ai_coding.domain.session import SessionData


def _session(**kwargs) -> SessionData:
    return SessionData(
        session_id="s",
        title="",
        created_time="t",
        last_access_time="t",
        **kwargs,
    )


def test_reads_keep_per_call_provenance_while_the_view_is_merged():
    first = ReadMark("a.py", 1, 60, 100, call_id="c1")
    second = ReadMark("a.py", 50, 80, 100, call_id="c2")

    ledger = EffectLedger(reads=(first, second))

    assert ledger.reads == (first, second)
    assert ledger.merged_reads() == (ReadMark("a.py", 1, 80, 100),)
    assert ledger.covering_read("a.py", 50, 60, "") == second


def test_covering_read_requires_the_current_version_and_exact_key():
    old = ReadMark("a.py", 1, 100, 100, version="v1", call_id="c1")

    ledger = EffectLedger(reads=(old,))

    assert ledger.covering_read("a.py", 10, 20, "v1") == old
    assert ledger.covering_read("a.py", 10, 20, "v2") is None
    assert ledger.covering_read("other.py", 10, 20, "v1") is None


def test_recording_an_effect_advances_the_sequence_and_replaces_the_receipt():
    ledger = EffectLedger()
    first = ledger.record_effect(
        "a.py", "v1", tool="write", call_id="c1", kind="write"
    )
    second = first.record_effect(
        "a.py", "v2", tool="edit", call_id="c2", kind="write"
    )

    assert first.sequence == 1
    assert second.sequence == 2
    assert second.receipt_for("a.py") == FileReceipt(
        path="a.py",
        version="v2",
        tool="edit",
        call_id="c2",
        seq=2,
        kind="write",
        confidence="exact",
    )
    assert second.receipt_for("a.py", min_seq=3) is None


def test_invalidate_reads_clears_current_versions_but_keeps_receipts():
    ledger = EffectLedger(
        reads=(ReadMark("a.py", 1, 10, 10, version="v1", call_id="c1"),),
    ).record_effect("a.py", "v1", tool="write", call_id="c1", kind="write")

    invalidated = ledger.invalidate_reads(["a.py"])

    assert invalidated.covering_read("a.py", 1, 10, "v1") is None
    assert invalidated.receipt_for("a.py") is not None


def test_session_round_trip_preserves_raw_reads_receipts_and_merged_view():
    session = _session()
    session.note_read("a.py", 1, 10, 10, version="v1", call_id="c1")
    session.effects = session.effects.record_effect(
        "a.py", "v1", tool="write", call_id="c1", kind="write"
    )

    restored = SessionData.from_dict(session.to_dict())

    assert restored.read_ledger == (ReadMark("a.py", 1, 10, 10, version="v1"),)
    assert restored.effects.reads[0].call_id == "c1"
    assert restored.effects.receipt_for("a.py") is not None


def test_old_readledger_rows_load_without_call_ids():
    session = SessionData.from_dict(
        {
            "sessionId": "s",
            "title": "",
            "createdTime": "t",
            "lastAccessTime": "t",
            "readLedger": [
                {"path": "a.py", "start": 1, "end": 10, "total": 10, "version": "v1"}
            ],
        }
    )

    assert session.read_ledger == (ReadMark("a.py", 1, 10, 10, version="v1"),)
    assert session.effects.reads[0].call_id == ""


def test_an_unknown_child_receipt_does_not_erase_a_local_exact_receipt():
    ledger = EffectLedger().record_effect(
        "a.py", "v1", tool="write", call_id="c1", confidence="exact"
    )

    after = ledger.record_effect(
        "a.py", "", tool="subAgent", call_id="s1", confidence="unknown"
    )

    assert after.receipt_for("a.py") == ledger.receipt_for("a.py")


def test_a_late_observed_receipt_does_not_downgrade_a_newer_exact_one():
    ledger = EffectLedger().record_effect(
        "a.py", "v1", tool="write", call_id="c1", confidence="exact", seq=6
    )

    after = ledger.record_effect(
        "a.py", "v2", tool="bash", call_id="b1", confidence="observed", seq=5
    )

    assert after.receipt_for("a.py") == ledger.receipt_for("a.py")
    assert after.sequence == 6
