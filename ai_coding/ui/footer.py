"""Footer: model info + context + timer + exit hint."""

from __future__ import annotations

import time
from typing import Any


def render_footer(loop: Any, turn_start: float) -> list[tuple[str, str]]:
    parts = []
    model = getattr(getattr(loop, "engine", None), "model_config", None)
    name = getattr(model, "name", "") or getattr(loop, "model_name", "") or "?"
    parts.append(f"model: {name}")
    outcome = getattr(loop, "last_outcome", None)
    usage = getattr(outcome, "usage", None) if outcome else None
    total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
    max_tokens = getattr(getattr(loop, "settings", None), "max_context_tokens", 0)
    if max_tokens > 0:
        pct = int(total_tokens / max_tokens * 100)
        parts.append(f"{pct}% context")
    elapsed = time.monotonic() - turn_start
    parts.append(f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
    parts.append("ctrl+c quit · / commands")
    text = " · ".join(parts)
    return [("class:footer.text", text)]
