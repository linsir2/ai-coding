"""``memory_search`` — on-demand recall (R3b).

Why a tool and not another prompt section: a tool result is **appended to the
history**, so it is cached from then on, while anything injected per turn is paid
for every turn (docs/r3-analysis.md 1.13: 94% vs 7% prefix-hit on the same
content).  It also lets the model ask *precisely* ("what did we decide about
dependency management?") instead of the loop guessing on every request.

Read-only, so the permission gate allows it without asking the user.
"""

from __future__ import annotations

from typing import Any

from ai_coding.core.events import Degraded
from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["MemorySearchTool"]


class MemorySearchTool(BaseTool):
    """Search the durable memory store and return structured notes."""

    parallel_safe = True
    mutates_world = False

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "memory_search"

    @property
    def description(self) -> str:
        return (
            "Search your own durable memory (project conventions, user preferences, "
            "decisions made in earlier sessions) and return the best matches with "
            "their ids and scores. Use it when the conversation refers to something "
            "that was decided or configured before, or when the user asks why "
            "something is the way it is. When nothing matches it also reports which "
            "query words memory has never seen, the notes closest to the match, and "
            "a bounded map of the index. Note that terms_known is measured over the "
            "notes the filters left, so it covers the whole index on a miss but only "
            "the candidates on a hit."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to look up (a topic, file, tool or decision).",
                },
                "k": {
                    "type": "integer",
                    "description": "How many notes to return (default 3).",
                },
                "kind": {
                    "type": "string",
                    "description": "Optional filter: fact | decision | preference | open_thread.",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Optional filter: 'tool' = notes recorded in a turn where a "
                        "tool call succeeded; 'model' = everything else (including "
                        "notes from before this was recorded)."
                    ),
                },
                "include_near_misses": {
                    "type": "boolean",
                    "description": (
                        "Include the closest notes that did not clear the gate. "
                        "By default they are returned only when nothing matched; "
                        "pass true to also get them alongside a hit."
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Optional: only notes whose scope matches exactly. Notes "
                        "written by this build are all 'workspace', so this only "
                        "narrows notes that were imported or written with another "
                        "scope."
                    ),
                },
                "since_days": {
                    "type": "integer",
                    "description": (
                        "Optional: only memories changed within the last N days "
                        "(falls back to the creation date when nothing has been "
                        "updated since)."
                    ),
                },
                "scene": {
                    "type": "string",
                    "description": (
                        "Optional: only notes in this topic group, matched exactly. "
                        "The groups are short free-text labels the extractor wrote "
                        "(e.g. '依赖管理'); when a search misses, `index.top_scenes` "
                        "lists the labels this memory actually uses."
                    ),
                },
            },
            "required": ["query"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        query = str(params.get("query", "") or "").strip()
        if not query:
            query = _query_from_context(resolve_ctx(ctx))
        if not query:
            return ToolResult(
                success=False,
                output="error: 'query' is required",
                error="empty_query",
                data={"query": ""},
            )
        wanted_evidence = str(params.get("evidence", "") or "").strip().casefold()
        if wanted_evidence and wanted_evidence not in ("tool", "model"):
            # Validated before the recall, so a bad value never reaches memory.
            return ToolResult(
                success=False,
                output="error: 'evidence' must be 'tool', 'model' or empty",
                error="bad_evidence",
                data={"evidence": wanted_evidence},
            )
        wanted_kind = str(params.get("kind", "") or "").strip().casefold()
        wanted_scope = str(params.get("scope", "") or "").strip()
        requested_near_misses = params.get("include_near_misses")
        # Normalised here, applied by the service: filters that narrow the candidate
        # set are the only path, so `k` is never spent on notes the model did not
        # ask for (and a filtered-out note is never recalled, so never touched).
        filters = {
            "kinds": [wanted_kind] if wanted_kind else None,
            "evidence": wanted_evidence or None,
            "scope": wanted_scope or None,
            "since_days": _optional_positive_int(params.get("since_days")),
            "scene": str(params.get("scene", "") or "").strip() or None,
        }
        try:
            detailed = getattr(self._memory, "recall_with_diagnostics", None)
            if detailed is None:
                # A memory double from before R10c: keep it working.  It cannot take
                # the filters, so they do not apply on this path.
                hits = await self._memory.recall_notes(query, k=_as_int(params.get("k"), 3))
                diagnostics = _EMPTY_DIAGNOSTICS
            else:
                hits, diagnostics = await detailed(
                    query, k=_as_int(params.get("k"), 3), **filters
                )
        except Exception as exc:  # noqa: BLE001 — the tool boundary
            return ToolResult(
                success=False,
                output=f"memory search failed: {exc}",
                error="memory_error",
                data={"query": query},
            )
        notes = [_note_payload(hit) for hit in hits]
        # A hit is the common case and does not need a diagnosis attached; the
        # model opts in when it wants candidates it can aim a follow-up at.
        ship_near_misses = (
            (not notes) if requested_near_misses is None else bool(requested_near_misses)
        )
        data: dict[str, Any] = {
            "notes": notes,
            "no_match": not notes,
            "terms_known": list(diagnostics.terms_known),
            "terms_unknown": list(diagnostics.terms_unknown),
            "semantic_unavailable": bool(diagnostics.semantic_unavailable),
            # M-F1: the tier's four states, read with `getattr` because a
            # pre-R10c double has no such field and inventing one would be
            # exactly what this payload must not do (same discipline as
            # `timed_out` below).  Without it an operator who set
            # `enabled=True` against an endpoint without `/embeddings` gets no
            # signal that the configuration did nothing.
            #
            # Read off `diagnostics` -- the *return value* -- not `detailed`,
            # which is the bound method and has no such attribute, so the
            # fallback always won and the payload said "off" unconditionally.
            "semantic_status": getattr(diagnostics, "semantic_status", "off"),
            # R11b: getattr, because a pre-R10c double has no such field and
            # inventing a timeout for it would be exactly what this text must
            # not do (same duck-typing discipline as `_filtered_out` below).
            "timed_out": bool(getattr(diagnostics, "timed_out", False)),
        }
        if data["timed_out"]:
            # The hits still come back (a timeout must never starve the model);
            # what was skipped is the optional `touch` write, and that skip is
            # what this event records.  Best-effort: observation never breaks
            # the tool, and a context built outside the loop just no-ops.
            try:
                elapsed = float(getattr(diagnostics, "elapsed_seconds", 0.0))
                resolve_ctx(ctx).emit(
                    Degraded(
                        component="memory_recall",
                        reason=f"recall exceeded its time budget after {elapsed:.2f}s",
                        fallback=(f"returned {len(notes)} hit(s) without recording use"),
                    )
                )
            except Exception:  # noqa: BLE001 — observation never breaks the tool
                pass
        if ship_near_misses:
            data["near_misses"] = [
                {
                    "id": hit.note.id,
                    "kind": hit.note.kind.value,
                    "score": round(hit.score, 3),
                    "relevance": round(hit.relevance, 3),
                    "preview": hit.note.text[:40],
                }
                # A candidate that shares no word with the query is not close to
                # anything, however well it scores on recency alone.
                for hit in diagnostics.near_misses[:3]
                if hit.matched_terms
            ]
        if not notes:
            summary = diagnostics.index_summary
            if summary is not None:
                data["index"] = {
                    # "active", not "total": status() reserves total for all notes
                    # including superseded ones, and this counts only the active set.
                    "active": summary.active,
                    "by_kind": dict(summary.by_kind),
                    "recent": [
                        {"id": note_id, "kind": kind, "preview": preview}
                        for note_id, kind, preview in summary.recent
                    ],
                }
                # R11c: the scene map is omitted entirely for a library that has
                # none, so an unscoped workspace pays nothing for this feature.
                if summary.by_scene:
                    data["index"]["by_scene"] = dict(summary.by_scene)
                # Only the head is navigation.  `top_scenes` is what the schema
                # description points a model at, so a label that is not a group
                # must not appear: it cannot narrow anything, and filtering on it
                # costs recall.  Omitted entirely when nothing has repeated.
                if summary.top_scenes:
                    data["index"]["top_scenes"] = [
                        [scene, count, heat] for scene, count, heat in summary.top_scenes
                    ]
            return ToolResult(
                success=True,
                output=_miss_text(
                    data,
                    # Measured by the service against the unfiltered index, and
                    # attributed to the filters that were actually asked for.
                    filtered_out=_filtered_out(diagnostics, filters),
                    filters=_filter_names(filters),
                    no_vocabulary_match=bool(diagnostics.no_vocabulary_match),
                    ignored_scene=_ignored_scene(diagnostics),
                ),
                data=data,
            )
        lines = [
            f"- [{note['kind']}#{note['id'][:8]} score={note['score']} "
            f"matched={'/'.join(note['matched_terms'])}] {note['text']}"
            for note in notes
        ]
        if _ignored_scene(diagnostics):
            # On a hit too, not only on a miss: the model set a filter that was
            # not applied, and a silent drop would read as "these are the notes in
            # that group".
            lines.append(_ignored_scene_note(_ignored_scene(diagnostics)))
        return ToolResult(success=True, output="\n".join(lines), data=data)


def _note_payload(hit: Any) -> dict[str, Any]:
    return {
        "id": hit.note.id,
        "kind": hit.note.kind.value,
        "score": round(hit.score, 3),
        "relevance": round(hit.relevance, 3),
        "matched_terms": list(hit.matched_terms),
        "text": hit.note.text,
        "updated_time": hit.note.updated_time or hit.note.created_time,
        "pinned": hit.note.pinned,
        "evidence": hit.note.evidence,
        # R11c: positions in `sessions/<source_session>.json`, always present as a
        # list so the payload shape does not depend on whether this note had a
        # transcript to point at.
        "source_refs": list(hit.note.source_message_ids),
    }


def _miss_text(
    data: dict[str, Any],
    *,
    filtered_out: int = 0,
    ignored_scene: str = "",
    filters: str = "",
    no_vocabulary_match: bool = False,
) -> str:
    """The dead end, replaced by something the model can act on.

    Every clause has to be one we actually measured.  A count we never read, a
    "no match" that our own filter caused, or a relevance gate a pre-R10c double
    never ran are all lies, and this text is what the model reads.
    """
    index = data.get("index")
    if index is None:
        # No summary means memory is off, the ranker raised, or the double
        # predates diagnostics — none of which means the index is empty.
        lines = ["没有可靠匹配（读不到索引概览，不知道记忆里有多少条）。"]
    else:
        by_kind = index.get("by_kind") or {}
        breakdown = ", ".join(f"{k} {v}" for k, v in by_kind.items()) or "空"
        lines = [f"没有可靠匹配（索引里有 {index['active']} 条：{breakdown}）。"]
    unknown = data["terms_unknown"]
    if no_vocabulary_match:
        # What was measured: no query term has df > 0 in the candidate set.
        # That says nothing about whether the *content* is stored — a CJK
        # cross-word bigram like 么构 out of 「怎么构建」 would appear in no
        # relevant note, so its absence carries no information about content.
        # Claiming otherwise asserts a fact about the corpus from a
        # measurement about tokens, and it contradicted the advice line one
        # below it (换个说法), which the spec 5.2 makes the whole point of
        # naming these words.
        lines.append(
            f"这些词索引里一条笔记都没出现过：{'、'.join(unknown[:6])}"
            "（只说明这些词本身没被记过，不说明记忆里有没有相关内容）。"
        )
    elif unknown:
        lines.append(f"这些词索引里没有：{'、'.join(unknown[:6])}。")
    near = data.get("near_misses") or []
    if near:
        preview = "；".join(
            f"#{item['id'][:8]}({item['kind']}) {item['preview']}" for item in near[:3]
        )
        lines.append(f"最接近但未经证实：{preview}")
    scenes = (index or {}).get("top_scenes") or []
    if scenes:
        # The labels are the library's own words, which is the point: a model
        # that guessed a scene name can copy one of these verbatim instead of
        # inventing a second spelling for the same group.
        listed = "、".join(
            f"{scene} {count} 条（被召回 {heat} 次）" for scene, count, heat in scenes[:5]
        )
        # Measured: a label is not a query. A live run took the top scene 「CI/CD」
        # and searched it verbatim; its note read 「CI 使用 GitHub Actions」, so the
        # search missed a note the map had named correctly. A scene narrows the
        # candidate set — the query still has to come from the question.
        lines.append(
            f"记忆里的场景分组：{listed}；可用 scene=<场景> 收窄候选（查询词仍要自己写）。"
        )
    if ignored_scene:
        # Said where the model can act on it: the filter it set was not wrong, it
        # named a label too thin to narrow — and the labels that *can* narrow are
        # the ones just listed above.
        lines.append(_ignored_scene_note(ignored_scene))
    if filtered_out:
        # Saying "rephrase, or narrow by kind" here would be telling the model to
        # repeat the filter it just used, and blaming kind/evidence for a scope
        # miss would point it at a filter it never set.
        blamed = filters or "kind/evidence"
        lines.append(
            f"有 {filtered_out} 条通过了相关度门槛，"
            f"是 {blamed} 过滤把它们排除了：放宽过滤条件再搜一次。"
        )
        return "\n".join(lines)
    if not near and not unknown and data["terms_known"] and index is not None:
        # Every word was known and nothing was close: the gate said no.  Gated
        # on a measured index, because otherwise we cannot tell a real rejection
        # from a double whose recall told us nothing — **and** on a non-empty
        # `terms_known`, because `rank_notes_detailed` returns a bare result
        # (naming neither known nor unknown words) when there are no active
        # notes, or when the query tokenises to nothing.  An empty
        # `terms_unknown` reads as "no word was unknown"; only a non-empty
        # `terms_known` reads as "every word was known".
        lines.append("查询用词索引都认识，但没有候选达到相关度门槛。")
    if filters:
        # The model already narrowed by whatever it set; offering to narrow again
        # is the same self-contradiction the filtered-out branch exists to avoid.
        lines.append("换一个说法再搜，或放宽刚才的过滤条件。")
    elif index is not None and not index["active"]:
        # Measured: there is nothing stored, so no rephrasing can match it.
        # This is the first `memory_search` on a fresh workspace, and the
        # generic advice would send the model round the same empty loop.
        lines.append("记忆里一条笔记都没有，没有东西可搜：先往记忆里写一条再搜。")
    else:
        lines.append("换一个说法再搜，或用 kind=decision/fact/preference/open_thread 缩小范围。")
    return "\n".join(lines)


class _EmptyDiagnostics:
    """Stand-in for a memory double that predates ``recall_with_diagnostics``."""

    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    near_misses: tuple[Any, ...] = ()
    index_summary: Any = None
    semantic_unavailable: bool = False
    no_vocabulary_match: bool = False
    filtered_out: int = 0
    ignored_scene: str | None = None


_EMPTY_DIAGNOSTICS = _EmptyDiagnostics()


def _ignored_scene(diagnostics: Any) -> str:
    """The ``scene=`` the service refused to apply, or "" when it applied it.

    ``getattr`` because the diagnostics object is duck-typed like the rest: a
    double from before this field has no claim to make.
    """
    return str(getattr(diagnostics, "ignored_scene", None) or "")


def _ignored_scene_note(scene: str) -> str:
    """The sentence that tells the model its filter was not the one it thinks."""
    return (
        f"scene={scene} 这个标签没有用来收窄：它只出现在一条记忆上，"
        f"按它过滤只会丢掉其他记忆。"
    )


def _filter_names(filters: dict[str, Any]) -> str:
    """The parameters the caller actually set, named as the schema names them."""
    return "/".join(
        {"kinds": "kind"}.get(name, name) for name, value in filters.items() if value
    )


def _filtered_out(diagnostics: Any, filters: dict[str, Any]) -> int:
    """How many notes the filters cost this miss, or 0 when nothing claims a cause.

    Read with ``getattr`` because ``recall_with_diagnostics`` is duck-typed: a
    double that predates the field reports no count, and inventing one is exactly
    what this text must not do.
    """
    if not any(filters.values()):
        return 0
    try:
        return max(0, int(getattr(diagnostics, "filtered_out", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _optional_positive_int(value: Any) -> int | None:
    """A window in days, or nothing at all — 0 and negatives mean "no window"."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _query_from_context(ctx: ToolContext) -> str:
    """Fall back to the parent turn's view when the model omits the query."""
    for message in reversed(ctx.messages):
        body = (message.content or "").strip()
        if message.role == "user" and body:
            return body
    return ""
