"""R11c acceptance: does the scene/provenance pair survive a real run?

R11c shipped two producers for `scene` (the extractor prompt and
`memory_write`), a bounded scene map in the miss payload, and position
references into the session transcript. Unit tests pin the *shapes*; this
script answers whether the producers actually produce anything:

  C1 adoption     — do candidates come back with a scene at all?
  C2 granularity  — are the labels few and short enough to filter on?
  C3 dedupe       — does an identical text still collapse to one note?
  C4 refs         — do the positions exist and stay inside the window?
  C5 navigation   — miss text names a label, and searching it finds the note?
  C6 no regression— `retrieval_cases --check` is bit-identical (run separately)

The deciding logic lives in :func:`analyze` and
:func:`check_navigation_roundtrip`, both pure and both tested offline in
``tests/test_r11c_measure.py`` — real tokens are only spent after the thing
that reads them is known to be right.

Offline (no model, no network)::

    .venv/bin/python scripts/r11c_cases.py

Real model, in a throwaway workspace so your own ``.memory`` is never
touched::

    AI_CODING_R11C_IT=1 .venv/bin/python scripts/r11c_cases.py --real \\
        --config config.local.yaml

Why the gate: the live arm spends money and needs a provider. The bars below
are fixed **before** the run; a failing bar is reported, never re-tuned (and
the extractor prompt is *not* edited to chase adoption).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.memory.note import MemoryNote  # noqa: E402
from ai_coding.memory.schema import HarvestCandidate  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.memory.tokenize import normalize  # noqa: E402

SEP = "-" * 68
GATE = "AI_CODING_R11C_IT"

#: C1: at least this share of extracted candidates must carry a scene. Below
#: half, the map and the filter are decoration and the feature should be cut or
#: re-scoped rather than kept "for later".
ADOPTION_BAR = 0.5
#: C2: a filter is only usable if the library's labels are few and short. Five
#: distinct labels per session is what a human can hold in their head; 12
#: characters is what fits the navigation line without bloating the payload.
MAX_DISTINCT_SCENES = 5
MAX_SCENE_CHARS = 12

#: The label the miss text tells the model to re-query with.
_SCENE_HINT = re.compile(r"scene=([^\s，。；、]+)")


@dataclass(frozen=True)
class Report:
    """What one batch's real output says about R11c. Rates, not verdicts alone:
    a rate can be read against a bar and argued with."""

    candidates: int
    labelled: int
    legacy: int
    distinct_scenes: int
    longest_scene_chars: int
    refs_out_of_range: tuple[str, ...]
    refs_too_long: tuple[str, ...]
    ref_counts: frozenset[tuple[str, ...]]
    #: True when no two active notes share normalised text. Vacuously true for a
    #: library with no repeats — the store's own dedupe is what collapses them,
    #: and ``test_r11c_the_same_text_twice_must_stay_one_note`` pins that.
    duplicate_texts_collapsed: bool
    notes: int

    @property
    def adoption_rate(self) -> float:
        return (self.labelled / self.candidates) if self.candidates else 0.0

    def reasons(self, adoption: float | None = None) -> list[str]:
        """Every violated claim, in the words the docs use.

        ``adoption`` lets the caller substitute the *pooled* rate: one batch can
        hold a single candidate, and judging C1 on 1-of-1 measures the model's
        mood, not the prompt.
        """
        rate = self.adoption_rate if adoption is None else adoption
        out: list[str] = []
        if rate < ADOPTION_BAR:
            out.append(
                f"C1 adoption {rate:.0%} < {ADOPTION_BAR:.0%}: the scene producers "
                "wrote nothing worth filtering on"
            )
        if self.distinct_scenes > MAX_DISTINCT_SCENES:
            out.append(
                f"C2 {self.distinct_scenes} distinct scenes > {MAX_DISTINCT_SCENES}: "
                "the labels are free text, so filtering needs luck"
            )
        if self.longest_scene_chars > MAX_SCENE_CHARS:
            out.append(
                f"C2 a scene label is {self.longest_scene_chars} characters > "
                f"{MAX_SCENE_CHARS}: it will bloat the navigation line"
            )
        if self.refs_out_of_range:
            out.append(
                f"C4 references name messages that do not exist: "
                f"{', '.join(self.refs_out_of_range)}"
            )
        if self.refs_too_long:
            out.append(f"C4 more references than the window allows: {self.refs_too_long}")
        if not self.duplicate_texts_collapsed:
            out.append("C3 two active notes share the same text")
        return out

    def passed(self, adoption: float | None = None) -> bool:
        return not self.reasons(adoption)


def pooled_adoption(reports: Sequence[Report]) -> tuple[float, int, int]:
    """``(rate, labelled, total)`` over every batch of a run.

    The extractor is a model: the same conversation yields a candidate on one
    turn and nothing on the next.  Judging C1 per batch would read 1-of-1 as
    100%, so the bar is applied to the pool and the per-batch rate is only ever
    printed as detail.
    """
    labelled = sum(report.labelled for report in reports)
    total = sum(report.candidates for report in reports)
    return ((labelled / total) if total else 0.0, labelled, total)


def analyze(
    candidates: Sequence[Any],
    notes: Sequence[MemoryNote],
    *,
    messages: Sequence[Any],
    window: int,
) -> Report:
    """Measure C1–C4 over what a real batch produced.

    ``candidates`` is whatever the extractor returned, which is *not* always
    ``HarvestCandidate`` objects.  ``legacy`` counts the ones that arrived as
    bare strings — a line can never carry a scene, so it must stay in the
    denominator; dropping it would make adoption look better than it is.

    That count is **0 since the malformed-answer fix**, and that is correct
    rather than a regression.  The plain-line fallback used to fire only when
    ``parse_candidates`` came back empty, which in practice meant it re-read
    *broken* JSON — so it was counting our own parser defect, not "the model
    ignored the JSON contract".  Genuine line-per-note answers now go through
    ``parse_candidates`` and arrive as ``HarvestCandidate`` without a scene, so
    they land in ``structured``/``labelled=0`` where they still count against
    C1.  Keep the field: it is what would catch a second route back to bare
    strings.

    ``messages`` is the session's transcript after the turn, so a reference is
    "real" exactly when it names a message that exists there. ``window`` is the
    harvest window (``memory.harvest_max_messages``): a note may not claim more
    messages than the extractor was shown, whatever those messages were.
    """
    total = len(candidates)
    structured = [c for c in candidates if isinstance(c, HarvestCandidate)]
    legacy = total - len(structured)
    labelled = [c for c in structured if (c.scene or "").strip()]
    scenes = {c.scene.strip() for c in labelled}
    limit = max(1, int(window))
    out_of_range: list[str] = []
    too_long: list[str] = []
    ref_tuples: set[tuple[str, ...]] = set()
    for note in notes:
        refs = tuple(note.source_message_ids)
        if not refs:
            continue  # memory_write, or a merge winner: nothing to point at
        ref_tuples.add(refs)
        if len(refs) > limit:
            too_long.append(f"{note.id[:8]}({len(refs)})")
        for ref in refs:
            index = _index_of(ref)
            if index is None or not 0 <= index < len(messages):
                out_of_range.append(ref)
    texts: dict[str, int] = {}
    for note in notes:
        key = normalize(note.text)
        texts[key] = texts.get(key, 0) + 1
    return Report(
        candidates=total,
        labelled=len(labelled),
        legacy=legacy,
        distinct_scenes=len(scenes),
        longest_scene_chars=max((len(scene) for scene in scenes), default=0),
        refs_out_of_range=tuple(sorted(set(out_of_range))),
        refs_too_long=tuple(sorted(set(too_long))),
        ref_counts=frozenset(ref_tuples),
        duplicate_texts_collapsed=all(count == 1 for count in texts.values()),
        notes=len(notes),
    )


async def check_navigation_roundtrip(
    tool: Any, probe_query: str, notes: Sequence[MemoryNote]
) -> tuple[bool, str]:
    """C5: a miss names a label, and that label is a filter that holds.

    Two halves, both measurable:

    1. the miss text names a scene, in prose as well as in the payload;
    2. searching with ``scene=<label>`` returns a note **from that scene**, and
       searching with a *different* scene does not.

    It deliberately does **not** search the label as a query, and it does not
    assume the hottest scene owns the newest note — two live runs falsified both:
    the top label was 「CI/CD」 while its note read 「CI 使用 GitHub Actions」 (the
    label as a query matched nothing), and `top_scenes[0]` was 「发布」 while
    `recent[0]` was a 构建 note. A scene narrows; the query still comes from the
    question, and the payload deliberately does not say which note sits in which
    scene — so the target is resolved from the store, which the caller owns.
    """
    miss = await tool.execute({"query": probe_query})
    if miss.data and miss.data.get("notes"):
        return False, f"probe query was not a miss: {probe_query!r}"
    index = (miss.data or {}).get("index") or {}
    labels = [str(row[0]) for row in index.get("top_scenes") or []]
    if not labels:
        return False, f"the miss text named no scene at all: {miss.output[:80]!r}"
    label = labels[0]
    if label not in miss.output:
        return False, f"scene {label!r} is in the payload but not in the prose"
    target = next((note for note in notes if note.scene == label), None)
    if target is None:
        return False, f"the map names scene {label!r} but no stored note carries it"
    hit = await tool.execute({"query": target.text, "scene": label})
    found = (hit.data or {}).get("notes") or []
    if not any(note["id"] == target.id for note in found):
        return (
            False,
            f"scene {label!r} did not return its own note {target.id[:8]} "
            f"(got {[n['id'][:8] for n in found]})",
        )
    other = next((note.scene for note in notes if note.scene and note.scene != label), "")
    if other:
        crossed = await tool.execute({"query": target.text, "scene": other})
        if any(note["id"] == target.id for note in (crossed.data or {}).get("notes") or []):
            return False, f"note leaked across scenes into {other!r}"
    return True, f"scene {label!r} -> {target.id[:8]} {target.text[:24]!r}"


def _index_of(ref: str) -> int | None:
    text = str(ref)
    if not text.startswith("m"):
        return None
    try:
        return int(text[1:])
    except ValueError:
        return None


# ---- the offline arm -------------------------------------------------------


async def _offline() -> int:
    """The claims, demonstrated on a hand-built store. No model, no network."""
    from ai_coding.tools.memory_search_tool import MemorySearchTool

    print(f"{SEP}\noffline (no model): the claims on a hand-built library")
    print(SEP)
    candidates = [
        HarvestCandidate(text="项目用 uv 管理依赖", scene="依赖管理"),
        HarvestCandidate(text="构建走 make 再跑 lint", scene="构建"),
    ]
    notes = [
        MemoryNote(id="n1", text="项目用 uv 管理依赖", scene="依赖管理",
                   source_message_ids=("m0", "m1"),
                   created_time="2026-10-01T00:00:00+00:00"),
        MemoryNote(id="n2", text="构建走 make 再跑 lint", scene="构建",
                   source_message_ids=("m1", "m2"),
                   created_time="2026-10-01T00:00:00+00:00"),
    ]
    messages = ["我们决定统一用 uv", "构建请走 make", "并跑 lint"]
    report = analyze(candidates, notes, messages=messages, window=3)
    _print_report("offline", report)

    with tempfile.TemporaryDirectory() as directory:
        store = MemoryStore(Path(directory) / ".memory")
        store.add_note(notes[0])
        store.add_note(notes[1])
        tool = MemorySearchTool(MemoryService(store))
        ok, detail = await check_navigation_roundtrip(tool, "量子纠缠的退相干时间", store.active())
    print(f"  {'PASS' if ok else 'FAIL'}  C5 navigation: {detail}")
    print(SEP)
    return 0 if (report.passed() and ok) else 2


def _print_report(tag: str, report: Report) -> None:
    print(
        f"  {'PASS' if report.passed() else 'FAIL'}  {tag}: "
        f"candidates={report.candidates} adoption={report.adoption_rate:.0%} "
        f"scenes={report.distinct_scenes} longest={report.longest_scene_chars} "
        f"notes={report.notes}"
    )
    for reason in report.reasons():
        print(f"          - {reason}")


# ---- the real arm ----------------------------------------------------------

CONVENTIONS = (
    "项目约定（请记住）：这个仓库统一用 uv 管理依赖，禁止使用 pip 安装任何东西。",
    "构建约定：请用 make 跑 lint 再编译，不要直接调用 gcc。",
    "CI 跑在 GitHub Actions 上，发布必须走 tag 触发。",
)
PROBES = (
    "量子纠缠的退相干时间是多少",
    "海王星有几个卫星",
    "蒙娜丽莎的作者是谁",
)


async def _real(config_path: str) -> int:
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.ai.llm_helpers import build_extractor
    from ai_coding.ai.prompt import BASE_INSTRUCTIONS, PromptAssembler
    from ai_coding.config.loader import ConfigLoader
    from ai_coding.config.models import AIConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.core.context_compression import ContextManager
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.memory_search_tool import MemorySearchTool
    from ai_coding.tools.registry import ToolRegistry

    app_cfg = ConfigLoader.load_app(config_path)
    model = app_cfg.models[app_cfg.default_model]
    print(f"model={app_cfg.default_model} base_url={model.base_url}")
    print(f"scene bar: adoption>={ADOPTION_BAR:.0%}, "
          f"<={MAX_DISTINCT_SCENES} scenes/session, <={MAX_SCENE_CHARS} chars")

    sessions_run = 3
    harvest_window = int(getattr(app_cfg.memory, "harvest_max_messages", 12))
    passed = failed = 0
    reports: list[Report] = []
    for index in range(sessions_run):
        workspace = Path(tempfile.mkdtemp(prefix=f"r11c_{index}_"))
        recorded: list[Any] = []
        base_extractor = build_extractor(model)

        async def recording(
            messages, conclusion=None, _base=base_extractor, _sink=recorded
        ):
            """Record what the extractor produced, so adoption is measured on the
            model's own output rather than on what survived reconciliation.

            Items are kept as they arrive — including the plain-string fallback
            ``build_extractor`` uses when the model ignores the JSON contract —
            because a line can never carry a scene and would otherwise vanish
            from the denominator."""
            out = await _base(messages, conclusion)
            _sink.extend(out)
            return out

        memory = MemoryService(
            MemoryStore(workspace / ".memory"),
            extractor=recording,
            config=app_cfg.memory,
        )
        settings = AIConfig(stop_policy="natural", max_tool_iterations=1)
        engine = build_engine_from_config(app_cfg)
        loop = AgentLoop(
            engine,
            SessionService(workspace / "sessions"),
            ToolRegistry(),
            context=ContextManager(settings, transcripts_root=workspace),
            memory=memory,
            prompt=PromptAssembler(BASE_INSTRUCTIONS),
            settings=settings,
            workspace=str(workspace),
        )
        session = loop.sessions.create()
        for convention in CONVENTIONS:
            await loop.process_input(session, convention)
        await loop.aclose()

        notes = memory.store.active()
        report = analyze(
            recorded,
            notes,
            messages=list(session.messages),
            window=harvest_window,
        )
        reports.append(report)
        print(f"\nsession {index + 1}: workspace={workspace}")
        for note in notes:
            print(f"    [{note.kind.value}#{note.id[:8]} scene={note.scene or '-'}] "
                  f"refs={list(note.source_message_ids)} {note.text[:36]}")
        _print_report(f"session {index + 1} (detail only)", report)
        # C5 from a **freshly loaded** store: if the scene map only works on the
        # in-memory object that produced it, it does not work for the next
        # session, which is the only case that matters.
        reloaded = MemoryService(MemoryStore(workspace / ".memory"), config=app_cfg.memory)
        ok, detail = await check_navigation_roundtrip(
            MemorySearchTool(reloaded), PROBES[index % len(PROBES)], notes
        )
        print(f"  {'PASS' if ok else 'FAIL'}  C5 navigation (reloaded store): {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    rate, labelled, total = pooled_adoption(reports)
    legacy = sum(report.legacy for report in reports)
    print(SEP)
    print(f"pooled C1 adoption: {rate:.0%} ({labelled}/{total} candidates carried a scene; "
          f"{legacy} arrived as plain lines the JSON contract could not carry)")
    if total < 5:
        print(f"  note: only {total} candidates — too few to call 50% a pass or a fail")
    pooled_ok = rate >= ADOPTION_BAR or total < 5
    other_ok = all(report.passed(rate) for report in reports)
    print(f"  {'PASS' if pooled_ok else 'FAIL'}  C1 pooled adoption "
          f"({rate:.0%} vs bar {ADOPTION_BAR:.0%})")
    print(f"  {'PASS' if other_ok else 'FAIL'}  C2/C3/C4 across sessions")
    passed += int(pooled_ok) + int(other_ok)
    failed += (not pooled_ok) + (not other_ok)
    print(SEP)
    print(f"SUMMARY {passed}/{passed + failed} passed ({sessions_run} real sessions)")
    return 0 if failed == 0 else 2


async def _main(real: bool, config_path: str) -> int:
    if real and os.environ.get(GATE) != "1":
        print(f"set {GATE}=1 to spend tokens on the live arm")
        return 0
    return await _real(config_path) if real else await _offline()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true",
                        help="drive a real model through a real turn (needs a provider)")
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.real, args.config))


if __name__ == "__main__":
    raise SystemExit(main())
