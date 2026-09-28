from ai_coding.ui.header import render_header


class _FakeTools:
    def list_names(self):
        return ["read", "write", "bash"]


class _FakeLoop:
    def __init__(self):
        self.tools = _FakeTools()
        self.memory_status = lambda: {"total": 42}
        self.settings = type("S", (), {"default_model": "DeepSeek-V4"})()
        self.workspace = "~/projects/atlas"


def test_header_contains_ascii():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "██" in flat


def test_header_contains_workspace():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "~/projects/atlas" in flat


def test_header_contains_model():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "DeepSeek-V4" in flat


def test_header_contains_tools_count():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "3" in flat


def test_header_contains_memory_count():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "42" in flat


def test_header_contains_checkmarks():
    text = render_header(_FakeLoop())
    flat = "".join(t for _, t in text)
    assert "✓" in flat
