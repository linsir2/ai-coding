"""Scrolling conversation transcript (behavior lands in Task 2)."""

from __future__ import annotations

from textual.containers import VerticalScroll


class Transcript(VerticalScroll):
    """Append-only conversation area."""
