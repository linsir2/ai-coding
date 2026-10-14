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
from dataclasses import dataclass
from pathlib import Path

from ai_coding.domain import message as _message
from ai_coding.domain.message import ImageRef
from ai_coding.security.sandbox import Sandbox, SandboxViolation


def allowed_mime() -> dict[str, str]:
    """``{mime: file extension}`` for the images the model can be shown.

    Derived from ``IMAGE_MIME_BY_SUFFIX`` rather than written out (r18/F16): two
    hand-kept copies of the same set is how a suffix became acceptable to ``@``
    while the paste path still rejected it -- and the provider then refused the
    whole turn.  Where a mime has several suffixes the shortest names the file,
    which is also what the table was written to produce.

    Read per call rather than frozen at import (r19/F2): a frozen copy is the
    same drift wearing a different hat -- the paste path would have followed
    the table while ``load_image`` refused what it had just accepted.  At ~2.5us
    and a handful of calls per turn, deriving it each time is free.
    """
    table = _message.IMAGE_MIME_BY_SUFFIX
    return {
        mime: min(
            (suffix for suffix, m in table.items() if m == mime),
            key=len,  # shortest wins; ``min()`` alone would order lexically and
                      # pick ".jpeg" over ".jpg"
        ).lstrip(".")
        for mime in set(table.values())
    }
_DATA_URL = re.compile(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$")
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class SavedImage:
    """The outcome of saving one pasted image, refusal included.

    ``save_data_url`` has five separate ways to say no.  Returning a bare
    ``None`` collapsed all of them into "nothing happened", which is why the
    session handler could only drop a rejected paste in silence while the
    transcript went on showing it as sent (r20/F5).
    """

    ref: ImageRef | None
    reason: str = ""


def save_data_url(
    workspace: str | Path,
    name: str,
    data_url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> SavedImage:
    """Write a pasted image under the workspace, saying why if it is refused.

    The reference carries the workspace-relative path, mime and display name.
    A payload that is not a data URL, is not an image type, is not valid
    base64, is empty or over the size budget, or cannot be written comes back
    with no reference and the rule it broke -- so the caller can tell the user
    what to change instead of silently dropping what they pasted.
    """
    match = _DATA_URL.match((data_url or "").strip())
    if match is None:
        return SavedImage(None, "不是图片数据（data URL 格式无法解析）")
    mime = match.group("mime").lower()
    extensions = allowed_mime()
    if mime not in extensions:
        return SavedImage(None, f"不支持的图片格式：{mime}")
    try:
        raw = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        return SavedImage(None, "图片数据损坏（base64 解码失败）")
    if not raw:
        return SavedImage(None, "图片数据为空")
    if len(raw) > max_bytes:
        return SavedImage(None, f"图片过大（{len(raw)} 字节 > {max_bytes} 字节上限）")
    relative = f".ai-coding/attachments/{hashlib.sha1(raw).hexdigest()}.{extensions[mime]}"
    target = Path(workspace).expanduser() / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)
    except OSError:
        return SavedImage(None, "图片写入失败（工作区不可写）")
    return SavedImage(
        ref=ImageRef(path=relative, mime=mime, name=name or target.stem, bytes=len(raw))
    )


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
    if ref.mime not in allowed_mime():
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


__all__ = ["DEFAULT_MAX_BYTES", "SavedImage", "allowed_mime", "load_image", "save_data_url"]
