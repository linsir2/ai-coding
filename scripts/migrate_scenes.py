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

from ai_coding.memory.note import normalize_scene
from ai_coding.memory.service import IndexSummary
from ai_coding.memory.store import MemoryStore

#: old label -> the label that replaces it.  A value that is also a key means
#: the whole family collapses onto it (e.g. all three 记忆 spellings -> 记忆).
SPELLING_MERGES: dict[str, str] = {
    "记忆模块": "记忆",
    "记忆系统": "记忆",
    "记忆配置": "记忆",
    "项目概况": "项目",
    "项目环境": "项目",
    "项目结构": "项目",
    "环境依赖": "环境",
    "环境信息": "环境",
    "环境版本": "环境",
    "环境约束": "环境",
    "环境配置": "环境",
    "文档规范": "文档",
    "文档组织": "文档",
    "架构演进": "架构",
    "架构约束": "架构",
    "配置管理": "配置",
    "配置约定": "配置",
    "MCP架构": "MCP",
    "MCP设置": "MCP",
    "开发命令": "开发流程",
    "执行约束": "开发流程",
    "分支管理": "开发流程",
    "工具环境": "工具",
    "工具约束": "工具",
    "UI状态栏": "界面",
    "UI组件": "界面",
    "酒店管理系统": "工具",
    "路由检查": "依赖",
    "测试质量": "测试",
    "模型配置": "技术",
    "权限控制": "权限",
}


def plan(store: MemoryStore) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """``(changes, counts_after)`` for this store, computed without touching it.

    ``counts_after`` is what the labels become, so the caller can see the head
    the merge would produce before deciding to write it.
    """
    changes: list[tuple[str, str]] = []
    counts_after: Counter[str] = Counter()
    for note in store.all():
        scene = note.scene
        target = SPELLING_MERGES.get(scene, scene)
        target = normalize_scene(target)
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

    store = MemoryStore(args.store)
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
    untouched = sorted(set(before_counts) - set(SPELLING_MERGES) - {""})
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
                if normalize_scene(SPELLING_MERGES.get(note.scene, note.scene)) != note.scene
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    written = 0
    for note in store.all():
        target = normalize_scene(SPELLING_MERGES.get(note.scene, note.scene))
        if target != note.scene and store.update(replace(note, scene=target)):
            written += 1
    print()
    print(f"wrote {written} notes; old labels kept in {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
