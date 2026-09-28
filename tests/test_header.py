"""Header: welcome box + status indicators."""

from ai_coding.ui.header import render_header


class _FakeTools:
    def list_names(self):
        return ["read", "write", "bash"]


class _FakeLoop:
    def __init__(self):
        self.tools = _FakeTools()
        self.memory_status = lambda: {"total": 42}
        self.engine = type("E", (), {
            "model_config": type("MC", (), {"name": "DeepSeek-V4"})(),
        })()
        self.workspace = "~/projects/atlas"


def test_renders_welcome_box():
    text = "".join(t for _, t in render_header(_FakeLoop()))
    assert "Welcome" in text
    assert "╭" in text
    assert "╰" in text


def test_renders_status_indicators():
    text = "".join(t for _, t in render_header(_FakeLoop()))
    assert "3 tools connected" in text
    assert "memory loaded" in text


def test_renders_model_name():
    text = "".join(t for _, t in render_header(_FakeLoop()))
    assert "DeepSeek-V4" in text


def test_renders_cwd():
    text = "".join(t for _, t in render_header(_FakeLoop()))
    assert "~/projects/atlas" in text


def test_no_ascii_art():
    text = "".join(t for _, t in render_header(_FakeLoop()))
    assert "██" not in text
