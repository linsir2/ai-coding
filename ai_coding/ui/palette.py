"""命令面板：键盘导航。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaletteEntry:
    name: str
    description: str
    icon: str = ""


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
        lines: list[tuple[str, str]] = []
        for i, entry in enumerate(self._entries):
            if i == self._selected:
                prefix = ">"
                style = "class:palette.selected.text"
            else:
                prefix = " "
                style = "class:palette.item"
            icon = f"{entry.icon} " if entry.icon else ""
            lines.append((style, f"  {prefix} /{entry.name:<15} {icon}{entry.description}\n"))
        lines.append(("class:palette.hint", "  ↑↓ navigate  Enter select  Esc close\n"))
        return lines
