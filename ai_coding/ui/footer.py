"""底部状态栏。"""

from __future__ import annotations

import time
from typing import Any


def render_footer(loop: Any, turn_start: float) -> list[tuple[str, str]]:
    parts = []
    outcome = getattr(loop, "last_outcome", None)
    usage = getattr(outcome, "usage", None) if outcome else None
    total_tokens = (usage or {}).get("total_tokens", 0)
    max_tokens = getattr(getattr(loop, "settings", None), "max_context_tokens", 0)
    if max_tokens > 0:
        pct = int(total_tokens / max_tokens * 100)
        parts.append(f"{pct}% context")
    elapsed = time.monotonic() - turn_start
    parts.append(f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
    text = " · ".join(parts)
    return [("class:footer.text", text)]
