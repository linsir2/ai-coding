"""Image attachments on disk (r17/T5).

A clipboard paste arrives as a data URL; it is written once under
``<workspace>/.ai-coding/attachments/`` and the message then references the
file.  Content-addressed names mean the same screenshot pasted twice is one
file, and a session file never carries the bytes (OpenCode stores base64 in the
message; we deliberately do not).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path

from ai_coding.domain.message import ImageRef

#: What the model can be shown.  A whitelist, not a sniff: the client already
#: declares the mime, and anything else is not an image input.
_ALLOWED_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_DATA_URL = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$")
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


def save_data_url(
    workspace: str | Path,
    name: str,
    data_url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ImageRef | None:
    """Write a pasted image under the workspace; ``None`` when unacceptable.

    Returns the reference (workspace-relative path, mime, display name), or
    None for a non-image, malformed, or oversized payload — the caller reports
    that rather than sending a request the provider would reject.
    """
    match = _DATA_URL.match((data_url or "").strip())
    if match is None:
        return None
    mime = match.group("mime").lower()
    if mime not in _ALLOWED_MIME:
        return None
    try:
        raw = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > max_bytes:
        return None
    relative = f".ai-coding/attachments/{hashlib.sha1(raw).hexdigest()}.{_ALLOWED_MIME[mime]}"
    target = Path(workspace).expanduser() / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)
    except OSError:
        return None
    return ImageRef(path=relative, mime=mime, name=name or target.stem)


__all__ = ["DEFAULT_MAX_BYTES", "save_data_url"]
