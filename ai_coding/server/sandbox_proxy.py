"""Minimal HTTP/CONNECT proxy for the network wall's ``proxy`` mode (M-D2 §C.5).

The sandbox has its own netns (``--unshare-net``) and can only reach loopback;
this proxy lives **outside** it, listening on a Unix socket that is bound into
the sandbox.  The in-sandbox bridge (``security/sandbox_bridge.py``) relays
loopback connections to that socket, so a normal ``HTTP_PROXY=http://127.0.0.1:PORT``
works -- ``curl`` rejects ``unix://`` as a proxy scheme (measured).

What it refuses, and why:

* **A domain not in ``allow``** -- empty means deny everything; ``"*"`` never
  matches (the config layer rejects it at load time, and the proxy keeps the
  rule locally so a hand-built instance cannot open the wall).
* **A non-public resolved address** -- the whitelist decides by *name*, but the
  connection is made by *IP*.  A whitelisted ``evil.com`` could resolve to
  ``169.254.169.254`` (cloud metadata) or an intranet address, so the resolution
  result is checked after DNS and the connection is pinned to the checked
  address (no second lookup, no rebinding window, §C.6).
* **Malformed or oversized requests** -- the sandbox is adversarial input by
  construction: request line/header sizes, illegal ports and non-ASCII bytes
  are rejected rather than parsed loosely.

Each connection gets its own thread: a single slow tunnel must not stall every
other request (review P2-12).  The socket file is ``chmod 0600`` -- another
local user must not be able to borrow the sandbox's exit (review P2-2).

Non-HTTP protocols (databases, raw TCP, UDP) are not proxied.  Domain
whitelisting is blind to bare IP/CIDR/UDP traffic; that is a recorded scope cut
(``docs/plan-m-d2.md`` §7.1), not an accident.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import stat
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = ["SandboxProxy"]

_CHUNK = 64 * 1024
_MAX_HEADERS = 100
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "proxy-connection",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
    }
)

#: ``(family, socktype, proto, sockaddr)`` as ``socket.connect`` wants it.
Address = tuple[int, int, int, tuple[Any, ...]]
ResolveTarget = Callable[[str, int], Address]


class _BadRequest(Exception):
    """The client sent something this proxy will not parse."""


class _Refused(Exception):
    """The request parsed, but policy forbids it."""


def _strip_port(value: str) -> str:
    """Lower-case a host and drop a trailing port, ``[]`` and trailing dot.

    ``CONNECT`` carries ``host:port`` and an ``allow`` entry does not, so the
    comparison has to happen on the host alone.  IPv6 literals keep their
    brackets' contents; a bare IPv6 address has more than one colon and is
    returned unchanged.
    """
    host = value.strip().lower().rstrip(".")
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[1:end]
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def _host_allowed(host: str, allow: Sequence[str]) -> bool:
    """``host == entry`` or ``host.endswith("." + entry)``, never a substring.

    The exact/suffix rule is the whole point: a substring test would let
    ``evil-example.com`` and ``example.com.evil.com`` through.  An empty
    allow-list denies everything, and ``"*"`` is treated as no entry -- a
    wildcard is not a wall.
    """
    candidate = _strip_port(host)
    if not candidate:
        return False
    for entry in allow:
        allowed = _strip_port(str(entry))
        if not allowed or allowed == "*":
            continue
        if candidate == allowed or candidate.endswith("." + allowed):
            return True
    return False


def _is_forbidden_ip(ip: str) -> bool:
    """True for private / loopback / link-local / metadata / other non-public IPs.

    ``169.254.169.254`` is link-local and is therefore covered.  An
    unparsable address is forbidden too: fail-closed is the only safe answer
    when the point of the check is to keep the sandbox out of the intranet.
    """
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or not addr.is_global
    )


def _resolve_public_address(host: str, port: int) -> Address:
    """Resolve ``host`` and refuse the request if **any** answer is non-public.

    The returned sockaddr is the one the caller must connect to, so there is no
    second lookup between the check and the connection (DNS rebinding, §C.6).
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _Refused(f"cannot resolve {host!r}: {exc}") from exc
    if not infos:
        raise _Refused(f"no address for {host!r}")
    resolved: list[Address] = []
    for family, socktype, proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if _is_forbidden_ip(ip):
            raise _Refused(f"{host!r} resolves to a non-public address ({ip})")
        resolved.append((int(family), int(socktype), int(proto), tuple(sockaddr)))
    return resolved[0]


def _connect(address: Address, timeout: float) -> socket.socket:
    family, socktype, proto, sockaddr = address
    sock = socket.socket(family, socktype, proto)
    sock.settimeout(timeout)
    try:
        sock.connect(sockaddr)
    except OSError:
        sock.close()
        raise
    return sock


class _ConnReader:
    """A tiny buffered reader over a socket.

    ``socket.makefile`` may read past the headers, which would lose bytes when a
    CONNECT tunnel takes over the connection; this keeps the buffer explicit so
    those bytes can be forwarded first.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    def read_line(self, limit: int) -> bytes | None:
        while b"\r\n" not in self._buf:
            if len(self._buf) > limit:
                raise _BadRequest("line too long")
            chunk = self._sock.recv(_CHUNK)
            if not chunk:
                if not self._buf:
                    return None
                raise _BadRequest("truncated request")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\r\n", 1)
        if len(line) > limit:
            raise _BadRequest("line too long")
        return line

    def read_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            chunk = self._sock.recv(_CHUNK)
            if not chunk:
                raise _BadRequest("truncated body")
            self._buf += chunk
        data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def take_buffer(self) -> bytes:
        data, self._buf = self._buf, b""
        return data


def _validate_host(host: str) -> None:
    if not host:
        raise _BadRequest("empty host")
    if any(ord(char) < 0x21 or char in "\r\n" for char in host):
        raise _BadRequest("illegal character in host")


def _read_headers(reader: _ConnReader, max_bytes: int) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    total = 0
    while True:
        line = reader.read_line(max_bytes)
        if line is None:
            raise _BadRequest("unexpected end of headers")
        if line == b"":
            return headers
        total += len(line) + 2
        if total > max_bytes or len(headers) > _MAX_HEADERS:
            raise _BadRequest("headers too large")
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _BadRequest("non-ascii header") from exc
        name, separator, value = text.partition(":")
        if not separator:
            raise _BadRequest("malformed header")
        name = name.strip()
        value = value.strip()
        if not name or any(ord(char) <= 0x20 or ord(char) >= 0x7F for char in name):
            raise _BadRequest("illegal header name")
        if any(char == "\x00" or (ord(char) < 0x20 and char != "\t") for char in value):
            raise _BadRequest("illegal header value")
        headers.append((name, value))
    return headers


def _parse_connect_target(target: str) -> tuple[str, int]:
    try:
        parsed = urlsplit("//" + target)
        port = parsed.port
    except ValueError as exc:
        raise _BadRequest(f"invalid CONNECT target: {target!r}") from exc
    host = parsed.hostname
    if host is None:
        raise _BadRequest("CONNECT requires host:port")
    if port is None:
        raise _BadRequest("CONNECT requires an explicit port")
    _validate_host(host)
    return host, port


def _parse_http_target(
    target: str, headers: list[tuple[str, str]]
) -> tuple[str, int, str]:
    if target.startswith("http://") or target.startswith("https://"):
        try:
            parsed = urlsplit(target)
            port = parsed.port or 80
        except ValueError as exc:
            raise _BadRequest(f"invalid request target: {target!r}") from exc
        if parsed.scheme != "http":
            raise _BadRequest("only plain http is proxied; use CONNECT for https")
        host = parsed.hostname
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
    else:
        path = target
        host_header = next(
            (value for name, value in headers if name.lower() == "host"), ""
        )
        if not host_header:
            raise _BadRequest("missing Host header")
        try:
            parsed = urlsplit("//" + host_header)
            port = parsed.port or 80
        except ValueError as exc:
            raise _BadRequest(f"invalid Host header: {host_header!r}") from exc
        host = parsed.hostname
    if host is None:
        raise _BadRequest("missing host")
    _validate_host(host)
    return host, port, path


def _send_error(conn: socket.socket, code: int, reason: str, detail: str) -> None:
    body = (detail + "\n").encode("utf-8", errors="replace")
    head = (
        f"HTTP/1.1 {code} {reason}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        conn.sendall(head + body)
    except OSError:
        pass


def _authorize(proxy: SandboxProxy, host: str) -> None:
    if not _host_allowed(host, proxy.allow):
        raise _Refused(f"domain not in allow-list: {host}")


def _body_length(headers: list[tuple[str, str]]) -> int:
    """Validate the request framing and return the declared body length.

    Done **before** connecting: a conflicting ``Content-Length`` is the classic
    request-smuggling primitive and must not depend on an upstream being
    reachable.  Chunked bodies are outside this minimal proxy and are refused
    rather than forwarded half-understood.
    """
    content_lengths: set[int] = set()
    for name, value in headers:
        lower = name.lower()
        if lower == "content-length":
            try:
                declared = int(value)
            except ValueError as exc:
                raise _BadRequest("invalid Content-Length") from exc
            if declared < 0:
                raise _BadRequest("negative Content-Length")
            content_lengths.add(declared)
        elif lower == "transfer-encoding" and "chunked" in value.lower():
            raise _BadRequest("chunked request bodies are not supported")
    if len(content_lengths) > 1:
        raise _BadRequest("conflicting Content-Length")
    return next(iter(content_lengths), 0)


def _forward_http(
    conn: socket.socket,
    upstream: socket.socket,
    reader: _ConnReader,
    method: str,
    path: str,
    headers: list[tuple[str, str]],
    host: str,
    content_length: int,
) -> None:
    lines = [f"{method} {path} HTTP/1.1"]
    has_host = False
    for name, value in headers:
        lower = name.lower()
        if lower in _HOP_BY_HOP:
            continue
        if lower == "host":
            has_host = True
        lines.append(f"{name}: {value}")
    if not has_host:
        lines.append(f"Host: {host}")
    lines.append("Connection: close")
    upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    remaining = content_length
    while remaining > 0:
        chunk = reader.read_exact(min(_CHUNK, remaining))
        upstream.sendall(chunk)
        remaining -= len(chunk)
    while True:
        data = upstream.recv(_CHUNK)
        if not data:
            break
        conn.sendall(data)


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Relay both directions until EOF (CONNECT tunnels)."""
    done = threading.Event()
    lock = threading.Lock()
    remaining = 2

    def pump(src: socket.socket, dst: socket.socket) -> None:
        nonlocal remaining
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
            with lock:
                remaining -= 1
                if remaining <= 0:
                    done.set()

    threads = [
        threading.Thread(target=pump, args=(client, upstream), daemon=True),
        threading.Thread(target=pump, args=(upstream, client), daemon=True),
    ]
    for thread in threads:
        thread.start()
    done.wait()
    for sock in (client, upstream):
        try:
            sock.close()
        except OSError:
            pass


def _serve_connection(conn: socket.socket, proxy: SandboxProxy) -> None:
    conn.settimeout(proxy.connect_timeout)
    reader = _ConnReader(conn)
    try:
        line = reader.read_line(proxy.max_request_line)
        if line is None:
            return
        if line == b"":
            raise _BadRequest("empty request line")
        try:
            text = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise _BadRequest("non-ascii request line") from exc
        parts = text.split()
        if len(parts) != 3:
            raise _BadRequest("malformed request line")
        method, target, version = parts
        if not version.startswith("HTTP/"):
            raise _BadRequest("unsupported protocol version")
        headers = _read_headers(reader, proxy.max_header_bytes)

        if method.upper() == "CONNECT":
            host, port = _parse_connect_target(target)
            _authorize(proxy, host)
            upstream = _connect(proxy.resolve_target(host, port), proxy.connect_timeout)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            buffered = reader.take_buffer()
            if buffered:
                upstream.sendall(buffered)
            # Keep a per-connection idle bound: a tunnel that goes quiet must
            # not hold a thread forever (an adversarial client can open many).
            upstream.settimeout(proxy.connect_timeout)
            _relay(conn, upstream)
            return

        host, port, path = _parse_http_target(target, headers)
        _authorize(proxy, host)
        content_length = _body_length(headers)
        upstream = _connect(proxy.resolve_target(host, port), proxy.connect_timeout)
        try:
            upstream.settimeout(proxy.connect_timeout)
            _forward_http(
                conn,
                upstream,
                reader,
                method,
                path,
                headers,
                host,
                content_length,
            )
        finally:
            try:
                upstream.close()
            except OSError:
                pass
    except _Refused as exc:
        _send_error(conn, 403, "Forbidden", str(exc))
    except _BadRequest as exc:
        _send_error(conn, 400, "Bad Request", str(exc))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class SandboxProxy:
    """A Unix-socket HTTP/CONNECT proxy with a domain allow-list.

    ``start()`` is synchronous and returns once the socket is bound, so the
    composition root can guarantee ``--bind <socket>`` will find a file.  A
    bind failure raises ``OSError``; the caller degrades to ``deny`` -- never to
    ``off`` (that would silently hand the sandbox the host network).

    ``resolve_target`` is injectable for tests that need a local upstream; the
    default does the DNS + non-public-address check described above.
    """

    def __init__(
        self,
        socket_path: str | Path,
        allow: Sequence[str] = (),
        *,
        connect_timeout: float = 30.0,
        resolve_target: ResolveTarget | None = None,
        max_request_line: int = 8192,
        max_header_bytes: int = 65536,
    ) -> None:
        self._socket_path = Path(socket_path).expanduser().resolve()
        self._allow = tuple(
            str(entry).strip().lower().rstrip(".")
            for entry in allow
            if str(entry).strip()
        )
        self.connect_timeout = connect_timeout
        self.resolve_target: ResolveTarget = resolve_target or _resolve_public_address
        self.max_request_line = max_request_line
        self.max_header_bytes = max_header_bytes
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    @property
    def allow(self) -> tuple[str, ...]:
        return self._allow

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def available(self) -> bool:
        return self._listener is not None and not self._closed.is_set()

    def start(self) -> SandboxProxy:
        path = self._socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            # A live socket means another proxy owns this path; stealing it
            # would silently split the traffic.  Only a stale socket (nobody
            # accepts) is replaced; a regular file is never deleted.  Either
            # way the caller decides: an OSError here degrades the wall to
            # ``deny``, never to ``off``.
            try:
                is_socket = stat.S_ISSOCK(path.stat().st_mode)
            except OSError:
                is_socket = False
            if not is_socket:
                raise OSError(f"proxy socket path exists and is not a socket: {path}")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                probe.connect(str(path))
            except OSError:
                probe.close()
                path.unlink()
            else:
                probe.close()
                raise OSError(f"proxy socket already in use: {path}")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen(64)
            os.chmod(path, 0o600)
        except OSError:
            listener.close()
            try:
                path.unlink()
            except OSError:
                pass
            raise
        listener.settimeout(0.5)
        self._listener = listener
        self._closed.clear()
        self._thread = threading.Thread(
            target=self._accept_loop, name="sandbox-proxy", daemon=True
        )
        self._thread.start()
        return self

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._closed.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._serve, args=(conn,), name="sandbox-proxy-conn", daemon=True
            ).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            _serve_connection(conn, self)
        except Exception:  # noqa: BLE001 - one bad connection must not stop the proxy
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._closed.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        try:
            self._socket_path.unlink()
        except OSError:
            pass
