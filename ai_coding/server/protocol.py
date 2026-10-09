"""Wire protocol: newline-delimited JSON-RPC 2.0 over a Unix socket.

The TUI frontend (TypeScript) and this backend exchange one JSON object per
line.  Two directions:

* the frontend sends **requests** (``{"id": N, "method": ..., "params": ...}``)
  and this side answers with ``{"id": N, "result": ...}`` or an ``error``;
* this side pushes **notifications** (``{"method": "event", "params": ...}``,
  no id) for loop events, and occasionally sends its own **requests** (an id)
  — the approval prompt is one — which the frontend answers.

Everything here is pure encode/decode: no IO, so the framing is testable in
isolation (``tests/test_server_protocol.py``).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["decode_line", "encode_error", "encode_notification", "encode_request", "encode_result"]

#: JSON-RPC error codes this side can produce.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def encode_request(request_id: int | str, method: str, params: Any = None) -> bytes:
    """One server-initiated request (used for approvals)."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return _dump(message)


def encode_notification(method: str, params: Any = None) -> bytes:
    """One event push; no id, so the peer never answers it."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return _dump(message)


def encode_result(request_id: int | str, result: Any) -> bytes:
    return _dump({"jsonrpc": "2.0", "id": request_id, "result": result})


def encode_error(request_id: int | str | None, code: int, message: str) -> bytes:
    return _dump(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def decode_line(line: bytes | str) -> dict[str, Any] | None:
    """Parse one wire line; ``None`` for garbage (never raises).

    A malformed line must not kill the server loop: the socket is fed by a
    separate process we do not control, and the protocol explicitly allows
    dropping unparseable messages (JSON-RPC parse error).
    """
    try:
        text = line.decode("utf-8") if isinstance(line, bytes) else line
    except UnicodeDecodeError:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _dump(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
