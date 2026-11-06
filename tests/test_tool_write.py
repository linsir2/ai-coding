"""TDD (M2-1): write tool (sandbox + stale-read)."""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.write_tool import WriteTool


@pytest.fixture
def setup(tmp_path):
    sb = Sandbox(tmp_path)
    fst = FileStateTracker()
    tool = WriteTool(sb, fst)
    return tmp_path, sb, fst, tool


def test_write_description_states_one_file_per_call_and_serial_approval(setup):
    _, _, _, tool = setup
    description = tool.description
    assert "Each call writes exactly one file" in description
    assert "executed sequentially" in description
    assert "never in parallel" in description
    assert "separate approval" in description


@pytest.mark.asyncio
async def test_write_creates_file(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "new.txt", "content": "hello"})
    assert result.success is True
    assert (tmp_path / "new.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_a_mid_file_slice_is_refused_as_an_overwrite(setup):
    """2026-11 real run: a 217-line module became a 31-line fragment.

    The model passed the route section alone with ``mode=overwrite``; that is
    an exact slice of the old file, not a rewrite, so the tool refuses it.
    """
    tmp_path, _, fst, tool = setup
    old = "import os\n\ndef a():\n    pass\n\ndef b():\n    pass\n"
    target = tmp_path / "m.py"
    target.write_text(old, encoding="utf-8")
    fst.record(target)

    result = await tool.execute({"path": "m.py", "content": "def b():\n    pass\n"})

    assert result.success is False
    assert result.error == "FAILED_PRECONDITION"
    assert result.detail == "overwrite_would_truncate"
    assert "edit" in result.output
    assert target.read_text(encoding="utf-8") == old, "the file must be untouched"


@pytest.mark.asyncio
async def test_a_legitimate_shorter_rewrite_still_passes(setup):
    tmp_path, _, fst, tool = setup
    target = tmp_path / "m.py"
    target.write_text("import os\n\ndef a():\n    pass\n\ndef b():\n    pass\n",
                     encoding="utf-8")
    fst.record(target)

    result = await tool.execute(
        {"path": "m.py", "content": "def a():\n    return 1\n"}
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "def a():\n    return 1\n"


@pytest.mark.asyncio
async def test_write_overwrites(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("old")
    fst.record(tmp_path / "f.txt")  # simulate prior read
    result = await tool.execute({"path": "f.txt", "content": "new"})
    assert result.success is True
    assert (tmp_path / "f.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_write_creates_parent_dirs(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "a/b/c.txt", "content": "x"})
    assert result.success is True
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "x"


@pytest.mark.asyncio
async def test_write_outside_sandbox_denied(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "/tmp/evil.txt", "content": "x"})
    assert result.success is False
    assert "workspace" in result.output.lower() or "sandbox" in result.output.lower()


@pytest.mark.asyncio
async def test_write_without_prior_read_denied(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("existing")
    # never read → stale check fails
    result = await tool.execute({"path": "f.txt", "content": "new"})
    assert result.success is False
    assert result.detail == "must_read_first"
    out = result.output.lower()
    assert "read" in out or "stale" in out or "never" in out
    assert "edit" in out, "the hint must name the targeted alternative"


@pytest.mark.asyncio
async def test_write_reports_stale_when_the_file_changed_during_the_call(
    setup, monkeypatch
):
    """The check-to-write window is closed by a second version comparison."""
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    p.write_text("v1")
    fst.record(p)
    from ai_coding.infra import file_utils

    monkeypatch.setattr(file_utils, "file_version", lambda _path: "changed-late")

    result = await tool.execute({"path": "f.txt", "content": "new"})

    assert result.success is False
    assert result.detail == "stale_read"
    assert p.read_text() == "v1"
    assert list(tmp_path.glob(".f.txt.*.tmp")) == []


@pytest.mark.asyncio
async def test_write_stale_after_external_modification(setup):
    tmp_path, sb, fst, tool = setup
    p = tmp_path / "f.txt"
    p.write_text("v1")
    fst.record(p)  # snapshot of v1
    p.write_text("v2 externally changed")  # external modification
    result = await tool.execute({"path": "f.txt", "content": "overwrite"})
    assert result.success is False
    assert result.detail == "stale_read", "the card must say why, not a bare '?'"
    assert "stale" in result.output.lower() or "modified" in result.output.lower()
    assert "older version" in result.output, "name why the context copy is useless"
