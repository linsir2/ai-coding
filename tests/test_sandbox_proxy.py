"""M-D2 §C.5/§C.6: the sandbox proxy's whitelist, tunnel and attack surface.

The proxy is the **only** way out of the ``proxy`` network tier, so it is a
single point that has to be tested as a parser and as a policy:

* the whitelist is exact-or-suffix (a substring test would admit
  ``evil-example.com``), the port is stripped first, empty means deny and
  ``"*"`` never matches;
* DNS is resolved **before** the connection and a non-public answer is refused
  (metadata / intranet / loopback), which is the DNS-rebinding defence;
* malformed and oversized requests are rejected rather than parsed loosely;
* every connection gets its own thread, and the socket file is ``0600``.
"""

from __future__ import annotations

import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_coding.config.models import NetworkConfig, ProcessSandboxConfig
from ai_coding.server.sandbox_proxy import (
    SandboxProxy,
    _host_allowed,
    _is_forbidden_ip,
    _resolve_public_address,
)

_BODY = b"HELLO-FROM-UPSTREAM\n"


def _resolver(host: str, port: int):  # noqa: ANN202 - test helper
    """Map a whitelisted name to the local upstream (offline end-to-end)."""
    return (socket.AF_INET, socket.SOCK_STREAM, 0, ("127.0.0.1", port))


@pytest.fixture
def upstream_port():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            self.send_response(200)
            self.send_header("Content-Length", str(len(_BODY)))
            self.end_headers()
            self.wfile.write(_BODY)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def proxy(tmp_path):
    instance = SandboxProxy(
        tmp_path / "proxy.sock",
        allow=["allowed.test"],
        resolve_target=_resolver,
    ).start()
    try:
        yield instance
    finally:
        instance.close()


def _raw(sock_path: Path, payload: bytes, *, timeout: float = 10.0) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock_path))
        client.sendall(payload)
        chunks: list[bytes] = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        client.close()


def _connect_tunnel(
    sock_path: Path, host: str, port: int, request: bytes, *, timeout: float = 10.0
) -> bytes:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(sock_path))
        client.sendall(
            f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode()
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                break
            head += chunk
        assert b" 200 " in head, head
        client.sendall(request)
        chunks: list[bytes] = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)
    finally:
        client.close()


# ---- the tunnel itself ---------------------------------------------------


def test_plain_http_is_forwarded(proxy, upstream_port):
    response = _raw(
        proxy.socket_path,
        (
            f"GET http://allowed.test:{upstream_port}/ HTTP/1.1\r\n"
            "Host: allowed.test\r\n\r\n"
        ).encode(),
    )
    assert response.split(b"\r\n", 1)[0].endswith(b"200 OK"), response[:120]
    assert _BODY in response


def test_connect_tunnel_is_forwarded(proxy, upstream_port):
    body = _connect_tunnel(
        proxy.socket_path,
        "allowed.test",
        upstream_port,
        b"GET / HTTP/1.0\r\nHost: allowed.test\r\n\r\n",
    )
    assert _BODY in body


def test_an_unlisted_domain_is_refused(proxy, upstream_port):
    response = _raw(
        proxy.socket_path,
        (
            f"GET http://blocked.test:{upstream_port}/ HTTP/1.1\r\n"
            "Host: blocked.test\r\n\r\n"
        ).encode(),
    )
    assert response.startswith(b"HTTP/1.1 403"), response[:120]
    assert _BODY not in response


def test_an_empty_allow_list_denies_everything(tmp_path, upstream_port):
    instance = SandboxProxy(
        tmp_path / "proxy.sock", allow=[], resolve_target=_resolver
    ).start()
    try:
        response = _raw(
            instance.socket_path,
            (
                f"GET http://allowed.test:{upstream_port}/ HTTP/1.1\r\n"
                "Host: allowed.test\r\n\r\n"
            ).encode(),
        )
        assert response.startswith(b"HTTP/1.1 403"), response[:120]
    finally:
        instance.close()


# ---- whitelist semantics -------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("example.com", True),
        ("api.example.com", True),
        ("example.com:443", True),
        ("Example.COM:443", True),
        ("evil-example.com", False),
        ("example.com.evil.com", False),
        ("notexample.com", False),
    ],
)
def test_the_whitelist_is_exact_or_suffix(host, expected):
    assert _host_allowed(host, ["example.com"]) is expected


def test_a_port_is_stripped_before_matching():
    assert _host_allowed("example.com:8443", ["example.com"])
    assert _host_allowed("[::1]:8443", ["::1"])


def test_a_wildcard_never_matches():
    assert _host_allowed("*", ["*"]) is False
    assert _host_allowed("anything.test", ["*"]) is False


def test_the_allow_list_is_required_for_proxy_mode():
    with pytest.raises(ValidationError):
        NetworkConfig(mode="proxy")
    with pytest.raises(ValidationError):
        NetworkConfig(mode="proxy", allow=["*"])


def test_proxy_and_block_af_unix_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValidationError):
        ProcessSandboxConfig(
            network={"mode": "proxy", "proxy_socket": str(tmp_path / "p.sock")},
            block_af_unix=True,
        )


# ---- §C.6: the resolved address is checked, not just the name ------------


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
    ],
)
def test_non_public_addresses_are_rejected(ip):
    assert _is_forbidden_ip(ip) is True


@pytest.mark.parametrize("ip", ["1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(ip):
    assert _is_forbidden_ip(ip) is False


def test_a_hostname_resolving_to_a_private_address_is_refused(monkeypatch):
    import ai_coding.server.sandbox_proxy as module

    def fake_getaddrinfo(host, port, **kwargs):  # noqa: ANN001, ANN202
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.5", port))]

    monkeypatch.setattr(module.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(module._Refused):
        module._resolve_public_address("evil.test", 80)


def test_a_public_resolution_is_pinned_for_the_connection(monkeypatch):
    import ai_coding.server.sandbox_proxy as module

    def fake_getaddrinfo(host, port, **kwargs):  # noqa: ANN001, ANN202
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("93.184.216.34", port),
            )
        ]

    monkeypatch.setattr(module.socket, "getaddrinfo", fake_getaddrinfo)
    family, _socktype, _proto, sockaddr = _resolve_public_address("example.com", 443)
    assert family == socket.AF_INET
    assert sockaddr[0] == "93.184.216.34"


def test_localhost_is_refused_by_the_real_resolver():
    import ai_coding.server.sandbox_proxy as module

    with pytest.raises(module._Refused):
        module._resolve_public_address("localhost", 80)


# ---- hardening and lifecycle --------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        b"GARBAGE\r\n\r\n",
        b"GET / HTTP/1.1\r\n\r\n",  # no Host
        b"CONNECT allowed.test:notaport HTTP/1.1\r\nHost: allowed.test\r\n\r\n",
        b"GET " + b"a" * 9000 + b" HTTP/1.1\r\nHost: allowed.test\r\n\r\n",
        b"GET http://allowed.test:80/ HTTP/1.1\r\nHost: allowed.test\r\nX: \xff\r\n\r\n",
        b"POST http://allowed.test:80/ HTTP/1.1\r\nHost: allowed.test\r\n"
        b"Content-Length: 1\r\nContent-Length: 2\r\n\r\nx",
        b"POST http://allowed.test:80/ HTTP/1.1\r\nHost: allowed.test\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    ],
)
def test_malformed_requests_are_rejected(proxy, payload):
    response = _raw(proxy.socket_path, payload)
    assert response.startswith(b"HTTP/1.1 400"), response[:160]


def test_the_socket_is_owner_only(proxy):
    mode = stat.S_IMODE(proxy.socket_path.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_closing_the_proxy_removes_the_socket(tmp_path):
    instance = SandboxProxy(tmp_path / "proxy.sock", allow=["allowed.test"]).start()
    path = instance.socket_path
    assert path.exists()
    instance.close()
    assert not path.exists()
    assert instance.available is False


def test_a_live_socket_is_not_stolen(tmp_path):
    """A second proxy on the same path fails closed instead of splitting traffic."""
    first = SandboxProxy(tmp_path / "proxy.sock", allow=["allowed.test"]).start()
    try:
        with pytest.raises(OSError):
            SandboxProxy(tmp_path / "proxy.sock", allow=["allowed.test"]).start()
    finally:
        first.close()


def test_a_stale_socket_is_replaced(tmp_path):
    """A socket file nobody accepts is a leftover, not a running proxy."""
    path = tmp_path / "proxy.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    instance = SandboxProxy(path, allow=["allowed.test"]).start()
    try:
        assert instance.available is True
    finally:
        instance.close()


def test_a_regular_file_at_the_socket_path_is_not_deleted(tmp_path):
    """Fail closed instead of deleting whatever happens to sit at the path."""
    path = tmp_path / "proxy.sock"
    path.write_text("keep me\n", encoding="utf-8")
    with pytest.raises(OSError):
        SandboxProxy(path, allow=["allowed.test"]).start()
    assert path.read_text(encoding="utf-8") == "keep me\n"


def test_concurrent_connections_are_served_in_parallel(proxy):
    """Each connection gets a thread: one slow tunnel must not stall the rest."""
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.4)
            body = b"SLOW-OK\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            with lock:
                state["active"] -= 1

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_address[1])
    try:
        results: list[bytes] = []

        def one() -> None:
            results.append(
                _raw(
                    proxy.socket_path,
                    (
                        f"GET http://allowed.test:{port}/ HTTP/1.1\r\n"
                        "Host: allowed.test\r\n\r\n"
                    ).encode(),
                )
            )

        workers = [threading.Thread(target=one) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        assert len(results) == 2
        assert all(b"SLOW-OK" in result for result in results)
        assert state["peak"] >= 2, state
    finally:
        server.shutdown()
        server.server_close()
