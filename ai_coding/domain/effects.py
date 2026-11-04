"""The session's effect ledger: one answer to "what has actually happened".

The read ledger remains the range-merge renderer.  This module keeps the raw
per-call reads (needed to prove that the exact earlier result is still visible)
and the file receipts every write path produces.  It is deliberately pure: no
filesystem, no clock, no session.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .read_ledger import ReadMark, merge_mark

__all__ = ["EffectLedger", "FileReceipt"]


@dataclass(frozen=True)
class FileReceipt:
    """One successful effect on one file, with the version it produced."""

    path: str
    version: str
    tool: str
    call_id: str
    seq: int
    kind: str = "write"
    #: ``exact`` = the tool named the file (write/edit/subagent artifact).
    #: ``observed`` = bash stat saw it change.  ``unknown`` never satisfies a
    #: completion receipt; it is a diagnostic only.
    confidence: str = "exact"


@dataclass(frozen=True)
class EffectLedger:
    """Raw read provenance plus file receipts, in one session-owned value."""

    reads: tuple[ReadMark, ...] = ()
    files: tuple[FileReceipt, ...] = ()
    sequence: int = 0

    def note_read(self, mark: ReadMark, *, max_marks: int = 80) -> EffectLedger:
        reads = (*self.reads, mark)
        if max_marks > 0 and len(reads) > max_marks:
            reads = reads[-max_marks:]
        return replace(self, reads=reads)

    def record_effect(
        self,
        path: str,
        version: str,
        *,
        tool: str,
        call_id: str,
        kind: str = "write",
        confidence: str = "exact",
        seq: int | None = None,
    ) -> EffectLedger:
        assigned_seq = int(seq) if seq is not None else self.sequence + 1
        if assigned_seq <= 0:
            assigned_seq = self.sequence + 1
        receipt = FileReceipt(
            path=path,
            version=version,
            tool=tool,
            call_id=call_id,
            seq=assigned_seq,
            kind=kind,
            confidence=confidence,
        )
        existing = self.receipt_for(path)
        if confidence == "unknown" and existing is not None and existing.confidence != "unknown":
            # An unknown child/worktree effect must not erase a stronger local
            # receipt for the same path.
            return self
        if (
            confidence == "observed"
            and existing is not None
            and existing.seq > assigned_seq
        ):
            # A late job must not overwrite a newer exact/observed write for
            # the same path with its older, launch-time sequence.
            return self
        files = tuple(r for r in self.files if r.path != path) + (receipt,)
        return replace(self, files=files, sequence=max(self.sequence, assigned_seq))

    def clear_files(self) -> EffectLedger:
        """Drop file receipts (a new plan must not inherit old work)."""
        return replace(self, files=())

    def invalidate_reads(self, paths: tuple[str, ...] | list[str]) -> EffectLedger:
        """Stale-ify read evidence, leaving completed file receipts intact."""
        keys = {str(path) for path in paths if str(path)}
        if not keys:
            return self
        reads = tuple(
            replace(mark, version="") if mark.path in keys else mark
            for mark in self.reads
        )
        return replace(self, reads=reads)

    def covering_read(
        self, path: str, start: int, end: int, version: str
    ) -> ReadMark | None:
        """The raw read that covers this exact range and file version."""
        for mark in reversed(self.reads):
            if mark.path != path or mark.version != version:
                continue
            if mark.start <= start and end <= mark.end:
                return mark
        return None

    def receipt_for(self, path: str, *, min_seq: int = 0) -> FileReceipt | None:
        for receipt in reversed(self.files):
            if receipt.path == path and receipt.seq >= min_seq:
                return receipt
        return None

    def merged_reads(self) -> tuple[ReadMark, ...]:
        """The display/citation projection: ranges merged, no call provenance."""
        merged: tuple[ReadMark, ...] = ()
        for mark in self.reads:
            merged = merge_mark(merged, mark, max_marks=0)
        return tuple(replace(mark, call_id="") for mark in merged)
