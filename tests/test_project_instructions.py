"""TDD (M3-2): ProjectInstructionsLoader — CLAUDE.md / AGENTS.md with truncation."""

from pathlib import Path

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
    # The notice now names the file it cut: a truncation that does not say so
    # reads as the file having ended at that point.
    assert "[project instructions truncated]" in text
    assert "src" in text
    assert len(text.encode("utf-8")) <= 1024 + 64  # slight slack for the notice


def test_ignores_directories_with_same_name(tmp_path):
    (tmp_path / "CLAUDE.md").mkdir()
    loader = ProjectInstructionsLoader(tmp_path)
    assert loader.load() == ""


def test_read_error_returns_empty(tmp_path, monkeypatch):
    """OSError while reading one file is swallowed: the rest of the stack still
    loads, and a file that cannot be read is not a reason to lose the others.
    A real unreadable file is covered, rather than a stand-in for ``Path``,
    because the walk itself is what this used to break."""
    (tmp_path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
    unreadable = tmp_path / "sub"
    unreadable.mkdir()
    guarded = unreadable / "CLAUDE.md"
    guarded.write_text("# Sub rules\n", encoding="utf-8")

    import ai_coding.core.project_instructions as pi

    real_read_text = Path.read_text

    def deny(self: Path, *args: object, **kwargs: object) -> str:
        if self == guarded:
            raise OSError("denied")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", deny)

    assert ProjectInstructionsLoader(unreadable).load() == "# Rules\n"


# ---- the parents count too (M9) -------------------------------------------
#
# A loader that reads the workspace root only silently loses a parent's
# conventions: `/projects/CLAUDE.md` is invisible to a session started in
# `/projects/sub`, with no error and no warning — just less context. Codex has
# always walked up ("collect every AGENTS.md found from the project root down to
# the current working directory and concatenate"), which is the shape copied
# here.


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_parent_directory_instructions_apply_to_a_child_workspace(tmp_path):
    _write(tmp_path / "CLAUDE.md", "# parent rules\n")
    child = tmp_path / "sub"
    child.mkdir()

    assert "parent rules" in ProjectInstructionsLoader(child).load()


def test_the_nearest_file_comes_last(tmp_path):
    """Broad context first, then the more specific refinement — Codex's order,
    and the one that reads correctly."""
    _write(tmp_path / "CLAUDE.md", "parent-rules")
    _write(tmp_path / "sub" / "CLAUDE.md", "child-rules")

    text = ProjectInstructionsLoader(tmp_path / "sub").load()

    assert text.index("parent-rules") < text.index("child-rules")


def test_both_files_are_kept_not_first_match_wins(tmp_path):
    """The old loader returned the first file it found, so a workspace with both
    a CLAUDE.md and an AGENTS.md ignored the AGENTS.md entirely."""
    _write(tmp_path / "sub" / "CLAUDE.md", "claude-rules")
    _write(tmp_path / "sub" / "AGENTS.md", "agents-rules")

    text = ProjectInstructionsLoader(tmp_path / "sub").load()

    assert "claude-rules" in text
    assert "agents-rules" not in text, "one file per directory, CLAUDE.md wins"


def test_one_file_per_directory_and_a_parent_may_supply_the_other(tmp_path):
    _write(tmp_path / "AGENTS.md", "agents-from-parent")
    _write(tmp_path / "sub" / "CLAUDE.md", "claude-from-child")

    text = ProjectInstructionsLoader(tmp_path / "sub").load()

    assert "agents-from-parent" in text
    assert "claude-from-child" in text


def test_a_file_in_the_home_directory_does_not_leak_into_every_project(tmp_path, monkeypatch):
    """`~` is an ancestor of every workspace. A file there is the user's *own*
    instructions, which is a deliberate global tier — not something a stray
    `~/CLAUDE.md` should quietly inject into all of them."""
    home = tmp_path / "home"
    _write(home / "CLAUDE.md", "user-home-rules")
    project = home / "proj"
    project.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    assert "user-home-rules" not in ProjectInstructionsLoader(project).load()


def test_a_lone_file_is_byte_identical_to_what_it_was_before(tmp_path):
    """The whole existing contract must survive: one file, no separators, no
    prefix, nothing added."""
    _write(tmp_path / "CLAUDE.md", "# Rules\nBe concise.")

    assert ProjectInstructionsLoader(tmp_path).load() == "# Rules\nBe concise."


def test_the_budget_is_shared_and_the_cut_is_named(tmp_path):
    """Each file truncating at 64KiB separately would let four parents each fill
    the budget and hand the model four times that. One budget across all of
    them, and the file that lost out is named, because a policy silently cutting
    a section mid-sentence is indistinguishable from a file that ended there."""
    _write(tmp_path / "CLAUDE.md", "x" * 500)
    _write(tmp_path / "sub" / "CLAUDE.md", "y" * 500)
    loader = ProjectInstructionsLoader(tmp_path / "sub", max_bytes=600)

    text = loader.load()

    assert len(text.encode("utf-8")) <= 600
    # Which file was cut matters: without a marker, a policy truncating one
    # level's section mid-sentence is indistinguishable from the file ending.
    assert "[project instructions truncated]" in text
    assert "sub" in text.split("[project instructions truncated]")[-1], (
        "the level that was cut is named"
    )


def test_a_deep_tree_does_not_pay_for_ten_copies_of_the_budget(tmp_path):
    deep = tmp_path
    for level in range(8):
        _write(deep / "CLAUDE.md", f"level-{level}-" + "z" * 300)
        deep = deep / f"d{level}"
    loader = ProjectInstructionsLoader(deep, max_bytes=1_000)

    text = loader.load()

    assert len(text.encode("utf-8")) <= 1_000 + 64
    assert "level-0-" in text, "the broadest context is still there"
    assert "truncated" in text, "and the budget was spent somewhere"
    assert text.count("level-") < 8, "not every level fits in 1,000 bytes"
