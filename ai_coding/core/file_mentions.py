"""``@path`` references resolved against the workspace (r17/T4).

A sibling of the skills mention (r16/E-3), sharing its tokenizer (``iter_mentions``)
and its lifecycle: resolved from the last user message, rendered into a volatile
tail block, gone with the next user message.  Files are the *content* half —
skills are instructions, files are what the instructions operate on.

Reference behaviour: only paths that *exist* inside the workspace resolve, so
``cc @alice`` is prose unless a file or directory named ``alice`` is actually
there — and then reading it is what the user asked for.  A token that escapes
the workspace is refused rather than read.
"""

from __future__ import annotations

import re
from pathlib import Path

from ai_coding.skills.registry import iter_mentions

#: What a pasted path looks like in prose.  Two shapes, because both are real:
#: a multi-segment path (``/abs/x.py``, ``pkg/a.py``) and a bare filename the
#: user is in the middle of talking about (``用 main.py 这个文件``).
#:
#: The filename branch is constrained to extensions that name a source or
#: document, which is what separates ``main.py`` from the prose the same shape
#: admits: ``3/4`` and ``1.2/3.4`` have no such extension, ``e.g.`` is not one,
#: and ``v1.2.3`` does not end in a file type.  Existence decides the rest.
_BARE_PATH = re.compile(
    r"(?<![\w@:/.-])"
    r"(?P<path>(?:\.{0,2}/)?(?:[A-Za-z0-9_~.+-]+/)*[A-Za-z0-9_~.+-]+\."
    r"(?:py|md|txt|json|ya?ml|toml|ini|cfg|ts|tsx|js|jsx|mjs|cjs|css|scss|html|htm"
    r"|sh|bash|zsh|rs|go|java|kt|swift|c|h|cc|cpp|hpp|hxx|sql|csv|tsv|log|lock"
    r"|env|ini|conf|xml|svg|png|jpg|jpeg|gif|webp|ipynb)"
    r"(?![A-Za-z0-9]))"
)

#: What a URL looks like immediately before the part that would match.  Without
#: this, ``https://example.com/a/b.py`` yields ``com/a/b.py`` -- a path-shaped
#: fragment of a web address, which is how a link turns into a file read.
_URL_PREFIX = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//", re.IGNORECASE)

#: How much text before a candidate can decide whether it is part of a URL.
_URL_LOOKBEHIND = 12


def resolve_file_mentions(
    text: str,
    workspace: str | Path,
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Workspace-relative paths named by ``@`` tokens, in order, deduplicated.

    ``exclude`` is the token spellings already claimed by another resolver
    (the skills mention), so a name that is both keeps its first meaning.
    """
    root = Path(workspace).expanduser().resolve()
    found: list[str] = []
    for token in iter_mentions(text):
        if token in exclude or token in found:
            continue
        candidate = root / token
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.exists():
            found.append(token)
    return found


def resolve_bare_paths(
    text: str,
    workspace: str | Path,
    *,
    exclude: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Workspace-relative paths a message *names*, with no ``@`` to introduce them.

    The other half of :func:`resolve_file_mentions`, for the same reason: a
    pasted path is a request to look at that file, and requiring a keystroke the
    user did not know about left the model holding a string it could not use
    (opencode materializes pasted files before the turn is queued --
    ``docs/orchestration-research/03-opencode编排数据流.md:37,42``).

    Same containment as the ``@`` half, and for the same reason: the block that
    consumes this reads with ``read_text`` and never goes through ``Sandbox``, so
    an outside path injected here would step around the approval the ``read``
    tool now asks for.  Outside paths stay the read tool's business.

    Existence is the tie-breaker.  ``3/4`` and ``1.2/3.4`` match the shape and
    name nothing on disk, so they are prose again.
    """
    root = Path(workspace).expanduser().resolve()
    found: list[str] = []
    for match in _BARE_PATH.finditer(text or ""):
        token = match.group("path")
        if token in exclude or token in found:
            continue
        before = text[max(0, match.start() - _URL_LOOKBEHIND) : match.start()]
        if _URL_PREFIX.search(before):
            continue
        candidate = root / token
        try:
            resolved = candidate.resolve()
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not resolved.exists():
            continue
        name = relative.as_posix()
        if name not in found:
            found.append(name)
    return found
