"""REPL direct-command (``/command``) parsing.

A line beginning with ``/`` is checked against a known alias table.  Known
commands return a :class:`SlashCommand`; anything else (plain text, unknown
slash-prefixed input) returns ``None`` so the caller sends it to the model.
Pure and offline — no terminal state involved.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    """A parsed direct command: its canonical name and the raw line."""

    name: str
    raw: str


_ALIASES: dict[str, str] = {
    "/quit": "quit",
    "/exit": "quit",
    "/q": "quit",
    "/clear": "clear",
    "/help": "help",
    "/h": "help",
    "/status": "status",
    "/compact": "compact",
    "/undo": "undo",
    "/redo": "redo",
    "/memory": "memory",
    "/trace": "trace",
    "/budget": "budget",
    "/stats": "stats",
    "/export": "export",
    "/model": "model",
    "/resume": "resume",
    "/permissions": "permissions",
    "/todo": "todo",
    "/tasks": "tasks",
    "/skills": "skills",
    "/plan": "plan",
    "/review": "review",
    "/config": "config",
}


def command_names() -> tuple[str, ...]:
    """Every canonical command the dispatcher recognises (r19/F5).

    Published over IPC so the TUI can ask "is this line a command?" instead of
    guessing from the leading slash: ``/path/to/file.py`` starts with one but
    the backend hands it to the model as ordinary text, and a guess that says
    "command" makes the client drop the picture the model is about to be sent.
    Derived from ``_ALIASES``, which is the only place the vocabulary is written.
    """
    return tuple(sorted(set(_ALIASES.values())))


def parse_slash(text: str) -> SlashCommand | None:
    """Return a ``SlashCommand`` for a known ``/``-command, else ``None``."""
    stripped = text.strip()
    if not stripped or not stripped.startswith("/"):
        return None
    token = stripped.split(None, 1)[0].lower()
    if token == "/":
        return None
    name = _ALIASES.get(token)
    if name is None:
        return None
    return SlashCommand(name=name, raw=stripped)


def _levenshtein(a: str, b: str) -> int:
    """Edit distance between two strings (no external dependency)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


#: Commands that existed and were absorbed by another one.  A tombstone beats
#: the edit-distance rule: "/plan-run" is four edits from "plan", so the
#: near-miss logic below would never fire, and the user who typed it got a
#: model turn instead of an answer.  Keys are the full token, values the
#: command that replaced it.  Kept (not deleted) because help text, docs and
#: habits outlive the rename.
_DEPRECATED: dict[str, str] = {"/plan-run": "/plan"}


def suggest_command(token: str) -> str | None:
    """Canonical name to suggest for a near-miss token, or ``None``.

    ``parse_slash`` returns ``None`` for anything unknown — deliberately, so a
    path like ``/path/to/file`` still reaches the model.  A typo such as
    ``/resum`` is not a path, though, and silently becoming a model turn is
    worse than a nudge.  Only single-word tokens within edit distance 2 of a
    known command are suggested; anything else stays text.

    Absorbed commands are answered from the tombstone table first, because
    their distance to the replacement is exactly what the near-miss rule
    cannot see.
    """
    name = token.lstrip("/").lower()
    if not name or "/" in name or " " in name:
        return None
    replacement = _DEPRECATED.get(token.strip().lower())
    if replacement is not None:
        return replacement.lstrip("/")
    best: str | None = None
    best_dist = 3
    for cmd in set(_ALIASES.values()):
        d = _levenshtein(name, cmd)
        if d < best_dist:
            best_dist = d
            best = cmd
    return best if best_dist <= 2 else None


__all__ = ["SlashCommand", "parse_slash", "suggest_command"]
