"""TDD (R5): the memory-facing half of a turn, on its own.

``_harvest`` was 72 lines inside the loop, mixing a delta cursor, two deadline
wrappers, two degradation reports and a conditional consolidation.  It is
best-effort by contract: **memory must never break a turn**, and every fallback
has to be visible (docs/r5-analysis.md 3.2).
"""

from dataclasses import dataclass, field

import pytest

from ai_coding.core.events import Degraded, MemoryHarvested, RecordingObserver
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.session import SessionData


@dataclass(frozen=True)
class _Note:
    """The real contract: a note has an ``id`` (memory/note.py)."""

    id: str


@dataclass
class _Report:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class _Memory:
    """A memory service that behaves, and records what it was asked."""

    def __init__(self, *, notes=None, report=None) -> None:
        self._notes = notes or []
        self._report = report or _Report()
        self.harvested: list[list] = []
        self.consolidated = 0
        self.raise_on: set[str] = set()

    def index_notes(self):  # type: ignore[no-untyped-def]
        if "index_notes" in self.raise_on:
            raise RuntimeError("index unreadable")
        return self._notes

    def render_index_block(self, notes):  # type: ignore[no-untyped-def]
        return "BLOCK:" + ",".join(note.id for note in notes)

    async def harvest(self, delta, **kwargs):  # type: ignore[no-untyped-def]
        if "harvest" in self.raise_on:
            raise RuntimeError("harvest exploded")
        self.harvested.append(list(delta))
        return self._report

    async def consolidate(self):  # type: ignore[no-untyped-def]
        if "consolidate" in self.raise_on:
            raise RuntimeError("consolidate exploded")
        self.consolidated += 1

    def status(self):  # type: ignore[no-untyped-def]
        if "status" in self.raise_on:
            raise RuntimeError("status exploded")
        return {"total": 3, "active": 2, "pinned": 1, "by_kind": {"fact": 2}, "enabled": True}


class _Session(SessionData):
    def __init__(self) -> None:
        super().__init__(
            session_id="s1", title="", created_time="", last_access_time=""
        )
        self.saved = 0


def _memory(**kwargs) -> tuple[TurnMemory, RecordingObserver, list[SessionData]]:
    observer = RecordingObserver()
    saved: list[SessionData] = []
    service = kwargs.pop("service", None) or _Memory(**kwargs)
    turn = TurnMemory(service, emit=observer.on_event, save=saved.append)
    return turn, observer, saved


def _outcome(text: str | None = "done") -> TurnOutcome:
    return TurnOutcome(status=TurnStatus.COMPLETED, text=text)


# ---- block -----------------------------------------------------------------


def test_block_renders_notes_and_reports_their_ids():
    turn, _, _ = _memory(notes=[_Note("n1"), _Note("n2")])
    text, ids = turn.block()
    assert text == "BLOCK:n1,n2"
    assert ids == ("n1", "n2")


def test_block_is_empty_without_notes():
    turn, _, _ = _memory(notes=[])
    assert turn.block() == ("", ())


def test_block_failure_degrades_instead_of_raising():
    service = _Memory(notes=[_Note("n1")])
    service.raise_on.add("index_notes")
    turn, observer, _ = _memory(service=service)
    assert turn.block() == ("", ())
    assert [e.component for e in observer.of_type(Degraded)] == ["memory_block"]


def test_without_a_memory_service_everything_is_quiet():
    observer = RecordingObserver()
    turn = TurnMemory(None, emit=observer.on_event, save=lambda s: None)
    assert turn.block() == ("", ())
    assert turn.status()["enabled"] is False


# ---- harvest ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_harvest_only_consumes_the_delta_and_advances_the_cursor():
    turn, observer, saved = _memory()
    session = _Session()
    session.messages = [object()] * 3
    session.harvested_messages = 1

    await turn.harvest(session, deadline=None, summaries=[], outcome=_outcome())

    assert saved, "the cursor must be persisted"
    assert session.harvested_messages == 3
    assert saved[0] is session
    assert len(observer.of_type(MemoryHarvested)) == 0  # empty report, no event


@pytest.mark.asyncio
async def test_harvest_reports_what_it_wrote():
    service = _Memory(report=_Report(added=["a"], superseded=["b"]))
    turn, observer, _ = _memory(service=service)
    session = _Session()
    session.messages = ["m1"]

    await turn.harvest(session, deadline=None, summaries=["a summary"], outcome=_outcome())

    event = observer.of_type(MemoryHarvested)[0]
    assert event.added == ("a",)
    assert event.superseded == ("b",)


@pytest.mark.asyncio
async def test_a_failed_harvest_is_visible_and_leaves_the_cursor_alone():
    service = _Memory()
    service.raise_on.add("harvest")
    turn, observer, saved = _memory(service=service)
    session = _Session()
    session.messages = ["m1", "m2"]
    session.harvested_messages = 1

    await turn.harvest(session, deadline=None, summaries=[], outcome=_outcome())

    assert session.harvested_messages == 1, "a failed harvest must not lose messages"
    assert saved == []
    assert [e.component for e in observer.of_type(Degraded)] == ["memory_harvest"]


@pytest.mark.asyncio
async def test_consolidation_only_runs_when_the_turn_concluded_something():
    service = _Memory()
    turn, _, _ = _memory(service=service)
    session = _Session()
    session.messages = ["m1"]

    await turn.harvest(session, deadline=None, summaries=[], outcome=_outcome(text=None))
    assert service.consolidated == 0

    session2 = _Session()
    session2.messages = ["m2"]
    turn2, _, _ = _memory(service=service)
    await turn2.harvest(session2, deadline=None, summaries=[], outcome=_outcome())
    assert service.consolidated == 1


@pytest.mark.asyncio
async def test_a_failed_consolidation_is_visible():
    service = _Memory()
    service.raise_on.add("consolidate")
    turn, observer, _ = _memory(service=service)
    session = _Session()
    session.messages = ["m1"]

    await turn.harvest(session, deadline=None, summaries=[], outcome=_outcome())

    assert [e.component for e in observer.of_type(Degraded)] == ["memory_consolidate"]


@pytest.mark.asyncio
async def test_harvest_is_a_no_op_without_memory_or_a_delta():
    observer = RecordingObserver()
    turn = TurnMemory(None, emit=observer.on_event, save=lambda s: None)
    await turn.harvest(_Session(), deadline=None, summaries=[], outcome=_outcome())
    assert observer.events == []

    turn2, observer2, _ = _memory()
    session = _Session()
    session.messages = []
    await turn2.harvest(session, deadline=None, summaries=[], outcome=_outcome())
    assert observer2.events == []


# ---- status ----------------------------------------------------------------


def test_status_passes_through_and_never_raises():
    turn, _, _ = _memory()
    assert turn.status()["total"] == 3

    service = _Memory()
    service.raise_on.add("status")
    broken, _, _ = _memory(service=service)
    stats = broken.status()
    assert stats["enabled"] is True
    assert stats["total"] == 0
