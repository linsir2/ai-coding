"""TurnMemory passes the turn's evidence through, and the event carries it (R6d)."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.core.events import MemoryHarvested, RecordingObserver
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.domain.loop import TurnOutcome, TurnStatus


class _Report:
    def __init__(self, added: tuple[str, ...] = ("a",)) -> None:
        self.added = added
        self.updated: tuple[str, ...] = ()
        self.superseded: tuple[str, ...] = ()
        self.skipped: tuple[str, ...] = ()
        # The real `HarvestReport` carries this (service.py), and TurnMemory
        # reads it — a stand-in without it made the harvest emit raise, and the
        # `except Exception` turned that into a Degraded instead of the event
        # this test is about.
        self.evicted: tuple[str, ...] = ()


class _Consolidation:
    """The real `consolidate()` returns a `ConsolidateReport`, never None."""

    def __init__(self) -> None:
        self.merged: tuple[str, ...] = ()
        self.promoted: tuple[str, ...] = ()
        self.expired: tuple[str, ...] = ()
        self.evicted: tuple[str, ...] = ()


class _Memory:
    """Records the kwargs the turn handed to the service."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def harvest(self, messages, **kwargs: Any) -> _Report:
        self.kwargs = kwargs
        return _Report()

    async def consolidate(self) -> _Consolidation:
        return _Consolidation()


class _Session:
    def __init__(self) -> None:
        self.session_id = "s"
        self.messages = ["m1", "m2"]
        self.harvested_messages = 0


def _outcome() -> TurnOutcome:
    return TurnOutcome(status=TurnStatus.COMPLETED, text="done", iterations=1)


@pytest.mark.asyncio
async def test_harvest_event_carries_the_evidence_and_the_turn_number():
    memory = _Memory()
    observer = RecordingObserver()
    turn = TurnMemory(memory, emit=observer.on_event, save=lambda session: None)
    await turn.harvest(
        _Session(), deadline=None, summaries=[], outcome=_outcome(), turn=3, evidence="tool"
    )
    assert memory.kwargs["turn_index"] == 3  # 是回合号，不是消息条数
    assert memory.kwargs["evidence"] == "tool"
    assert observer.of_type(MemoryHarvested)[0].evidence == "tool"


@pytest.mark.asyncio
async def test_without_a_turn_number_the_old_cursor_semantics_survive():
    """Callers that pass no turn (and the R5 tests) keep the old behaviour."""
    memory = _Memory()
    turn = TurnMemory(memory, emit=RecordingObserver().on_event, save=lambda session: None)
    session = _Session()
    await turn.harvest(session, deadline=None, summaries=[], outcome=_outcome())
    assert memory.kwargs["turn_index"] == len(session.messages)
    assert memory.kwargs["evidence"] == ""
