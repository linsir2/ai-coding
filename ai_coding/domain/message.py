"""Core message and tool-call contracts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

Role = Literal["user", "assistant", "tool", "system"]

#: ``ToolResult.error`` when the user cancelled this one call (R8c, D-7).  The
#: call still gets a result - the declared tool_calls must pair up - and the turn
#: carries on, so the model can change approach.
CANCELLED_BY_USER = "cancelled_by_user"

#: The first line of a compaction checkpoint note.  The note is stored as a
#: user-role message (so the model reads it as history and replies pair up),
#: but it is model-generated content, not something the user typed.  It lives
#: here, the lowest shared layer, because three readers must agree on it:
#: the compressor that recognises its own note (``split_checkpoint``), the
#: session preview that must skip it, and the frontend replay's TS twin.
CHECKPOINT_MARK = "[以下为早期对话摘要]"


@dataclass(frozen=True)
class ToolCallRef:
    """The model's raw tool-call reference: ``arguments`` is a JSON *string*."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCall:
    """A resolved tool call with its arguments parsed into a mapping."""

    id: str
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolArtifact:
    """A side effect produced by a tool: a created/changed file, directory or url.

    Structured counterpart to the free-text ``output``; lets the runtime, UI and
    session records describe what a tool actually did without parsing prose.
    """

    path: str
    kind: str = "file"  # file | dir | url | ref
    bytes: int | None = None
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "kind": self.kind}
        if self.bytes is not None:
            data["bytes"] = self.bytes
        if self.description:
            data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolArtifact:
        return cls(
            path=str(data.get("path", "")),
            kind=str(data.get("kind", "file")),
            bytes=data.get("bytes"),
            description=str(data.get("description", "")),
        )


@dataclass(frozen=True)
class ToolResult:
    """The outcome produced by executing a tool, ready to be fed back.

    ``output`` is what the model reads; ``data`` is the machine-readable result
    (R1a/D9) so callers never have to parse prose.  ``data is None`` keeps the
    legacy shape valid, which is what makes the R1a migration incremental.
    """

    success: bool
    output: str = ""
    data: dict[str, Any] | None = None
    artifacts: tuple[ToolArtifact, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ToolOutput:
    """A resolved tool's output paired with its originating call id.

    Captured from the SDK's ``tool_output`` stream event so the session can
    persist ``role=tool`` messages (strict-pairing contract of the Java shape).
    """

    call_id: str
    tool_name: str
    output: str
    success: bool = True
    data: dict[str, Any] | None = None
    artifacts: tuple[ToolArtifact, ...] = ()


@dataclass(frozen=True)
class ToolExecution:
    """Pairing unit of a resolved tool call and its result."""

    tool_call: ToolCall
    result: ToolResult


def tool_call_from_ref(ref: ToolCallRef) -> ToolCall:
    """Parse ``ref.arguments`` JSON into ``params``; never raise on bad JSON."""
    try:
        parsed = json.loads(ref.arguments)
        params = parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        params = {}
    return ToolCall(id=ref.id, name=ref.name, params=params)


#: r17/T5: what counts as an image a message can carry.  One table, two
#: readers: the loop extracting ``@shot.png`` and the file-mention block that
#: must *not* also inject those bytes as text.
IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class ImageRef:
    """A message image, referenced by path (r17/T5).

    The bytes are *not* stored in the message: a session file must not grow by
    the screenshot.  ``path`` is workspace-relative; whoever sends the request
    loads it through the loader the engine is handed for that call.
    """

    path: str
    mime: str
    name: str
    #: The file's size, recorded where it was already known -- the bytes of a
    #: paste, or a stat at the moment ``@path`` is extracted (r19/F3).  The
    #: context budget charges images by it, so the alternative is reading the
    #: file every time the history is measured.  Zero for a reference written
    #: before this field existed, which keeps those sessions costing what they
    #: always did.
    bytes: int = 0


@dataclass
class ChatMessage:
    """A single conversation message.

    ``content`` is ``None`` for assistant messages that only carry tool calls.

    ``reasoning`` is the model's thinking for an assistant step (R1-4).  It
    never satisfies the assistant invariant on its own — reasoning without
    speech or action is not a message — so a reasoning-only step is a loud
    ``ValueError`` here, not silent corruption downstream.
    """

    role: Role
    content: str | None
    timestamp: str
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list[ToolCallRef] | None = None
    reasoning: str | None = None
    #: r17/T5: images the user attached (clipboard paste or an @path.png).  Empty
    #: for every message that predates them, so nothing downstream changed.
    images: tuple[ImageRef, ...] = ()
    #: r18/F3: True when the *loop* wrote this "user" message — a todo nudge, a
    #: fatigue retry, a steering line, the closing prompt — rather than a human
    #: typing.  Nothing on the wire reads it (``mapping`` never copies it, so the
    #: provider request is byte-identical); the runtime reads it to tell "which
    #: turn did the human start" apart from "what did the harness say during it".
    #: Without it, undo picks the nudge as the turn to take back, and the
    #: volatile mention blocks rebuild from a system string.
    synthetic: bool = False
    #: r18/F4: the side effects this step produced, as the tools reported them.
    #: Written to the session so ``/undo`` knows which files the turn created --
    #: git cannot tell those from files the human made while the turn ran.
    artifacts: tuple[ToolArtifact, ...] = ()
    #: P1 (补充式方案 06): a stable identity minted at creation.  Two readers
    #: need it to name one message across representations -- the undo record
    #: (which stores the removed messages verbatim and puts them back on redo)
    #: and any future audit.  Empty means "not yet minted"; ``__post_init__``
    #: fills it, so the fifteen construction sites need no change, and a
    #: ``from_dict`` that carries a persisted id keeps it.
    #:
    #: ``compare=False``: equality stays **content-based**, which is the
    #: contract every existing reader already relies on (``_ensure_pinned``,
    #: the compaction tests).  The id names a message; it does not define what
    #: the message says, and two equal bodies are still equal.
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("role='tool' messages must carry a tool_call_id")
        if self.role == "assistant" and not self.content and not self.tool_calls:
            raise ValueError("assistant messages must carry content or tool_calls")
        if not self.id:
            self.id = uuid4().hex
