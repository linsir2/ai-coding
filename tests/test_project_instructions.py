"""TDD (M3-2): ProjectInstructionsLoader — CLAUDE.md / AGENTS.md with truncation."""

from ai_coding.core.project_instructions import ProjectInstructionsLoader


def test_missing_returns_empty(tmp_path):
    loader = ProjectInstructionsLoader(tmp_path)
    assert loader.load() == ""
    assert loader.find_path() is None


def test_loads_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Rules\nBe concise.", encoding="utf-8")
    loader = ProjectInstructionsLoader(tmp_path)
    assert loader.load() == "# Rules\nBe concise."


def test_agends_md_precedence_claude_first(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("claude-body", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents-body", encoding="utf-8")
    loader = ProjectInstructionsLoader(tmp_path)
    assert loader.load() == "claude-body"
    assert loader.find_path().name == "CLAUDE.md"


def test_agends_fallback_when_claude_absent(tmp_path):
    (tmp_path / "AGENTS.md").write_text("agents-body", encoding="utf-8")
    loader = ProjectInstructionsLoader(tmp_path)
    assert loader.load() == "agents-body"


def test_truncation_at_64kb(tmp_path):
    body = "a" * 200_000  # far over the default cap
    (tmp_path / "CLAUDE.md").write_text(body, encoding="utf-8")
    loader = ProjectInstructionsLoader(tmp_path, max_bytes=1024)
    text = loader.load()
    assert text.endswith("[project instructions truncated]")
    assert len(text.encode("utf-8")) <= 1024 + 64  # slight slack for the notice


def test_ignores_directories_with_same_name(tmp_path):
    (tmp_path / "CLAUDE.md").mkdir()
    loader = ProjectInstructionsLoader(tmp_path)
    assert loader.load() == ""


def test_read_error_returns_empty(tmp_path, monkeypatch):
    # OSError during read_text is swallowed -> "" .
    # (pathlib methods are immutable, so we patch module-level Path with a fake.)
    import ai_coding.core.project_instructions as pi

    class _FakePath:
        def __init__(self, *_: object) -> None:
            self.name = "CLAUDE.md"

        def is_file(self) -> bool:
            return True

        def read_text(self, *_: object, **__: object) -> str:
            raise OSError("denied")

        def __truediv__(self, other: object) -> "_FakePath":
            return self

    monkeypatch.setattr(pi, "Path", _FakePath)
    loader = pi.ProjectInstructionsLoader(tmp_path)
    assert loader.load() == ""
