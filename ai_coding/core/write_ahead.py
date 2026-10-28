"""M-C1: the session's write-ahead records, and the reuse decision.

The loop owns one of these per turn and hands it to the dispatcher on the tool
context -- the same shape as ``note_approval`` / ``note_checkpoint`` /
``note_plan_approved``, which exist for the same reason: the dispatcher needs
session-scoped durable state it must not fetch itself.

Both methods are **synchronous**: the dispatcher's ALLOW group runs a batch
through ``asyncio.gather`` (``dispatch.py``), and a sync method is what keeps
two same-param calls in one batch from both claiming a miss.

The reuse domain is **the turn**: ``clear`` runs at turn close, so a match can
only come from the turn that is running (or the one a crash interrupted).  A
later turn starts with an empty log -- this is crash recovery, not a
session-long call cache.
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.guard import call_fingerprint
from ai_coding.domain.message import ToolArtifact, ToolCall, ToolResult
from ai_coding.domain.write_ahead import (
    WriteAheadIntent,
    clip_artifacts,
    clip_data,
    clip_output,
)

#: ``max_iterations=10 × max_parallel=4 = 40``, a replay adds another 40; 256
#: leaves room and, with the turn-close clear, the real length is one batch.
MAX_RECORDS = 256

__all__ = ["MAX_RECORDS", "WriteAheadLog"]


class WriteAheadLog:
    """The turn's write-ahead records, keyed by ``(tool, digest)``."""

    def __init__(
        self,
        session: Any,
        sessions: Any,
        *,
        enabled: bool = True,
        replay_scope: frozenset[str] | None = None,
    ) -> None:
        self._session = session
        self._sessions = sessions
        self._enabled = bool(enabled)
        #: Which digests may be answered from a record at all.  ``None`` means
        #: "whatever is on disk" (a resumed turn that did not take a snapshot);
        #: a set narrows it to the records the *interrupted* turn left behind,
        #: so a call this turn settled cannot be replayed back to itself.
        self._replay_scope = replay_scope
        self._records: dict[str, WriteAheadIntent] = {}
        # ``load`` reads the sidecar back onto the session, so a resumed turn
        # starts from what the crashed one left behind.
        for record in getattr(session, "write_ahead", None) or ():
            self._records[record.digest] = record

    @staticmethod
    def replay_scope(session: Any) -> frozenset[str]:
        """The digests a resumed turn may answer from.

        Only records the *crashed* turn settled: ``running`` proves nothing
        (the side effect's outcome is unknown), and a record the resumed turn
        settles itself must stay out of the scope or the next batch replays it.
        """
        return frozenset(
            record.digest
            for record in (getattr(session, "write_ahead", None) or ())
            if record.status == "ok"
        )

    def claim(
        self, call: ToolCall, *, allow_replay: bool = False
    ) -> ToolResult | None:
        """Record the intent before it runs; return a result to reuse, if any.

        ``running`` is not a hit -- the side effect's outcome is unknown -- so a
        resumed turn re-runs the call and settles it again.  Only ``ok`` is
        reused, and the reuse carries the recorded output/data/artifacts so it
        is indistinguishable from a fresh run.

        ``allow_replay`` defaults to **False** because the record exists for
        crash recovery, not for de-duplicating a live turn.  Reusing inside the
        turn it was written in is how a model came to re-read a file it had
        just edited and conclude the edit had failed (eval-08 stage F).
        """
        if not self._enabled:
            return None
        digest = call_fingerprint(call)
        record = self._records.get(digest)
        if (
            record is not None
            and record.status == "ok"
            and allow_replay
            and (self._replay_scope is None or digest in self._replay_scope)
        ):
            return ToolResult(
                success=True,
                output=record.output,
                # ``None`` when the call had no structured result: a fresh run
                # of such a tool reports ``data=None``, and an empty dict would
                # add a ``<data>{}</data>`` block the fresh run never had.
                data=dict(record.data) if record.data else None,
                artifacts=tuple(
                    ToolArtifact.from_dict(row) for row in record.artifacts
                ),
            )
        if record is None and len(self._records) < MAX_RECORDS:
            self._records[digest] = WriteAheadIntent(
                call_id=call.id, tool=call.name, digest=digest
            )
            self._persist()
        return None

    def settle(self, call: ToolCall, result: ToolResult) -> None:
        """Back-fill the record with the call's outcome (capped)."""
        if not self._enabled:
            return
        digest = call_fingerprint(call)
        record = self._records.get(digest)
        if record is None:
            return
        record.status = "ok" if result.success else "failed"
        record.output = clip_output(result.output)
        record.data = clip_data(result.data)
        record.artifacts = clip_artifacts(result.artifacts)
        self._persist()

    def clear(self) -> None:
        """Drop the turn's records -- the reuse domain ends here (M-C1)."""
        if not self._enabled:
            return
        self._records = {}
        self._session.write_ahead = None
        # Duck-typed like ``jobs``/``note_checkpoint``: a test double without the
        # sidecar methods keeps the in-memory behaviour and loses only durability.
        clear = getattr(self._sessions, "clear_write_ahead", None)
        if callable(clear):
            clear(self._session.session_id)

    def _persist(self) -> None:
        rows = [record.to_dict() for record in self._records.values()]
        self._session.write_ahead = list(self._records.values())
        mark = getattr(self._sessions, "mark_write_ahead", None)
        if callable(mark):
            mark(self._session.session_id, rows)
