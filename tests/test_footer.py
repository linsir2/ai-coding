import time

from ai_coding.ui.footer import render_footer


class _FakeLoop:
    def __init__(self):
        self.last_outcome = None
        self.settings = type("S", (), {"max_context_tokens": 48000})()


def test_footer_renders_context_percentage():
    loop = _FakeLoop()
    loop.last_outcome = type("O", (), {"usage": {"total_tokens": 24000}})()
    text = render_footer(loop, time.monotonic())
    flat = "".join(t for _, t in text)
    assert "50%" in flat


def test_footer_renders_timer():
    loop = _FakeLoop()
    start = time.monotonic() - 5.0
    text = render_footer(loop, start)
    flat = "".join(t for _, t in text)
    assert "00:05" in flat


def test_footer_no_usage():
    loop = _FakeLoop()
    text = render_footer(loop, time.monotonic())
    flat = "".join(t for _, t in text)
    assert "0%" in flat
