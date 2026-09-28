"""启动横幅：ASCII art + 状态指示器。"""

from __future__ import annotations

from typing import Any

_ASCII_LOGO = r"""
███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗
████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝
██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗
██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║
██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║
╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝
"""


def render_header(loop: Any) -> list[tuple[str, str]]:
    """Render the startup banner as FormattedText (list of (style, text) tuples)."""
    lines: list[tuple[str, str]] = []
    for line in _ASCII_LOGO.strip("\n").split("\n"):
        lines.append(("class:ascii", f"{line}\n"))
    lines.append(("class:header.label", "  ✓ workspace indexed\n"))
    tools = getattr(loop, "tools", None)
    if tools and hasattr(tools, "list_names"):
        names = tools.list_names()
        lines.append(("class:header.label", f"  ✓ {len(names)} tools connected\n"))
    memory_status = getattr(loop, "memory_status", None)
    if callable(memory_status):
        try:
            stats = memory_status()
            total = stats.get("total", 0)
            lines.append(("class:header.label", f"  ✓ memory loaded ({total} notes)\n"))
        except Exception:
            lines.append(("class:header.label", "  ✗ memory unavailable\n"))
    # ``loop.settings`` is the ``AIConfig`` slice, which has no model name; the
    # engine is what actually knows (and what ``/model`` swaps).
    model = getattr(getattr(loop, "engine", None), "model_config", None)
    name = getattr(model, "name", "") or getattr(loop, "model_name", "") or "?"
    lines.append(("class:header.model", f"  model:  {name}\n"))
    workspace = getattr(loop, "workspace", "?")
    lines.append(("class:header.cwd", f"  cwd:    {workspace}\n"))
    return lines
