"""Memories carry where they came from (R6d) — metadata, not a filter.

The measurement behind this: two live runs that forbade tool use produced
``open_thread`` notes ("不能凭记忆提供"), not invented facts, so the extractor is not
the problem (docs/r6-analysis.md 1.4).  What was missing is that nothing in the
system could *say* where a memory came from.  So this is a provenance field with no
behaviour attached: no filtering, no downgrade — the user's own statement is our
best memory and it involves no tool call at all (D-R6-10).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.config.models import MemoryConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.memory.note import MemoryNote, new_note
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


class _Extractor:
    """Answers with fixed candidates and records the batches it saw."""

    def __init__(self, *texts: str) -> None:
        self.texts = texts
        self.seen: list[list[ChatMessage]] = []

    async def __call__(self, messages, conclusion=None):
        self.seen.append(list(messages))
        return [HarvestCandidate(text=text) for text in self.texts]


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text, timestamp="t")


def _service(tmp_path: Path, *texts: str) -> tuple[MemoryService, MemoryStore]:
    store = MemoryStore(tmp_path / ".memory")
    service = MemoryService(store, extractor=_Extractor(*texts), config=MemoryConfig())
    return service, store


@pytest.mark.asyncio
async def test_notes_from_a_tool_backed_turn_are_marked_tool(tmp_path):
    service, store = _service(tmp_path, "项目用 uv 管理依赖")
    await service.harvest([_user("我们改用 uv 了")], turn_index=3, evidence="tool")
    assert [note.evidence for note in store.active()] == ["tool"]
    assert [note.source_turn for note in store.active()] == [3]


@pytest.mark.asyncio
async def test_notes_from_a_turn_without_tools_are_marked_model(tmp_path):
    service, store = _service(tmp_path, "项目用 uv 管理依赖")
    await service.harvest([_user("我们改用 uv 了")], turn_index=3, evidence="model")
    assert [note.evidence for note in store.active()] == ["model"]


def test_evidence_survives_the_store_round_trip(tmp_path):
    store = MemoryStore(tmp_path / ".memory")
    store.add_note(new_note("有工具证据的", evidence="tool"))
    reread = MemoryStore(tmp_path / ".memory")  # 重新打开：真的走了磁盘
    assert [note.evidence for note in reread.all()] == ["tool"]
    # 旧行没有这个字段：读回来是空串（未知），不是异常
    legacy = MemoryNote.from_dict({"id": "x", "text": "老数据"})
    assert legacy.evidence == ""


@pytest.mark.asyncio
async def test_status_counts_by_evidence(tmp_path):
    # Two *unrelated* candidates: near-duplicates are merged by the reconciler, and
    # this test is about provenance counting, not about de-duplication.
    service, store = _service(tmp_path, "项目用 uv 管理依赖", "测试框架是 pytest")
    await service.harvest([_user("x")], evidence="tool")
    store.add_note(new_note("c", evidence="model"))
    assert service.status()["by_evidence"] == {"tool": 2, "model": 1}
