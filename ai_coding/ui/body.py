"""可滚动输出 Buffer。"""

from __future__ import annotations


class BodyBuffer:
    def __init__(self) -> None:
        self._lines: list[str] = []

    def write(self, text: str, style: str = "") -> None:
        self._lines.append(text)

    def clear(self) -> None:
        self._lines.clear()

    def get_formatted_text(self) -> str:
        return "".join(self._lines)
