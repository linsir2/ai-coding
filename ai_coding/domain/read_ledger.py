"""The read ledger — what this session has actually read, and where the gaps are.

RC-2 (docs/r20-t2-rootcause.md): the memory layer stored *conclusions*, not
*evidence*.  The checkpoint said "读取 ai_coding/tools/present_plan_tool.py 源码"
with no line numbers, so after one compaction the model could not tell "I read
this file" from "I never opened it" — and re-read the same 441-line file eleven
times at overlapping offsets (195, 300, 195, 140, 55, 25, 195, 195, 195, 245,
360, 384).

The design answer (section 1 of the plan): the ledger does **not** pretend the
content survives.  It records "which lines, and that the content is gone" and
tells the model to re-read the gaps — the opencode/codex split, where the
pointer survives compaction and the content does not.

Everything here is a pure function over values: no IO, no session, no clock.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

#: The file suffixes a citation can plausibly name.  Opened up to any token and
#: ordinary prose ("foo.bar") would read as a file the model never touched.
_SOURCE_SUFFIXES = (
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt",
    ".toml", ".yaml", ".yml", ".cfg", ".ini", ".sh", ".go", ".rs",
    ".java", ".rb", ".c", ".h", ".cpp", ".hpp",
)

_SUFFIX_ALT = "|".join(
    sorted({suffix.lstrip(".") for suffix in _SOURCE_SUFFIXES})
)

#: What a source path looks like, without the edges.  The edges live with the
#: users because they differ: a token in a prose scan must not start mid-word or
#: inside a URL, and a citation's path must not swallow the ``c`` of ``a.pyc``.
#:
#: The suffix list is deliberately local.  ``core/file_mentions.py`` holds the
#: repository's other answer to the same question, and the two have drifted --
#: but ``domain/`` does not import ``core/`` anywhere, and creating that edge
#: for a regex fragment is the worse trade.
_PATH_SHAPE = (
    r"(?:\.{0,2}/)?(?:[A-Za-z0-9_~.+-]+/)*[A-Za-z0-9_~.+-]+\."
    rf"(?:{_SUFFIX_ALT})"
)

#: Not preceded by a word character, ``@``, or a URL/path separator, and not
#: followed by more name.  Without the left edge ``https://host/a/b.py`` yields
#: ``host/a/b.py``; without the right edge ``Node.js`` yields ``Node.js``.
_PATH_EDGE = r"(?<![\w@:/.-])" + _PATH_SHAPE + r"(?![A-Za-z0-9])"

#: Every citation shape the real answer used, in one alternation so a single
#: left-to-right pass can bind line numbers to the last path it saw.
#:
#: * ``path:line`` / ``path:line-line`` — the inline form
#: * ``第 N 行`` / ``第 N-M 行`` — with or without a space
#: * ``line N`` / ``L N`` / ``#L N`` — the English and shorthand forms
#: * a bare path on its own — it only moves the binding target
_CLAIM = re.compile(
    rf"(?P<inline>(?<![\w@:/.-])(?P<ipath>{_PATH_SHAPE})\s*:\s*(?P<iline>\d+)"
    rf"(?:\s*[-–~]\s*(?P<ieend>\d+))?)"
    rf"|(?<![\w@:/.-])(?P<path>{_PATH_SHAPE})(?![A-Za-z0-9])"
    rf"|(?P<zhang>第\s*(?P<zstart>\d+)\s*(?:[-–~]\s*(?P<zend>\d+)\s*)?行)"
    rf"|(?P<en>(?<![A-Za-z0-9_])#?\s*[Ll](?:ine)?\s*(?P<estart>\d+)"
    rf"(?:\s*[-–~]\s*(?P<eend>\d+))?)"
)

_PATH_TOKEN = re.compile(_PATH_EDGE)


@dataclass(frozen=True)
class ReadMark:
    """One contiguous run of lines the session has read.

    ``path`` is workspace-relative (``normalize`` is the only way in), ``start``
    and ``end`` are 1-based inclusive, and ``total`` is the file's line count —
    ``0`` when the read never learned it, which is a real answer, not a default
    to be filled in with a guess.
    """

    path: str
    start: int
    end: int
    total: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# ------------------------------------------------------------------ normalise


def normalize(path: str, workspace: str = "") -> str:
    """Absolute / relative / ``./``-prefixed paths → one workspace-relative key.

    Measured on T2: the same session received both
    ``/workspace/ai-coding/ai_coding/domain/plan_delivery.py`` and
    ``ai_coding/core/agent_loop.py`` for the very same files, while the answer
    cited ``ai_coding/domain/plan_delivery.py``.  Without this, ``covers`` would
    answer False for every citation and the check would reject the whole answer.
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return ""
    if os.path.isabs(text):
        if workspace:
            try:
                relative = os.path.relpath(text, workspace).replace("\\", "/")
            except ValueError:
                relative = ""
            # ``..`` means "outside the workspace": keep the basename rather
            # than a path the model can never resolve.
            if relative and not relative.startswith(".."):
                return relative
        return os.path.basename(text)
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def same_file(a: str, b: str) -> bool:
    """Are these two paths the same file?  Exact, or basename-onto-path.

    The ledger stores ``ai_coding/domain/plan_delivery.py``; the answer writes
    ``plan_delivery.py:81``; grep's output writes the relative path.  All three
    are one file.

    Only a *bare* basename is folded onto a path.  Two paths that both carry a
    directory are never folded together: ``a/foo.py`` and ``b/foo.py`` are
    different files, and treating them as one would answer a citation of the
    unread one from a read of the other -- and would stop tier 1 ("no tool ever
    named this") from ever firing.
    """
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if "/" not in left or "/" not in right:
        return os.path.basename(left) == os.path.basename(right)
    return False


def paths_in(text: str, workspace: str = "") -> tuple[str, ...]:
    """Every source path a piece of tool output names.

    grep renders ``path:line: text`` and glob renders bare paths, so both are
    the model's evidence that a file exists — which is what tier 1 of the
    citation check needs.  Deduplicated, order preserved.
    """
    out: list[str] = []
    for token in _PATH_TOKEN.findall(text or ""):
        norm = normalize(token, workspace)
        if norm and norm not in out:
            out.append(norm)
    return tuple(out)


# ---------------------------------------------------------------------- marks


def merge_mark(
    marks: Sequence[ReadMark], mark: ReadMark, *, max_marks: int = 40
) -> tuple[ReadMark, ...]:
    """Add one read to the ledger, coalescing the ranges of the same file.

    Overlapping *and adjacent* runs collapse: reading 1-60 and then 61-120 is
    one run of 1-120, not two, and the render must not ask the model to re-read
    line 61.

    Overflow drops the oldest marks rather than evicting the newest: the newest
    are what the model is working from right now.
    """
    group = [m for m in marks if m.path == mark.path]
    others = [m for m in marks if m.path != mark.path]
    spans = sorted((m.start, m.end) for m in group)
    spans.append((mark.start, mark.end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    total = max([mark.total, *(m.total for m in group)] or [0])
    out = (*others, *(ReadMark(mark.path, s, e, total) for s, e in merged))
    # ``out[-0:]`` is the whole list, so the ceiling has to be guarded: a
    # ``max_marks`` of 0 must not read as "no limit".
    if max_marks > 0 and len(out) > max_marks:
        out = out[-max_marks:]
    return out


def _clip_lines(body: str, max_chars: int) -> str:
    """Cut a rendered block at a line boundary, never through a line.

    Cutting by character count leaves the model holding half of a file line,
    which it will then try to interpret.
    """
    while body and len(body) > max_chars:
        cut = body.rfind("\n")
        if cut <= 0:
            return body[:max_chars] + "…"
        body = body[:cut]
    return body


def _gaps(spans: Sequence[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """The unread runs between 1 and ``total``, for sorted ``spans``."""
    out: list[tuple[int, int]] = []
    cursor = 1
    for start, end in spans:
        if start > cursor:
            out.append((cursor, min(start - 1, total)))
        cursor = max(cursor, end + 1)
    if cursor <= total:
        out.append((cursor, total))
    return [(a, b) for a, b in out if a <= b]


def missing_ranges(
    marks: Sequence[ReadMark], path: str
) -> list[tuple[int, int]]:
    """Where this file has not been read yet (only answerable when the size is known).

    ``total == 0`` means the read never learned the length; inventing a gap would
    send the model chasing lines that do not exist.
    """
    rows = [m for m in marks if same_file(m.path, path)]
    if not rows:
        return []
    total = max(m.total for m in rows)
    if not total:
        return []
    return _gaps(sorted((m.start, m.end) for m in rows), total)


def covers(marks: Sequence[ReadMark], path: str, start: int, end: int) -> bool:
    """Is the cited range *entirely inside* one of the read runs?  (R20b)

    Containment, not intersection.  The intersection rule let "read 61-110, cite
    100-120" pass while lines 111-120 had never been read — the exact shape of
    the T2 hallucination.  Containment closes that.

    Normal paging is unaffected: ``merge_mark`` coalesces adjacent runs, so
    reading 1-500 then 501-1000 leaves a single 1-1000 mark.

    A citation spanning a read gap (read 1-60 and 300-434, cite 50-350) is
    challenged on purpose.  That costs one iteration; the ambiguity guard (two or
    more files in one answer skips the check) and the one-retry-per-turn cap are
    the buffers.
    """
    for mark in marks:
        if same_file(mark.path, path) and mark.start <= start and end <= mark.end:
            return True
    return False


# --------------------------------------------------------------------- render


def render(
    marks: Sequence[ReadMark], *, max_chars: int = 2_400, max_files: int = 64
) -> str:
    """The ``<read-ledger>`` tail block, or ``""`` when there is nothing to say.

    ``max_files`` used to be 8.  After a compaction the model read the block,
    saw eight names, and restarted the task ("8 个文件已完整读取") even though
    the session had read twenty -- the "…还有 12 个文件" tail names none of
    them.  The ledger exists to stop that re-read, so the default has to list
    every file a real session touches (64 names cost ~2k chars, an order of
    magnitude less than one re-read).
    """
    if not marks:
        return ""
    order: list[str] = []
    groups: dict[str, list[ReadMark]] = {}
    for mark in reversed(marks):
        if mark.path not in groups:
            groups[mark.path] = []
            order.append(mark.path)
        groups[mark.path].append(mark)
    lines: list[str] = []
    for path in order[:max_files]:
        rows = groups[path]
        total = max(m.total for m in rows)
        spans = sorted((m.start, m.end) for m in rows)
        shown = ",".join(f"{s}-{e}" for s, e in spans)
        if total:
            gaps = _gaps(spans, total)
            missing = ",".join(f"{a}-{b}" for a, b in gaps)
            tail = "（完整）" if not gaps else f"（缺 {missing}）"
            lines.append(f"- {path} 共 {total} 行：{shown}{tail}")
        else:
            lines.append(f"- {path}：{shown}")
    body = _clip_lines("\n".join(lines), max_chars)
    # The count rides *outside* the budget: a truncation that dropped it would
    # leave the model believing eight files is all there is.
    if len(order) > max_files:
        body = f"{body}\n…还有 {len(order) - max_files} 个文件"
    return (
        "<read-ledger>\n"
        "本会话已读（行号；内容不在上下文里，需要时按缺口重读）：\n"
        f"{body}\n"
        "未读完的只读缺的段；已完整的不要重读。\n"
        "</read-ledger>"
    )


# ------------------------------------------------------------ claim extraction


def extract_claims(text: str) -> list[tuple[str, int, int]]:
    """Every ``(path, start, end)`` the answer cites, in the order written.

    One left-to-right pass: a path token moves the binding target, and every
    line-number shape after it binds to that target.  A bare ``第 N 行`` with no
    path anywhere is dropped — there is nothing to check it against.
    """
    claims: list[tuple[str, int, int]] = []
    current: str | None = None
    for match in _CLAIM.finditer(text or ""):
        if match.group("inline"):
            current = match.group("ipath")
            start = int(match.group("iline"))
            end = int(match.group("ieend") or start)
            if start > end:
                start, end = end, start
            claims.append((current, start, end))
            continue
        if match.group("path"):
            current = match.group("path")
            continue
        if not current:
            continue
        if match.group("zhang"):
            start = int(match.group("zstart"))
            end = int(match.group("zend") or start)
        else:
            start = int(match.group("estart"))
            end = int(match.group("eend") or start)
        # ``a.py:100-1`` is a typo, not a range.  Swapping keeps the check
        # meaningful: with containment, an inverted range would otherwise be
        # "inside" any mark whose start is <= its (larger) start -- a citation
        # that read better than the old intersection rule did (review finding).
        if start > end:
            start, end = end, start
        claims.append((current, start, end))
    return claims


def names_more_than_one_file(paths: set[str]) -> bool:
    """Do these path spellings denote more than one file?

    ``llm_helpers.py`` and ``ai_coding/ai/llm_helpers.py`` are one file -- the
    ledger stores the second and the answer abbreviates to the first.  Counting
    them as two would skip a perfectly checkable answer, which is exactly what
    happened on the first real re-run of T2.
    """
    names = list(paths)
    first = names.pop()
    return any(not same_file(first, other) for other in names)


def unsupported_claims(
    marks: Sequence[ReadMark],
    seen: Sequence[str],
    claims: Sequence[tuple[str, int, int]],
    *,
    size_of: Callable[[str], int] | None = None,
) -> list[tuple[str, int, int]]:
    """Which citations must be challenged, by the three-tier rule (R20b).

    * **Tier 1** — the path no tool result ever named.  Strongest signal: the
      model named a file this session never touched.
    * **Tier 2** — the path was seen (grep/glob/bash) but not read.  Those tools
      show lines the model may legitimately cite, and the ledger cannot say which
      ones, so the default is to let it pass.  The one thing that *is* provable
      is a citation past the end of the file, and ``size_of`` supplies that: it
      is called only here, only for the cited path, so the common case costs no
      IO at all.  ``None`` (the eval script, old sessions) degrades to pass.
    * **Tier 3** — the path was read, and the cited range is not contained in what
      was read.  The shape of T2's actual hallucination.

    An answer naming two or more files is skipped entirely: the binding is
    last-path-wins, so a paragraph that mentions two files would challenge a
    citation that was right.  Missing a rejection is cheaper than inventing one.
    """
    if not claims:
        return []
    if names_more_than_one_file({normalize(path) for path, _, _ in claims}):
        return []
    path = claims[0][0]
    rows = [mark for mark in marks if same_file(mark.path, path)]
    if rows:
        return [
            (cited, start, end)
            for cited, start, end in claims
            if not covers(rows, path, start, end)
        ]
    if any(same_file(seen_path, path) for seen_path in seen):
        if size_of is None:
            return []
        # Resolve the *seen* spelling, not the cited one: an answer may abbreviate
        # ``ai_coding/ai/llm_helpers.py`` to ``llm_helpers.py``, and the caller's
        # ``size_of`` resolves against the workspace root, where the bare name
        # does not exist -- that made tier 2 silently fail open (review finding).
        resolved = next(
            (seen_path for seen_path in seen if same_file(seen_path, path)), path
        )
        try:
            total = size_of(resolved)
        except (OSError, ValueError):
            # The contract says "do not raise", but a caller is a caller.
            total = 0
        if not total:
            # Unreadable: outside the workspace (normalize folds it to a
            # basename), deleted, a directory.  Widening, not a safety property.
            return []
        return [
            (cited, start, end)
            for cited, start, end in claims
            if end > total
        ]
    return list(claims)


#: The quote forms an answer uses around a pasted fragment: straight and curly
#: doubles, the two CJK bracket pairs, straight singles, and backticks.  Eight
#: characters minimum because a one-word "quote" carries no evidence.
_QUOTE_FORMS = re.compile(
    r'"([^"\n]{8,})"'
    r"|“([^”\n]{8,})”"
    r"|「([^」\n]{8,})」"
    r"|『([^』\n]{8,})』"
    r"|'([^'\n]{8,})'"
    r"|`([^`\n]{8,})`"
)


def _normalize_quote(text: str) -> str:
    """Fold a quoted fragment into what a substring check can compare.

    NFKC maps full-width punctuation and digits to their ASCII forms; the curly
    quotes have no compatibility decomposition, so they are replaced by hand.
    A ``read`` output prefixes every line with ``%6d  `` -- an answer may copy
    that prefix into its quote, and the file does not have it.
    """
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    text = re.sub(r"^\s*\d+\s{2}", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _quote_matches(quote: str, window: str) -> bool:
    """Is ``quote`` in ``window`` -- verbatim, or as a close paraphrase?

    The paraphrase allowance exists because the model legitimately reflows
    code into prose.  It is bounded on purpose: a ratio of 0.9 over at least
    ten tokens; a three-word fragment that happens to share half its letters
    is not evidence of anything.
    """
    if not quote:
        return True
    if quote in window:
        return True
    if len(quote.split()) < 10:
        return False
    matcher = SequenceMatcher(None, quote, window, autojunk=False)
    if matcher.ratio() >= 0.9:
        return True
    return any(
        SequenceMatcher(None, quote, line, autojunk=False).ratio() >= 0.9
        for line in window.splitlines()
    )


def _quotes_near_citations(answer: str) -> Iterator[tuple[str, int, int, str]]:
    """Each quoted fragment, bound to the citation on its own line.

    Last citation wins, the same rule ``extract_claims`` uses: a paragraph
    that moves between two files cannot say which quote belongs to which, so
    attaching to the nearest preceding citation is the only stable reading.
    """
    for line in answer.splitlines():
        claims = extract_claims(line)
        if not claims:
            continue
        path, start, end = claims[-1]
        for match in _QUOTE_FORMS.finditer(line):
            raw = next(group for group in match.groups() if group is not None)
            if extract_claims(raw):
                continue  # a quoted path, not a quoted fragment
            if "/" in raw and " " not in raw:
                continue  # a bare file path in backticks
            yield path, start, end, raw


def unsupported_quotes(
    answer: str,
    marks: Sequence[ReadMark],
    reader: Callable[[str], str | None],
) -> list[tuple[str, int, int, str]]:
    """Quotes the cited range does not contain (R21 §6).

    The ledger records *which lines* were read, never what they said.  So a
    citation can be fully contained in a read range and still quote code that
    those lines no longer hold -- the file was rewritten or replaced between
    the read and the answer.  This reads the region back and looks for the
    quoted fragment.

    Fail-open everywhere it cannot know: no quote attached to a citation, two
    files in the answer, an unreadable file (``reader`` returns ``None``), a
    range past the end of what is on disk.  A challenge is only ever raised on
    a comparison that actually happened.
    """
    claims = extract_claims(answer)
    if not claims:
        return []
    if names_more_than_one_file({normalize(path) for path, _, _ in claims}):
        return []
    checked: list[tuple[str, int, int, str]] = []
    for path, start, end, raw in _quotes_near_citations(answer):
        quote = _normalize_quote(raw)
        if not quote:
            continue
        rows = [mark for mark in marks if same_file(mark.path, path)]
        # The ledger stored the spelling the read tool used; the answer may
        # abbreviate it, and the reader resolves from the workspace root.
        resolved = rows[0].path if rows else path
        text = reader(resolved)
        if text is None:
            continue
        lines = text.splitlines()
        if start < 1 or end > len(lines):
            continue
        window = _normalize_quote("\n".join(lines[start - 1 : end]))
        if _quote_matches(quote, window):
            continue
        row = (path, start, end, quote)
        if row not in checked:
            checked.append(row)
    return checked


__all__ = [
    "ReadMark",
    "covers",
    "extract_claims",
    "merge_mark",
    "missing_ranges",
    "names_more_than_one_file",
    "normalize",
    "paths_in",
    "render",
    "same_file",
    "unsupported_claims",
    "unsupported_quotes",
]
