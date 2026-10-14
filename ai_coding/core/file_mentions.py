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

from pathlib import Path

from ai_coding.skills.registry import iter_mentions


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
