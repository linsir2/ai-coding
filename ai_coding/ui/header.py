"""Header: welcome box + status indicators."""

from __future__ import annotations

from typing import Any


def render_header(loop: Any) -> list[tuple[str, str]]:
    """Render the welcome box and status indicators."""
    lines: list[tuple[str, str]] = []
    lines.append(("class:header.border", "╭──────────────────────────────────────╮\n"))
    lines.append(("class:header.text", "│ ✻ Welcome to ai-coding!               │\n"))
    lines.append(("class:header.text", "│                                      │\n"))
    lines.append(("class:header.text", "│ /help for help                        │\n"))
    workspace = getattr(loop, "workspace", "?")
    lines.append(("class:header.cwd", f"│ cwd: {workspace:<34}│\n"))
    lines.append(("class:header.border", "╰──────────────────────────────────────╯\n"))
    tools = getattr(loop, "tools", None)
    if tools and hasattr(tools, "list_names"):
        names = tools.list_names()
        lines.append(("class:header.label", f"✓ {len(names)} tools connected · /tools\n"))
    memory_status = getattr(loop, "memory_status", None)
    if callable(memory_status):
        try:
            stats = memory_status()
            total = stats.get("total", 0)
            lines.append(("class:header.label", f"✓ memory loaded ({total} notes) · /memory\n"))
        except Exception:
            lines.append(("class:header.label", "✗ memory unavailable\n"))
    model = getattr(getattr(loop, "engine", None), "model_config", None)
    name = getattr(model, "name", "") or getattr(loop, "model_name", "") or "?"
    lines.append(("class:header.model", f"model: {name}\n"))
    return lines
