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


@pytest.mark.asyncio
async def test_dropping_the_tail_of_a_file_is_a_legitimate_rewrite(setup):
    """A prefix body means "remove trailing lines", not an accidental splice.

    Only a strict *interior* slice is refused (see the mid-file case); the old
    offset-0 rejection mistook this ordinary rewrite for truncation.
    """
    tmp_path, _, fst, tool = setup
    old = "a\nb\nc\n"
    target = tmp_path / "m.py"
    target.write_text(old, encoding="utf-8")
    fst.record(target)

    result = await tool.execute({"path": "m.py", "content": "a\n"})

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "a\n"


@pytest.mark.asyncio
async def test_resubmitting_the_whole_file_is_not_a_slice(setup):
    """A full rewrite -- even with the trailing newline trimmed -- still passes."""
    tmp_path, _, fst, tool = setup
    old = "a\nb\nc\n"
    target = tmp_path / "m.py"
    target.write_text(old, encoding="utf-8")
    fst.record(target)

    result = await tool.execute({"path": "m.py", "content": old})

    assert result.success is True
    assert target.read_text(encoding="utf-8") == old


@pytest.mark.asyncio
async def test_append_rechecks_the_base_version_before_replacing(setup, monkeypatch):
    """X-01b: append is a new version, not a blind ``ab`` write."""
    tmp_path, _, fst, tool = setup
    p = tmp_path / "f.txt"
    p.write_text("v1")
    fst.record(p)
    from ai_coding.tools import write_tool as write_tool_module

    # ``before`` is captured from this, while ``write_text_atomic`` compares the
    # real version -- exactly the check-to-write race the old append ignored.
    monkeypatch.setattr(
        write_tool_module, "file_version", lambda _path: "changed-late"
    )

    result = await tool.execute(
        {"path": "f.txt", "content": "+more", "mode": "append"}
    )

    assert result.success is False
    assert result.detail == "stale_read"
    assert p.read_text() == "v1"
    assert list(tmp_path.glob(".f.txt.*.tmp")) == []


@pytest.mark.asyncio
async def test_append_preserves_crlf_and_mode(setup):
    tmp_path, _, fst, tool = setup
    p = tmp_path / "f.txt"
    p.write_bytes(b"one\r\ntwo\r\n")
    p.chmod(0o600)
    fst.record(p)

    result = await tool.execute(
        {"path": "f.txt", "content": "three\r\n", "mode": "append"}
    )

    assert result.success is True
    assert p.read_bytes() == b"one\r\ntwo\r\nthree\r\n"
    assert p.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_append_refuses_a_non_utf8_file_without_touching_it(setup):
    """A lossy decode would silently rewrite the old bytes; refusing is honest."""
    tmp_path, _, fst, tool = setup
    p = tmp_path / "f.bin"
    original = b"\xff\xfeold bytes"
    p.write_bytes(original)
    fst.record(p)

    result = await tool.execute(
        {"path": "f.bin", "content": "more", "mode": "append"}
    )

    assert result.success is False
    assert result.detail == "write_error"
    assert "UTF-8" in result.output
    assert p.read_bytes() == original, "the refused call must not modify the file"


@pytest.mark.asyncio
async def test_append_on_a_large_file_does_not_rewrite_the_whole_file(setup):
    """Past the cap, append is an in-place write with fsync, not O(size) rebuild."""
    import os

    from ai_coding.tools.write_tool import _APPEND_REWRITE_MAX_BYTES

    tmp_path, _, fst, tool = setup
    p = tmp_path / "big.log"
    p.write_bytes(b"a" * (_APPEND_REWRITE_MAX_BYTES + 1))
    fst.record(p)
    before_size = p.stat().st_size

    result = await tool.execute(
        {"path": "big.log", "content": "tail", "mode": "append"}
    )

    assert result.success is True
    assert "large-file in-place append" in result.output
    assert p.stat().st_size == before_size + len("tail")
    with p.open("rb") as handle:
        handle.seek(-len("tail"), os.SEEK_END)
        assert handle.read() == b"tail"
