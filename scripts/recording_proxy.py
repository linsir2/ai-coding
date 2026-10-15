#!/usr/bin/env python3
"""Recording proxy for the model endpoint — takes over the live traffic.

Sits between the app and the provider: forwards every request verbatim, streams
the response back untouched, and appends one JSONL line per exchange:

    {"seq": 1, "path": "...", "request": {...}, "status": 200,
     "events": 42, "content_chars": 380, "aborted": false}

Optional fault injection for live acceptance:

    --cut-first  N   on the *first* request, cut the connection once N SSE
                     events with content have gone out (reasoning-only events
                     do not count) — a real mid-stream break for the B
                     continuation path (13 §2.5).  Later requests pass through
                     untouched.

Usage (the app's config points its model ``base_url`` at the listen address):

    .venv/bin/python scripts/recording_proxy.py \
        --upstream https://dashscope.aliyuncs.com/compatible-mode/v1 \
        --port 8777 --out /tmp/opencode/wire.jsonl [--cut-first 5]

The upstream path suffix must match the client's ``base_url`` suffix (the proxy
forwards the path verbatim after the origin).
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

_CHUNK = 64 * 1024


class _Recorder:
    def __init__(self, out_path: Path) -> None:
        self.path = out_path
        self._lock = threading.Lock()
        self.seq = 0

    def append(self, entry: dict) -> None:
        with self._lock:
            self.seq += 1
            entry["seq"] = self.seq
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _content_chars(raw_event: bytes) -> int:
    """Characters of user-visible content in one SSE event (best effort)."""
    if not raw_event.startswith(b"data: "):
        return 0
    payload = raw_event[len(b"data: ") :].strip()
    if payload == b"[DONE]":
        return 0
    try:
        data = json.loads(payload)
        choices = data.get("choices") or []
        if not choices:
            return 0
        delta = choices[0].get("delta") or {}
        return len(str(delta.get("content") or ""))
    except (ValueError, AttributeError):
        return 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    # wired by serve()
    upstream_origin: str = ""
    recorder: _Recorder | None = None
    cut_first: int = 0
    _first_taken = False

    def do_POST(self) -> None:  # noqa: N802 -- http.server's spelling
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            parsed: object = json.loads(raw or b"{}")
        except ValueError:
            parsed = {"<unparsable>": raw[:200].decode("utf-8", "replace")}

        klass = type(self)
        with klass._cut_lock:
            first = not klass._first_taken
            if first:
                klass._first_taken = True

        target = self.upstream_origin + self.path
        forwarded = {
            name: self.headers[name]
            for name in ("authorization", "content-type", "accept")
            if self.headers.get(name)
        }
        entry: dict = {
            "path": self.path,
            "request": parsed,
            "status": 0,
            "events": 0,
            "content_chars": 0,
            "aborted": False,
        }
        cut_after = self.cut_first if (first and self.cut_first) else 0
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=15.0)
            ) as client:
                with client.stream(
                    "POST", target, content=raw, headers=forwarded
                ) as response:
                    entry["status"] = response.status_code
                    self.send_response(response.status_code)
                    self.send_header(
                        "Content-Type",
                        response.headers.get("content-type", "application/json"),
                    )
                    self.end_headers()

                    buffer = b""
                    allowed = cut_after
                    for chunk in response.iter_raw():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        buffer += chunk
                        while b"\n\n" in buffer:
                            event, buffer = buffer.split(b"\n\n", 1)
                            entry["events"] += 1
                            entry["content_chars"] += _content_chars(event)
                            if allowed and entry["content_chars"] > 0:
                                allowed -= 1
                                if allowed <= 0:
                                    # A real mid-stream break: reset, not EOF.
                                    entry["aborted"] = True
                                    raise _CutStream()
        except _CutStream:
            self.connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            self.connection.close()
        except (BrokenPipeError, ConnectionResetError):
            entry["aborted"] = True
        finally:
            entry["elapsed"] = round(time.monotonic() - started, 2)
            if self.recorder is not None:
                self.recorder.append(entry)

    def log_message(self, *args: object) -> None:  # keep the console readable
        return None


class _CutStream(Exception):
    pass


_Handler._cut_lock = threading.Lock()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        return None


def serve(upstream: str, port: int, out_path: Path, cut_first: int) -> _Server:
    """Build the proxy server (not yet serving; the caller starts the thread)."""
    origin = f"{urlsplit(upstream).scheme}://{urlsplit(upstream).netloc}"
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "upstream_origin": origin,
            "recorder": _Recorder(out_path),
            "cut_first": cut_first,
            "_first_taken": False,
        },
    )
    return _Server(("127.0.0.1", port), handler)


def start(server: _Server) -> None:
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="provider base_url to forward to")
    parser.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    parser.add_argument("--out", required=True, help="JSONL recording path")
    parser.add_argument(
        "--cut-first",
        type=int,
        default=0,
        metavar="N",
        help="cut the first request once N events with content have gone out",
    )
    args = parser.parse_args()

    server = serve(args.upstream, args.port, Path(args.out), args.cut_first)
    print(f"proxy on 127.0.0.1:{server.server_address[1]} -> {args.upstream}")
    print(f"recording to {args.out}  (cut-first={args.cut_first or 'off'})")
    start(server)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
