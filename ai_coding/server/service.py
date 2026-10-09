"""JSON-RPC service over a Unix socket, built on python-lsp-jsonrpc (M0.5).

Replaces the hand-rolled ``protocol.py`` + ``server.py`` (plan v2).  The
library is the Python port of vscode-jsonrpc (maintained by the python-lsp
team); the TS frontend pairs with ``vscode-jsonrpc`` itself, and the two
share the Content-Length framed JSON-RPC 2.0 wire format (verified in
``docs/opencode-gui-replica-plan.md`` §3.0).

Threading model:

* a daemon **serve thread** blocks in ``accept()`` then in
  ``JsonRpcStreamReader.listen`` — incoming requests dispatch to an
  ``Endpoint`` thread pool;
* handlers that need the asyncio world call :meth:`run` (blocking bridge via
  ``run_coroutine_threadsafe``) or :meth:`spawn` (fire-and-forget);
* the asyncio side pushes events with :meth:`notify` (the writer holds its own
  lock) and asks the client with :meth:`call` (approvals; awaited via
  ``asyncio.wrap_future``).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from pylsp_jsonrpc.endpoint import Endpoint
from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

_log = logging.getLogger(__name__)

__all__ = ["IpcService", "Handler"]

Handler = Callable[[dict[str, Any]], Any]


def _with_dict_params(handler: Handler) -> Handler:
    """Adapt a handler to the library's ``params=None`` convention."""

    def call(params: Any = None) -> Any:
        return handler(params if isinstance(params, dict) else {})

    return call


class IpcService:
    """Serve one TUI client over a Unix socket; never raise from the loop."""

    def __init__(
        self,
        socket_path: str | Path,
        loop: asyncio.AbstractEventLoop,
        dispatcher: dict[str, Handler] | None = None,
    ) -> None:
        self._path = Path(socket_path)
        self._loop = loop
        self._dispatcher: dict[str, Handler] = dispatcher if dispatcher is not None else {}
        self._server_sock: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._endpoint: Endpoint | None = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()

    # ---- lifecycle ---------------------------------------------------------

    @property
    def socket_path(self) -> Path:
        return self._path

    @property
    def dispatcher(self) -> dict[str, Handler]:
        """The live handler table; the launcher fills it after construction."""
        return self._dispatcher

    def start(self) -> None:
        """Bind, listen and start the serve thread (fast; safe on the loop)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            self._path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._path))
        server.listen(1)
        self._server_sock = server
        self._thread = threading.Thread(target=self._serve, name="ipc-serve", daemon=True)
        self._thread.start()
        _log.debug("ipc service listening on %s", self._path)

    def _serve(self) -> None:
        """Accept one client, then pump its stream until it closes."""
        server = self._server_sock
        if server is None:
            return
        try:
            conn, _addr = server.accept()
        except OSError:
            return  # closed before a client attached
        self._conn = conn
        writer = JsonRpcStreamWriter(conn.makefile("wb"))
        # The library passes ``None`` when a request omits params; our handlers
        # all take a dict, so normalize at the boundary.  The snapshot is safe:
        # the launcher fills the table before the child can connect.
        safe = {name: _with_dict_params(handler) for name, handler in self._dispatcher.items()}
        endpoint = Endpoint(safe, writer.write)
        self._endpoint = endpoint
        self._connected.set()
        _log.debug("ipc client connected")
        try:
            reader = JsonRpcStreamReader(conn.makefile("rb"))
            reader.listen(endpoint.consume)  # blocks until EOF
        except Exception:  # noqa: BLE001 — a dead client must not kill the process
            _log.debug("ipc reader stopped", exc_info=True)
        finally:
            endpoint.shutdown()

    def wait_connected(self, timeout: float | None = None) -> bool:
        return self._connected.wait(timeout)

    def close(self) -> None:
        if self._endpoint is not None:
            try:
                self._endpoint.shutdown()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass
        for sock in (self._conn, self._server_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- outbound ----------------------------------------------------------

    def notify(self, method: str, params: Any = None) -> None:
        """Push a notification to the client (thread-safe; no client = drop)."""
        endpoint = self._endpoint
        if endpoint is None:
            return
        try:
            endpoint.notify(method, params)
        except Exception:  # noqa: BLE001 — a dead client must not break the loop
            _log.debug("notify failed", exc_info=True)

    async def call(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        """Send a request and await the client's answer (approvals)."""
        endpoint = self._endpoint
        if endpoint is None:
            raise ConnectionError("no client connected")
        future = endpoint.request(method, params)
        wrapped = asyncio.wrap_future(future, loop=self._loop)
        if timeout is not None:
            return await asyncio.wait_for(wrapped, timeout=timeout)
        return await wrapped

    # ---- bridges for handlers (they run on the endpoint's pool threads) ----

    def run(self, coro: Coroutine[Any, Any, Any], timeout: float | None = None) -> Any:
        """Run a coroutine on the asyncio loop and block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Schedule a coroutine on the asyncio loop without waiting."""
        asyncio.run_coroutine_threadsafe(coro, self._loop)
