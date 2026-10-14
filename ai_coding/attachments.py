"""Image attachments on disk (r17/T5, r18/F2).

A clipboard paste arrives as a data URL; it is written once under
``<workspace>/.ai-coding/attachments/`` and the message then references the
file.  Content-addressed names mean the same screenshot pasted twice is one
file, and a session file never carries the bytes (OpenCode stores base64 in the
message; we deliberately do not).

The read direction is the security-relevant one.  ``ImageRef.path`` arrives from
a session file -- hand-edited, synced between machines, or simply wrong -- so
``load_image`` is the *only* place image bytes leave the disk, and it refuses
anything that is not a whitelisted image inside the workspace and inside the
budget.  It reuses ``security.sandbox.Sandbox`` rather than re-deriving
containment, so file tools and image loads agree on what "inside" means.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path

from ai_coding.domain.message import IMAGE_MIME_BY_SUFFIX, ImageRef
from ai_coding.security.sandbox import Sandbox, SandboxViolation

#: What the model can be shown.  A whitelist, not a sniff: the client already
#: declares the mime, and anything else is not an image input.
_ALLOWED_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
#: The same set, read from the table the loop and request assembly already use --
#: one truth source for "is this an image", two projections of it (extensions
#: for naming files, mimes for checking them).
IMAGE_MIMES = frozenset(IMAGE_MIME_BY_SUFFIX.values())
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


def load_image(
    workspace: str | Path,
    ref: ImageRef,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str | None:
    """Read one message image as a data URL, or ``None`` when it may not be read.

    The single gate every outbound image byte passes (r18/F2).  ``ref.path`` is
    data from a session file, so three things have to hold: the mime is one the
    model was promised, the path stays inside the workspace once symlinks are
    resolved, and the file fits the budget.  ``None`` means "degrade to a text
    note" -- the caller already has that path, and a refused image must not fail
    the turn.
    """
    if ref.mime not in IMAGE_MIMES:
        return None
    try:
        target = Sandbox(workspace).resolve(ref.path)
        # stat before read: an oversized file costs a stat, not its whole size.
        if target.stat().st_size > max_bytes:
            return None
        raw = target.read_bytes()
    except (OSError, ValueError, SandboxViolation):
        return None
    return f"data:{ref.mime};base64,{base64.b64encode(raw).decode('ascii')}"


__all__ = ["DEFAULT_MAX_BYTES", "IMAGE_MIMES", "load_image", "save_data_url"]
