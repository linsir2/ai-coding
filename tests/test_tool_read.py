"""TDD (M2-1): read tool (with sandbox + file state tracker)."""

import pytest

from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.read_tool import ReadTool


@pytest.fixture
def setup(tmp_path):
    sb = Sandbox(tmp_path)
    fst = FileStateTracker()
    tool = ReadTool(sb, fst)
    return tmp_path, sb, fst, tool


@pytest.mark.asyncio
async def test_read_basic(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("hello\nworld\n")
    result = await tool.execute({"path": "f.txt"})
    assert result.success is True
    assert "hello" in result.output
    assert "world" in result.output


@pytest.mark.asyncio
async def test_read_line_numbers(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("a\nb\nc\n")
    result = await tool.execute({"path": "f.txt"})
    assert "     1  a" in result.output or "1 | a" in result.output or "1:" in result.output


@pytest.mark.asyncio
async def test_read_offset_limit(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("\n".join(f"line{i}" for i in range(10)) + "\n")
    result = await tool.execute({"path": "f.txt", "offset": 3, "limit": 2})
    assert result.success is True
    assert "line2" not in result.output  # 0-indexed offset 3 = line 4
    assert "line3" in result.output
    assert "line4" in result.output
    assert "line5" not in result.output


@pytest.mark.asyncio
async def test_read_nonexistent_fails(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "nope.txt"})
    assert result.success is False
    assert "not found" in result.output.lower() or "no such" in result.output.lower()


@pytest.mark.asyncio
async def test_read_records_snapshot(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("hi")
    await tool.execute({"path": "f.txt"})
    assert fst.check(tmp_path / "f.txt").name == "CLEAN"


@pytest.mark.asyncio
async def test_read_outside_sandbox_denied(setup):
    tmp_path, sb, fst, tool = setup
    result = await tool.execute({"path": "/etc/passwd"})
    assert result.success is False
    assert "workspace" in result.output.lower() or "sandbox" in result.output.lower()


# ---- R20/RC-3: the per-call line cap --------------------------------------


def _long_file(tmp_path, lines: int):
    (tmp_path / "long.py").write_text(
        "".join(f"# line {i}\n" for i in range(1, lines + 1))
    )
    return "long.py"


@pytest.mark.asyncio
async def test_a_long_file_is_paged_with_a_continue_hint(setup):
    """C1: RC-3.  One whole-file read of ``agent_loop.py`` was 174,113
    characters (≈43k tokens) against a 12k-token tail budget, so it either
    overflowed or got summarised to a 2,000-character excerpt."""
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 3081)
    result = await tool.execute({"path": "long.py"})
    assert result.success is True
    assert "# line 500" in result.output
    assert "# line 501" not in result.output
    assert "offset=500" in result.output


@pytest.mark.asyncio
async def test_a_file_under_the_cap_is_returned_whole(setup):
    """C2: the 441-line ``present_plan_tool.py`` is exactly the file T2 read
    eleven times; at this size one call must be enough."""
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 441)
    result = await tool.execute({"path": "long.py"})
    assert "# line 441" in result.output
    assert "继续" not in result.output
    assert "showing lines" not in result.output


@pytest.mark.asyncio
async def test_an_explicit_limit_above_the_cap_is_clamped(setup):
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 3081)
    result = await tool.execute({"path": "long.py", "limit": 900})
    assert "# line 500" in result.output
    assert "# line 501" not in result.output
    assert "offset=500" in result.output


@pytest.mark.asyncio
async def test_offset_plus_limit_past_the_end_does_not_record_a_bad_range(setup):
    """C4: the pre-existing bug.  ``offset + limit`` ran past the end, so the
    ledger would have recorded ``101-150`` for a 117-line file and ``covers``
    would answer a question about lines that do not exist."""
    tmp_path, sb, fst, tool = setup
    seen: list[tuple[str, int, int, int]] = []
    _long_file(tmp_path, 117)
    result = await tool.execute(
        {"path": "long.py", "offset": 100, "limit": 50},
        _Context(note_read=lambda *row: seen.append(row)),
    )
    assert result.success is True
    assert "# line 101" in result.output
    assert "# line 117" in result.output
    assert "# line 118" not in result.output
    assert "showing lines 101-117 of 117" in result.output
    assert seen == [("long.py", 101, 117, 117)]


@pytest.mark.asyncio
async def test_an_explicit_limit_below_the_end_keeps_its_suffix(setup):
    """C5: the old branch printed this; the rewrite must not drop it silently."""
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 100)
    result = await tool.execute({"path": "long.py", "limit": 50})
    assert "showing lines 1-50 of 100" in result.output


@pytest.mark.asyncio
async def test_a_zero_limit_says_so_instead_of_printing_a_contradiction(setup):
    """``limit=0`` used to print ``showing lines 1-0 of 100`` -- a range whose
    start is past its end, which the model would have to guess at."""
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 100)
    result = await tool.execute({"path": "long.py", "limit": 0})
    assert "没有返回任何行" in result.output
    assert "showing lines" not in result.output


@pytest.mark.asyncio
async def test_an_offset_past_the_end_says_so_too(setup):
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 100)
    result = await tool.execute({"path": "long.py", "offset": 300})
    assert "没有返回任何行" in result.output
    assert "showing lines" not in result.output


@pytest.mark.asyncio
async def test_a_read_reports_the_range_it_returned(setup):
    """The ledger records the *returned* page, not the file."""
    tmp_path, sb, fst, tool = setup
    seen: list[tuple[str, int, int, int]] = []
    _long_file(tmp_path, 3081)
    await tool.execute(
        {"path": "long.py"},
        _Context(note_read=lambda *row: seen.append(row)),
    )
    assert seen == [("long.py", 1, 500, 3081)]


@pytest.mark.asyncio
async def test_a_failed_read_records_nothing(setup):
    tmp_path, sb, fst, tool = setup
    seen: list[tuple[str, int, int, int]] = []
    result = await tool.execute(
        {"path": "nope.py"},
        _Context(note_read=lambda *row: seen.append(row)),
    )
    assert result.success is False
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"path": "sub"},                       # a directory, not a file
        {"path": "big.bin"},                   # over the 10 MB cap
        {"path": "/etc/passwd"},               # outside the sandbox
        {"path": "../escape.py"},              # workspace escape
    ],
)
async def test_every_failed_read_records_nothing(setup, params):
    """The ledger must describe what reached the context.  A failed call reached
    nothing, so recording one would invent evidence the model never had."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / "sub").mkdir()
    (tmp_path / "big.bin").write_bytes(b"x" * (11 * 1024 * 1024))
    seen: list[tuple[str, int, int, int]] = []
    result = await tool.execute(
        params, _Context(note_read=lambda *row: seen.append(row))
    )
    assert result.success is False
    assert seen == []


@pytest.mark.asyncio
async def test_a_credential_read_leaves_no_ledger_trace(setup):
    """A credential file is the one read the model must never cite by line, and
    its name has no business in the session file or in the block that is
    re-read to the model on every iteration."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / ".env").write_text("TOKEN=secret\n")
    seen: list[tuple[str, int, int, int]] = []
    result = await tool.execute(
        {"path": ".env"},
        _Context(
            note_read=lambda *row: seen.append(row),
            credential_approved=True,
        ),
    )
    assert result.success is True
    assert seen == []


class _Context:
    """A ``ToolContext`` with a capturing ``note_read``.

    ``resolve_ctx`` only checks for ``None``, so the wrapper has to delegate the
    rest (``credential_approved`` and friends) to the real object.
    """

    def __init__(self, note_read=None, credential_approved=False):
        from ai_coding.tools.base import ToolContext

        self._inner = ToolContext(
            note_read=note_read, credential_approved=credential_approved
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)
