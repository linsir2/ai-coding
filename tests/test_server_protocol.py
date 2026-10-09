"""TDD (M0): wire protocol — encode/decode of the TUI JSON-RPC lines."""

from __future__ import annotations

import json

from ai_coding.server.protocol import (
    decode_line,
    encode_error,
    encode_notification,
    encode_request,
    encode_result,
)


def _loads(payload: bytes) -> dict:
    return json.loads(payload.decode("utf-8"))


class TestDecodeLine:
    def test_parses_a_request(self):
        line = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
        message = decode_line(line)
        assert message is not None
        assert message["method"] == "initialize"
        assert message["id"] == 1

    def test_parses_a_string_line(self):
        message = decode_line('{"id": 5, "result": {"ok": true}}')
        assert message == {"id": 5, "result": {"ok": True}}

    def test_garbage_returns_none(self):
        assert decode_line(b"{not json") is None
        assert decode_line(b"") is None
        assert decode_line(b"   ") is None

    def test_non_object_returns_none(self):
        assert decode_line(b"[1, 2, 3]") is None
        assert decode_line(b'"hello"') is None

    def test_invalid_utf8_returns_none(self):
        assert decode_line(b"\xff\xfe\x00") is None


class TestEncode:
    def test_encode_result_shape(self):
        payload = _loads(encode_result(3, {"ok": True}))
        assert payload == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}

    def test_encode_error_shape(self):
        payload = _loads(encode_error(4, -32601, "unknown method: x"))
        assert payload["id"] == 4
        assert payload["error"]["code"] == -32601
        assert "unknown method" in payload["error"]["message"]

    def test_encode_notification_has_no_id(self):
        payload = _loads(encode_notification("event", {"type": "turn.start"}))
        assert "id" not in payload
        assert payload["method"] == "event"
        assert payload["params"] == {"type": "turn.start"}

    def test_encode_request_has_id_and_method(self):
        payload = _loads(encode_request(7, "approval.request", {"tool": "bash"}))
        assert payload["id"] == 7
        assert payload["method"] == "approval.request"

    def test_every_payload_ends_with_newline(self):
        assert encode_result(1, None).endswith(b"\n")
        assert encode_notification("event").endswith(b"\n")
        assert encode_error(None, -32700, "parse").endswith(b"\n")
