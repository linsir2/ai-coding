"""Serialized-record fragments never enter memory, and old ones fall out."""

import json
from types import SimpleNamespace

import pytest

from ai_coding.config.models import MemoryConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.memory.note import is_plausible_note_text, new_note
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


def _cfg(**overrides):
    base = {
        "enabled": True,
        "auto_extract": True,
        "max_index_entries": 100,
        "dedupe_similarity": 0.80,
        "pinned_kinds": ["decision"],
        "harvest_max_messages": 12,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(content):
    return ChatMessage(role="user", content=content, timestamp="t")


def test_predicate_rejects_the_observed_fragments():
    assert not is_plausible_note_text('"text": "AI-Coding 的编排')
    assert not is_plausible_note_text('"kind": "decision",')
    assert not is_plausible_note_text("[]")
    assert not is_plausible_note_text("")
    assert is_plausible_note_text("酒店项目使用 FastAPI")


def test_new_note_refuses_a_fragment():
    with pytest.raises(ValueError):
        new_note('"kind": "decision",')


def test_load_skips_fragments_written_by_the_old_bug(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "notes": [
                    {"id": "a" * 32, "text": '"text": "AI-Coding 的编排', "kind": "fact"},
                    {"id": "b" * 32, "text": "[]", "kind": "fact"},
                    {"id": "c" * 32, "text": "酒店使用 SQLite", "kind": "fact"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = MemoryStore(path)
    assert [note.id[:1] for note in store.all()] == ["c"]


def test_write_note_rejects_a_fragment(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    service = MemoryService(store, config=MemoryConfig())
    report = service.write_note('"text": "AI-Coding 的编排')
    assert report.added == ()
    assert store.all() == []


@pytest.mark.asyncio
async def test_harvest_skips_a_fragment_candidate(tmp_path):
    async def extract(messages, conclusion):
        return [
            HarvestCandidate(text='"kind": "decision",', kind="fact"),
            HarvestCandidate(text="酒店使用 SQLite", kind="fact"),
        ]

    store = MemoryStore(tmp_path / "m.json")
    service = MemoryService(store, extractor=extract, config=_cfg())
    await service.harvest([_user("记住酒店用 SQLite")])
    assert len(store.all()) == 1
    assert store.all()[0].text == "酒店使用 SQLite"
