"""The asyncio Unix-socket JSON-RPC server (M0 of the OpenCode-style TUI).

One client connection at a time (the TUI process).  The server owns:

* **dispatch** — incoming requests are routed to a handler table
  (``{"initialize": fn, ...}``); handlers are async or sync;
* **event push** — ``push()`` writes a notification to the connected client;
* **server-initiated requests** — ``call()`` sends a request with an id and
  awaits the peer's answer (the approval prompt round-trip).

Every failure mode is contained: a bad line is dropped, an unknown method
answers ``METHOD_NOT_FOUND``, a handler exception answers ``INTERNAL_ERROR``
and logs — the accept loop never dies from a peer mistake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ai_coding.server.protocol import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    decode_line,
    encode_error,
    encode_notification,
    encode_request,
    encode_result,
)

_log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]

#: How long a server-initiated request waits for the peer (approval prompt).
DEFAULT_CALL_TIMEOUT = 300.0


class IpcServer:
    """Serve one TUI client over a Unix socket; never raise from the loop."""

    def __init__(
        self,
        socket_path: str | Path,
        handlers: dict[str, Handler],
        *,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self._path = Path(socket_path)
        self._handlers = handlers
        self._call_timeout = call_timeout
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()

    # ---- lifecycle --------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        return self._path

    async def start(self) -> None:
        """Bind the socket and start accepting.  Removes a stale socket file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.unlink()
        self._server = await asyncio.start_unix_server(
            self._on_client, path=str(self._path)
        )
        _log.debug("ipc server listening on %s", self._path)

    async def stop(self) -> None:
        self._closed.set()
        self._fail_pending(ConnectionError("server stopped"))
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass
        writer = self._writer
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    async def wait_connected(self) -> None:
        """Block until a client attaches (used by the launcher for startup)."""
        await self._connected.wait()

    # ---- event push / server-initiated call --------------------------------

    async def push(self, method: str, params: Any = None) -> None:
        """Send a notification; silently dropped when no client is attached."""
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(encode_notification(method, params))
            await writer.drain()
        except (ConnectionError, RuntimeError):
            self._writer = None

    def push_nowait(self, method: str, params: Any = None) -> None:
        """``push`` from a sync context (the observer bridge)."""
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(encode_notification(method, params))
        except (ConnectionError, RuntimeError):
            self._writer = None

    async def call(self, method: str, params: Any = None) -> Any:
        """Send a request and await the peer's answer (raises on timeout)."""
        writer = self._writer
        if writer is None:
            raise ConnectionError("no client connected")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            writer.write(encode_request(request_id, method, params))
            await writer.drain()
            return await asyncio.wait_for(future, timeout=self._call_timeout)
        finally:
            self._pending.pop(request_id, None)

    # ---- connection handling ----------------------------------------------

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # One TUI at a time: a second connection replaces the first.
        self._writer = writer
        self._connected.set()
        _log.debug("ipc client connected")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                await self._handle_line(line)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the accept loop die
            _log.exception("ipc connection failed")
        finally:
            if self._writer is writer:
                self._writer = None
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _handle_line(self, line: bytes) -> None:
        message = decode_line(line)
        if message is None:
            await self._send(encode_error(None, PARSE_ERROR, "malformed JSON"))
            return
        # A response to a server-initiated request (an approval answer).
        if "id" in message and "method" not in message:
            self._resolve_pending(message)
            return
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str):
            await self._send(encode_error(request_id, INVALID_REQUEST, "missing method"))
            return
        handler = self._handlers.get(method)
        if handler is None:
            await self._send(
                encode_error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")
            )
            return
        params = message.get("params") or {}
        try:
            result = handler(params)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 — a handler bug answers, not crashes
            _log.exception("ipc handler %s failed", method)
            await self._send(encode_error(request_id, INTERNAL_ERROR, str(exc)))
            return
        if request_id is not None:
            await self._send(encode_result(request_id, result))

    def _resolve_pending(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(RuntimeError(str(message["error"])))
        else:
            future.set_result(message.get("result"))

    async def _send(self, payload: bytes) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, RuntimeError):
            self._writer = None

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


__all__ = ["DEFAULT_CALL_TIMEOUT", "Handler", "IpcServer"]
