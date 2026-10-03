"""Tokenising for recall (R3a).

The old code split with ``[^\\w\\u4e00-\\u9fff]+``, but Python's ``\\w`` already matches
CJK, so a Chinese sentence became **one** keyword and substring search could never
match it (case 1 in docs/r3-analysis.md).  English kept stopwords, so a single
"the"/"file" was enough to inject an unrelated note (case 10).

What this module does instead:

  - CJK runs become character bigrams (``项目用uv`` -> ``项目``, ``目用`` ...), which lets a
    query and a note actually share terms; a one-character run stays whole;
  - ASCII words drop a small English stopword list;
  - identifiers such as ``config/pay.yaml``, ``todo_write()`` and ``RS256`` survive
    (tokens are casefolded, so they survive in lowercase);
  - :func:`normalize` strips common lead-ins (``记住：``, ``- ``) before building a
    dedupe key — without this an identical fact looked like a new one (prototype G2).
"""

from __future__ import annotations

import re

__all__ = [
    "STOPWORDS",
    "clean_query_for_recall",
    "normalize",
    "similarity",
    "strip_lead",
    "tokenize",
]

STOPWORDS: frozenset[str] = frozenset(
    """
    a an the of to in on at for and or with is are was were be been being am
    do does did how what when where which who whom this that these those it its
    as by from into over under again further then once here there all any both
    each few more most other some such no nor not only own same so than too very
    can will just should now please look see use uses used using
    """.split()
)

# CJK particles / pronouns / question words: they carry no recall signal, and as
# bigrams they are pure noise ("怎么" from "依赖怎么装").  A bigram made *only* of
# stop characters is dropped too.
CJK_STOPWORDS: frozenset[str] = frozenset(
    """
    的 了 吗 呢 吧 啊 呀 哦 嗯 我 你 他 她 它 我们 你们 他们 这个 那个 这些 那些
    这里 那里 什么 怎么 怎样 如何 可以 能否 是否 请 帮我 一下 有没有 以及 或者
    还有 但是 因为 所以 如果 就是 还是 已经 正在 现在 然后 而且 并且 那么 这样
    那样 知道 告诉 觉得 应该 需要 想要
    """.split()
)
_CJK_STOP_CHARS = frozenset(
    "的了吗呢吧啊呀哦嗯我你他她它这那些什么怎如何可以请帮一下有没是否在于和或还就而并那么样嘛哪"
)

_WORD = re.compile(r"[a-z0-9_][a-z0-9_./\\-]*")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_LEAD = re.compile(
    r"(?:请?记住|请?注意|备忘|提示|note|todo|remember|[-*·•])\s*[:：]?\s*",
    re.IGNORECASE,
)

# R11a: envelopes a recall query may carry (pasted tool results, volatile tail
# blocks that leaked into the next question).  The block pattern pairs opening
# and closing tags by name, so `<tool_output>…</tool_output>` goes as one piece;
# the bare-tag pattern only fires when the first character is a letter or `/`,
# so `a < b` survives.
_CLEAN_BLOCK = re.compile(
    r"<(tool_output|memory-constraints|working-memory|task-state|hook-context"
    r"|persisted-output|known-memory)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_CLEAN_TAG = re.compile(r"</?[A-Za-z][^>]{0,63}>")
_CLEAN_BLANK_RUN = re.compile(r"\n{3,}")


def clean_query_for_recall(text: str, *, max_chars: int = 2048) -> str:
    """Strip tool/memory envelopes from a recall query (R11a).

    The ranker only sees what survives here, so a pasted `<tool_output>` block
    or a leaked `<memory-constraints>` tail can no longer become recall
    vocabulary.  Plain questions pass through untouched (up to truncation).
    """
    body = _CLEAN_BLOCK.sub("", text or "")
    body = _CLEAN_TAG.sub("", body)
    body = _CLEAN_BLANK_RUN.sub("\n\n", body).strip()
    if max_chars > 0 and len(body) > max_chars:
        body = body[:max_chars]
    return body


def strip_lead(text: str) -> str:
    """Remove leading "记住："/"- "/"note:" markers (repeatable), keep the content."""
    out = (text or "").strip()
    while True:
        stripped = _LEAD.sub("", out, count=1).strip()
        if stripped == out:
            return out
        out = stripped


def tokenize(text: str) -> list[str]:
    """Split text into recall terms (CJK bigrams + ASCII words without stopwords)."""
    lowered = (text or "").casefold()
    tokens: list[str] = [
        word for word in _WORD.findall(lowered) if len(word) > 1 and word not in STOPWORDS
    ]
    for segment in _CJK.findall(lowered):
        tokens.extend(_cjk_tokens(segment))
    return tokens


def _cjk_tokens(segment: str) -> list[str]:
    """Character bigrams of one CJK run, minus particles and question words."""
    if len(segment) == 1:
        return [] if segment in _CJK_STOP_CHARS or segment in CJK_STOPWORDS else [segment]
    out: list[str] = []
    for index in range(len(segment) - 1):
        bigram = segment[index : index + 2]
        if bigram in CJK_STOPWORDS:
            continue
        if all(char in _CJK_STOP_CHARS for char in bigram):
            continue
        out.append(bigram)
    return out


def normalize(text: str) -> str:
    """Fold a note into a comparable key: case, spacing, punctuation, lead-ins."""
    body = strip_lead(text).casefold()
    return re.sub(r"[\s\W_]+", "", body, flags=re.UNICODE)


def similarity(a: str, b: str) -> float:
    """Token Jaccard similarity in ``[0, 1]`` (used for dedupe and merging)."""
    left, right = set(tokenize(a)), set(tokenize(b))
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
