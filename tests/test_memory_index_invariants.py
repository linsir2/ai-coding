"""The index is a pointer list with a hard budget (R6e — closes D-R4-15).

R4 promised three properties of the always-on ``<memory-constraints>`` block; until
now nothing tested them.  The block is injected into *every* request, so a leak here
is paid on every turn (docs/r6-analysis.md 2.5).
"""

from __future__ import annotations

import re
from pathlib import Path

from ai_coding.config.models import MemoryConfig
from ai_coding.memory.note import NoteKind, new_note
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore

# One line = one pointer: "- [kind#id8] a single sentence".  Bodies do not belong.
POINTER = re.compile(r"^- \[(fact|decision|preference|open_thread)#[0-9a-f]{8}\] \S")


def _service(tmp_path: Path, **overrides: object) -> tuple[MemoryService, MemoryStore]:
    store = MemoryStore(tmp_path / ".memory")
    return MemoryService(store, config=MemoryConfig(**overrides)), store


def test_index_lines_are_pointers_only(tmp_path):
    service, store = _service(tmp_path)
    for index in range(5):
        store.add_note(new_note(f"决策 {index}：用 uv 管理依赖", kind=NoteKind.DECISION))
    block = service.index_block()
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert lines, "决定类 note 必须出现在常驻块里"
    assert all(POINTER.match(line) for line in lines)
    assert all("\n" not in line for line in lines)


def test_index_never_exceeds_its_budget(tmp_path):
    service, store = _service(tmp_path, index_max_chars=600)
    for index in range(200):
        store.add_note(
            new_note(
                f"决策 {index}：这条写得像真的会出现在项目里的一句话",
                kind=NoteKind.DECISION,
            )
        )
    block = service.index_block()
    assert block.startswith("<memory-constraints>")
    assert len(block) <= 600


def test_index_does_not_grow_when_the_store_grows(tmp_path):
    service, store = _service(tmp_path, index_max_chars=600, max_index_entries=200)
    for index in range(5):
        store.add_note(new_note(f"初始决策 {index}：项目约定", kind=NoteKind.DECISION))
    before = service.index_block()
    for index in range(100):
        store.add_note(new_note(f"后续事实 {index}：读到的细节", kind=NoteKind.FACT))
    assert service.index_block() == before  # L2/L3 增长不推高索引
    assert len(service.index_block()) <= 600
