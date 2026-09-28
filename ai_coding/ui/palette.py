"""命令面板：键盘导航。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: Total width of the panel, border included. Content rows carry a leading
#: ``│`` and a trailing ``│``, so they are padded to ``PANEL_WIDTH - 1``.
PANEL_WIDTH = 72
PALETTE_INNER_WIDTH = PANEL_WIDTH - 2


@dataclass(frozen=True)
class PaletteEntry:
    name: str
    description: str
    icon: str = ""


def _cell_width(ch: str) -> int:
    """Terminal columns one character occupies: CJK and emoji take two."""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    # Emoji and dingbats report as neutral but render double-width.
    if unicodedata.category(ch) == "So" and ord(ch) >= 0x1F000:
        return 2
    return 1


def _display_width(text: str) -> int:
    return sum(_cell_width(ch) for ch in text)


def _fit(text: str, width: int) -> str:
    """Pad or ellipsize ``text`` to exactly ``width`` display columns.

    Truncation is what keeps the frame straight: a long description must lose
    characters, never a column, or the right-hand border drifts per row.
    """
    used = _display_width(text)
    if used <= width:
        return text + " " * (width - used)
    out = ""
    spent = 0
    for ch in text:
        step = _cell_width(ch)
        if spent + step > width:
            break
        out += ch
        spent += step
    return out + " " * (width - spent)


class CommandPalette:
    def __init__(self, entries: list[PaletteEntry]) -> None:
        self._entries = entries
        self._is_open = False
        self._selected = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def selected_index(self) -> int:
        return self._selected

    def open(self) -> None:
        self._is_open = True
        self._selected = 0

    def close(self) -> None:
        self._is_open = False

    def handle_key(self, key: str) -> None:
        if key == "down":
            self._selected = (self._selected + 1) % len(self._entries)
        elif key == "up":
            self._selected = (self._selected - 1) % len(self._entries)

    def select(self) -> PaletteEntry:
        entry = self._entries[self._selected]
        self.close()
        return entry

    def render(self) -> list[tuple[str, str]]:
        """The panel: a border, one row per command, then the key hints.

        Every row is the same display width — the border's ``│`` counts as two
        columns, so the inner area is two narrower than the frame.
        """
        inner = PALETTE_INNER_WIDTH
        lines: list[tuple[str, str]] = [
            ("class:palette.border", f"╭{'─' * inner}╮\n"),
        ]
        for i, entry in enumerate(self._entries):
            selected = i == self._selected
            marker = "❯" if selected else " "
            icon = f"{entry.icon} " if entry.icon else ""
            row = f"│ {marker} /{entry.name:<12} {icon}{entry.description}"
            style = "class:palette.selected" if selected else "class:palette.item"
            lines.append((style, _fit(row, inner) + " │\n"))
        hint = "│ ↑↓ navigate   Enter select   Esc close"
        lines.append(("class:palette.hint", _fit(hint, inner) + " │\n"))
        lines.append(("class:palette.border", f"╰{'─' * inner}╯\n"))
        return lines


__all__ = ["CommandPalette", "PaletteEntry", "PALETTE_INNER_WIDTH"]
