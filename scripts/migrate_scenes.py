"""One-off: fold the real store's near-synonym scene labels onto one name each.

Why this exists.  The extractor was shown no vocabulary, so on this machine it
named 36 labels over 43 notes and 30 of those were singletons.  M8 then stopped
treating a label as a group unless three memories share it — which left exactly
one group, because the labels that *should* have been one group were spelled
several ways (「记忆模块」/「记忆系统」/「记忆配置」, 「项目概况」/「项目环境」/「项目结构」).
Split spellings cannot reach the support threshold no matter how long the
conversation runs, so the labels have to be folded once.

This is a spelling merge, not a taxonomy.  It is hand-written because the
decisions are semantic and a fuzzy matcher gets them wrong in a way nothing
reports afterwards.  「工具约束」 and 「执行约束」 share the word 约束 and nothing
else — the first is what the tools may do, the second is what a run may do — and
they go to *different* labels here.  A matcher that reads the shared word merges
them into one, and then `scene=` silently answers a question nobody asked.
Every entry below was read in full, and the labels are named by what the note
says, not by what the model seemed to be thinking of when it named them: the
note labelled 「酒店管理系统」 is about the read/write sandbox boundary.

Dry run by default::

    .venv/bin/python scripts/migrate_scenes.py                 # report
    .venv/bin/python scripts/migrate_scenes.py --apply         # write
    .venv/bin/python scripts/migrate_scenes.py --store other    # another store

Safe to re-run: a label that is already its own target changes nothing.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from ai_coding.memory.note import MemoryNote
from ai_coding.memory.scenes import SCENE_SYNONYMS, fold_scene
from ai_coding.memory.service import IndexSummary

__all__ = ["SCENE_SYNONYMS", "head", "label_count", "plan"]


class _NoteSource(Protocol):
    """What ``plan`` reads: a source of notes, in file order."""

    def all(self) -> list[MemoryNote]: ...


class _RawStore:
    """A store view that reads the file **without** the read-time fold.

    ``MemoryStore._load`` folds synonyms on read (M-F2) — right for recall, and
    wrong for the one-off whose whole job is to rewrite those spellings on disk.
    Reading through ``MemoryStore`` would fold first and leave nothing to merge,
    so this reads the raw rows itself.  The on-disk format is unchanged.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._notes = _read_raw_notes(path)

    def all(self) -> list[MemoryNote]:
        return list(self._notes)

    def active(self) -> list[MemoryNote]:
        return [note for note in self._notes if note.is_active]

    def update(self, note: MemoryNote) -> bool:
        for index, current in enumerate(self._notes):
            if current.id == note.id:
                self._notes[index] = note
                self._save()
                return True
        return False

    def _save(self) -> None:
        import os
        import uuid

        from ai_coding.infra.json_utils import to_json_pretty

        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = to_json_pretty(
            {"version": 2, "notes": [note.to_dict() for note in self._notes]}
        )
        # Atomic, like MemoryStore._save: --apply rewrites the whole file once
        # per note, so a crash mid-run must not leave a truncated store.
        tmp = self._path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self._path)


def _read_raw_notes(path: Path) -> list[MemoryNote]:
    """Read v2 (``notes``) or v1 (``entries``) rows without the fold; never raise."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("notes")
    if not isinstance(raw, list):
        raw = data.get("entries")  # v1 migration (read-only)
    if not isinstance(raw, list):
        return []
    notes: list[MemoryNote] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            notes.append(MemoryNote.from_dict(item))
        except (ValueError, TypeError):
            continue
    return notes


def plan(store: _NoteSource) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """``(changes, counts_after)`` for this store, computed without touching it.

    ``counts_after`` is what the labels become, so the caller can see the head
    the merge would produce before deciding to write it.
    """
    changes: list[tuple[str, str]] = []
    counts_after: Counter[str] = Counter()
    for note in store.all():
        target = fold_scene(note.scene)
        if note.scene != target:
            changes.append((note.id, target))
        counts_after[target] += 1
    return changes, dict(counts_after)


def head(counts: dict[str, int]) -> list[tuple[str, int]]:
    """The labels M8 will offer as groups: at least SCENE_SUPPORT memories."""
    groups = {name: n for name, n in counts.items() if name and n >= IndexSummary.SCENE_SUPPORT}
    return sorted(groups.items(), key=lambda row: (-row[1], row[0]))


def label_count(counts: dict[str, int]) -> int:
    return sum(1 for name in counts if name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--store", type=Path, default=Path(".memory"), help="store to read")
    parser.add_argument(
        "--apply", action="store_true", help="write the merges (default: report only)"
    )
    args = parser.parse_args()

    store = _RawStore(args.store)
    changes, after_counts = plan(store)
    before_counts = Counter(note.scene for note in store.active() if note.scene)
    before, after = head(dict(before_counts)), head(after_counts)

    print(f"store: {args.store}")
    print(f"notes: {len(store.all())}")
    print(f"labels: {label_count(dict(before_counts))} -> {label_count(after_counts)}")
    print(f"groups (>= {IndexSummary.SCENE_SUPPORT} memories): {len(before)} -> {len(after)}")
    print(f"  before: {'、'.join(name for name, _ in before) or '(none)'}")
    print()
    print(f"rewriting {len(changes)} notes:")
    for note_id, target in changes:
        print(f"  {note_id}  ->  {target}")
    print()
    print("groups after the merge:")
    for name, count in after:
        print(f"  {count:>3}  {name}")
    untouched = sorted(set(before_counts) - set(SCENE_SYNONYMS) - {""})
    if untouched:
        print()
        print(f"left alone ({len(untouched)}): {'、'.join(untouched)}")

    if not args.apply:
        print()
        print("dry run — pass --apply to write")
        return 0
    if not changes:
        print()
        print("nothing to do")
        return 0
    # The old spelling is the only thing the merge destroys, so it is written
    # next to the store before anything is overwritten.  Restoring is then a
    # read of this file, not a guess.
    backup = args.store.with_suffix(args.store.suffix + ".scenes.bak")
    backup.write_text(
        json.dumps(
            {
                note.id: note.scene
                for note in store.all()
                if fold_scene(note.scene) != note.scene
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    written = 0
    for note in store.all():
        target = fold_scene(note.scene)
        if target != note.scene and store.update(replace(note, scene=target)):
            written += 1
    print()
    print(f"wrote {written} notes; old labels kept in {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
