"""Display helpers for the TUI (M1): labels derived from configuration.

The prompt metadata row shows ``provider · model``; the model config has no
provider field, only a ``base_url`` — deriving the label from the host keeps
the row honest (it reflects the real endpoint) without inventing config.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

__all__ = ["provider_from_base_url"]

#: Host labels that are infrastructure, not the provider name.
_SKIP_LABELS = {"api", "www"}
#: A plausible hostname: letters, digits, dots, hyphens (no spaces/paths).
_HOSTNAME = re.compile(r"[A-Za-z0-9.-]+")


def provider_from_base_url(base_url: str) -> str:
    """``https://api.deepseek.com/v1`` -> ``"deepseek"``; junk -> ``""``."""
    raw = base_url.strip()
    if not raw:
        return ""
    # urlparse only fills ``hostname`` when the URL has a netloc; a bare
    # "api.example.com/v1" is a path otherwise.
    parsed = urlparse(raw if "//" in raw else "//" + raw)
    host = (parsed.hostname or "").strip()
    if not host or not _HOSTNAME.fullmatch(host):
        return ""
    labels = [part for part in host.split(".") if part]
    while labels and labels[0].lower() in _SKIP_LABELS:
        labels.pop(0)
    return labels[0].lower() if labels else ""
