"""Scrollable output Buffer."""

from __future__ import annotations

from prompt_toolkit.buffer import Buffer


class BodyBuffer:
    def __init__(self) -> None:
        self._buffer = Buffer()

    def write(self, text: str, style: str = "") -> None:
        self._buffer.insert_text(text)

    def clear(self) -> None:
        self._buffer.reset()

    def get_buffer(self) -> Buffer:
        return self._buffer

    def get_formatted_text(self) -> str:
        return self._buffer.text
