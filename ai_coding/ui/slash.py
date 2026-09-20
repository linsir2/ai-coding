"""REPL direct-command (``/command``) parsing (M5-2).

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
}


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


__all__ = ["SlashCommand", "parse_slash"]
