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
            lines.append(("class:header.label", f"  ✓ memory loaded ({stats.get('total', 0)} notes)\n"))
        except Exception:
            lines.append(("class:header.label", "  ✗ memory unavailable\n"))
    settings = getattr(loop, "settings", None)
    if settings:
        model = getattr(settings, "default_model", "?")
        lines.append(("class:header.model", f"  model: {model}\n"))
    workspace = getattr(loop, "workspace", "?")
    lines.append(("class:header.cwd", f"  cwd:    {workspace}\n"))
    return lines
