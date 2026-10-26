#!/usr/bin/env python3
"""In-sandbox proxy bridge for the network wall's ``proxy`` mode (M-D2 §C.1).

``curl`` (and every other HTTP client) rejects ``unix://`` as a proxy scheme
(measured: ``curl: (7) Unsupported proxy scheme``).  The proxy itself must live
**outside** the netns -- it needs the host's connectivity -- while the sandbox
only has loopback.  This program closes that gap from the inside:

    <bwrap ...> -- <this script> --unix <proxy.sock> -- <user argv...>

It binds a loopback listener, hands the command a normal ``http://`` proxy URL
through the environment, and relays every accepted connection to the bound
Unix socket.  The user command is therefore still a plain argv -- ``wrap()``
returns ``list[str]`` and no shell re-parsing happens.

**Lifecycle.**  ``execv`` destroys every thread in the process, so a background
forwarding *thread* would die exactly when the bridge starts working (the
original design's contradiction, review P1-2).  The relay therefore runs in a
**forked child process**: the parent ``execv``s the user command (same pid, so
``--die-with-parent`` still tracks it) and the child accepts and relays.
``PR_SET_PDEATHSIG`` (plus a periodic ``getppid()`` check) makes the child exit
with the command instead of lingering.

**Fail-closed.**  A bridge that cannot listen, cannot reach the proxy socket or
cannot exec the command prints ``sandbox proxy bridge failed: ...`` and exits
non-zero: the user command never runs, so the outcome is "no network", never
"host network by accident".

**Argument protocol.**  The bridge's own options come first and the **first**
``--`` is the boundary; everything after it is the user argv verbatim (so a
user command that itself contains ``--`` is unambiguous, review P3-3).
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading

__all__ = ["main"]

_CHUNK = 64 * 1024
_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


class _BridgeError(Exception):
    """A startup failure that must stop the command from running."""


def _parse(argv: list[str]) -> tuple[str, int, list[str]]:
    """Parse ``--unix SOCK [--port N] -- <command...>``.

    Only the bridge's own options may appear before the boundary.  Anything
    else -- including a stray ``--unix`` meant for the user command -- is a
    protocol error rather than a guess, because guessing here would run the
    wrong thing with network access.
    """
    sock_path: str | None = None
    port = 0
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            command = argv[index + 1 :]
            if sock_path is None:
                raise _BridgeError("missing --unix <socket>")
            if not command:
                raise _BridgeError("missing command after --")
            return sock_path, port, command
        if arg == "--unix":
            if index + 1 >= len(argv):
                raise _BridgeError("--unix requires a path")
            sock_path = argv[index + 1]
            index += 2
            continue
        if arg == "--port":
            if index + 1 >= len(argv):
                raise _BridgeError("--port requires a number")
            try:
                port = int(argv[index + 1])
            except ValueError as exc:
                raise _BridgeError(f"invalid --port value: {argv[index + 1]!r}") from exc
            if not 0 <= port <= 65535:
                raise _BridgeError(f"--port out of range: {port}")
            index += 2
            continue
        raise _BridgeError(f"unexpected argument before --: {arg!r}")
    raise _BridgeError("missing -- separator")


def _set_pdeathsig(sig: int) -> None:
    """Ask the kernel to signal this process when its parent dies.

    Best-effort: the periodic ``getppid()`` check in the accept loop is the
    fallback for a libc that does not expose ``prctl``.
    """
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, sig, 0, 0, 0)  # PR_SET_PDEATHSIG
    except Exception:
        return


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """Copy until EOF, then half-close the destination."""
    try:
        while True:
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    threads = [
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True),
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for sock in (client, upstream):
        try:
            sock.close()
        except OSError:
            pass


def _handle(client: socket.socket, sock_path: str) -> None:
    """One client connection: open a fresh connection to the proxy socket."""
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.settimeout(30.0)
        upstream.connect(sock_path)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        return
    upstream.settimeout(None)
    client.settimeout(None)
    _relay(client, upstream)


def _forwarding_loop(listener: socket.socket, sock_path: str) -> None:
    """The forked child: accept and relay until the command exits.

    Every failure except a real shutdown is survivable and the loop continues.
    A non-timeout ``OSError`` from ``accept`` (``EMFILE``, ``ECONNABORTED``,
    ``EINVAL`` ...) used to end the loop, which silently killed the relay while
    the user command kept running -- "the bridge started but forwarding is
    dead", and the command then had no network at all (review P2-4).  A thread
    that cannot be created (``RuntimeError`` from ``EAGAIN`` when the process is
    out of threads) drops that one connection instead of dying with it.
    """
    _set_pdeathsig(signal.SIGTERM)
    if os.getppid() == 1:
        os._exit(0)
    listener.settimeout(1.0)
    while True:
        try:
            client, _ = listener.accept()
        except TimeoutError:
            if os.getppid() == 1:
                break
            continue
        except Exception:
            if os.getppid() == 1:
                break
            continue
        try:
            threading.Thread(
                target=_handle, args=(client, sock_path), daemon=True
            ).start()
        except Exception:
            try:
                client.close()
            except OSError:
                pass
            continue


def _start_listener(port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(64)
    except OSError as exc:
        listener.close()
        raise _BridgeError(f"cannot listen on loopback: {exc}") from exc
    return listener


def _check_proxy_reachable(sock_path: str) -> None:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(5.0)
        probe.connect(sock_path)
    except OSError as exc:
        raise _BridgeError(f"proxy socket is not reachable: {sock_path}: {exc}") from exc
    finally:
        probe.close()


def main(argv: list[str]) -> int:
    try:
        sock_path, port, command = _parse(argv)
        if not os.path.exists(sock_path):
            raise _BridgeError(f"proxy socket not found: {sock_path}")
        _check_proxy_reachable(sock_path)
        listener = _start_listener(port)
    except _BridgeError as exc:
        print(f"sandbox proxy bridge failed: {exc}", file=sys.stderr, flush=True)
        return 127

    bound_port = int(listener.getsockname()[1])
    try:
        pid = os.fork()
    except OSError as exc:
        listener.close()
        print(
            f"sandbox proxy bridge failed: cannot fork the relay process: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 127
    if pid == 0:  # pragma: no cover - the forked child never returns
        # The child keeps the inherited listener; the parent closes its copy
        # below.  No re-bind: that would race with the port already in use.
        _forwarding_loop(listener, sock_path)
        os._exit(0)

    listener.close()
    proxy_url = f"http://127.0.0.1:{bound_port}"
    env = dict(os.environ)
    for name in _PROXY_ENV:
        env[name] = proxy_url
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    try:
        os.execvpe(command[0], command, env)
    except OSError as exc:
        print(
            f"sandbox proxy bridge failed: cannot exec {command[0]!r}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 127
    return 0  # pragma: no cover - execvpe never returns


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(main(sys.argv[1:]))
