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
async def test_a_one_line_read_names_the_offset_it_used(setup):
    """2026-11 real run: the model wanted line 176, passed offset=176, got 177.

    The header is 1-based and the parameter is 0-based; a one-line response
    must say both so the same call is not repeated seven times.
    """
    tmp_path, sb, fst, tool = setup
    _long_file(tmp_path, 300)
    result = await tool.execute({"path": "long.py", "offset": 100, "limit": 1})
    assert result.success is True
    assert "showing lines 101-101 of 300" in result.output
    assert "offset=100 is 0-based" in result.output
    assert "pass offset=99" in result.output


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
        _Context(note_read=lambda *row, **kwargs: seen.append(row[:4])),
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
        _Context(note_read=lambda *row, **kwargs: seen.append(row[:4])),
    )
    assert seen == [("long.py", 1, 500, 3081)]


@pytest.mark.asyncio
async def test_a_failed_read_records_nothing(setup):
    tmp_path, sb, fst, tool = setup
    seen: list[tuple[str, int, int, int]] = []
    result = await tool.execute(
        {"path": "nope.py"},
        _Context(note_read=lambda *row, **kwargs: seen.append(row[:4])),
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
        params, _Context(note_read=lambda *row, **kwargs: seen.append(row[:4]))
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
            note_read=lambda *row, **kwargs: seen.append(row[:4]),
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


@pytest.mark.asyncio
async def test_a_read_records_the_file_version(setup):
    tmp_path, _sb, _fst, tool = setup
    (tmp_path / "f.txt").write_text("hello\n")
    seen: list[tuple] = []
    await tool.execute(
        {"path": "f.txt"},
        _Context(note_read=lambda *row, **kwargs: seen.append(row)),
    )
    assert len(seen[0]) == 5
    assert seen[0][4]


# ---- R2-13: a repeat read answers with a pointer, not the same bytes -----


@pytest.mark.asyncio
async def test_a_fully_covered_reread_returns_a_pointer(setup):
    """A model that reads its own growing trace re-sent the same lines every
    step (22 retrievals, 262k prompt tokens, one 300s timeout).  Coverage comes
    from the loop, so the checks below install a fake marker source.

    R3-1: the page must be larger than ``_SMALL_READ_LINES`` -- a small file is
    deliberately answered with the body instead of a pointer."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / "trace.jsonl").write_text(
        "\n".join(f'{{"line":{index}}}' for index in range(1, 101)) + "\n"
    )
    key = sb.key(tmp_path / "trace.jsonl")
    marks = [(1, 100, "v1")]

    from ai_coding.tools.base import ToolContext

    result = await tool.execute(
        {"path": "trace.jsonl"},
        ToolContext(workspace=str(tmp_path), read_marks=lambda _key: tuple(marks)),
    )

    assert result.success is True
    assert "未返回正文" in result.output
    assert '"line":1' not in result.output
    assert result.data is not None and result.data["repeated_read"] is True
    assert key  # the key is what the loop's marker source is queried with


@pytest.mark.asyncio
async def test_the_second_pointer_returns_the_body(setup):
    """R3-1: the pointer tells the model to ask again; that promise must hold.

    Measured live: 18 identical reads of one 22-line file, each answered with
    the same pointer, because the advice ("use limit=N") never worked.
    """
    tmp_path, sb, fst, tool = setup
    (tmp_path / "trace.jsonl").write_text(
        "\n".join(f'{{"line":{index}}}' for index in range(1, 101)) + "\n"
    )
    marks = [(1, 100, "v1")]
    seen: list[int] = []

    def _count(key, start, end, version):
        seen.append(1)
        return len(seen)

    from ai_coding.tools.base import ToolContext

    ctx = ToolContext(
        workspace=str(tmp_path),
        read_marks=lambda _key: tuple(marks),
        count_repeat_read=_count,
    )
    first = await tool.execute({"path": "trace.jsonl"}, ctx)
    second = await tool.execute({"path": "trace.jsonl"}, ctx)

    assert "未返回正文" in first.output
    assert "未返回正文" not in second.output
    assert '"line":1' in second.output


@pytest.mark.asyncio
async def test_a_small_file_is_never_answered_with_a_pointer(setup):
    """R3-1: pointing at a 22-line config costs more than re-sending it."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / "package.json").write_text(
        "\n".join(f'"key{index}": {index},' for index in range(1, 21)) + "\n"
    )
    marks = [(1, 20, "v1")]

    from ai_coding.tools.base import ToolContext

    result = await tool.execute(
        {"path": "package.json"},
        ToolContext(workspace=str(tmp_path), read_marks=lambda _key: tuple(marks)),
    )

    assert "未返回正文" not in result.output
    assert '"key1"' in result.output


@pytest.mark.asyncio
async def test_a_short_but_wide_page_is_not_exempt(setup):
    """R4-4: "small" is lines **and** characters.  Twenty lines of minified
    output is not the 450-byte config the exemption was written for."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / "bundle.js").write_text(
        "\n".join("x" * 300 for _ in range(20)) + "\n"
    )
    marks = [(1, 20, "v1")]

    from ai_coding.tools.base import ToolContext

    result = await tool.execute(
        {"path": "bundle.js"},
        ToolContext(workspace=str(tmp_path), read_marks=lambda _key: tuple(marks)),
    )

    assert "未返回正文" in result.output


@pytest.mark.asyncio
async def test_a_59_line_file_is_deduplicated(setup):
    """R4-4: the live loop re-read a 59-line ``App.tsx`` 47 times because the
    old 60-line threshold exempted it."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / "App.tsx").write_text(
        "\n".join(f"const row{index} = {index};" for index in range(1, 60)) + "\n"
    )
    marks = [(1, 59, "v1")]

    from ai_coding.tools.base import ToolContext

    result = await tool.execute(
        {"path": "App.tsx"},
        ToolContext(workspace=str(tmp_path), read_marks=lambda _key: tuple(marks)),
    )

    assert "未返回正文" in result.output


@pytest.mark.asyncio
async def test_a_repeat_read_returns_only_the_gap(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "trace.jsonl").write_text(
        "\n".join(f'{{"line":{index}}}' for index in range(1, 101)) + "\n"
    )
    marks = [(1, 95, "v1")]

    from ai_coding.tools.base import ToolContext

    result = await tool.execute(
        {"path": "trace.jsonl"},
        ToolContext(workspace=str(tmp_path), read_marks=lambda _key: tuple(marks)),
    )

    assert result.success is True
    assert '"line":96' in result.output
    assert '"line":97' in result.output
    assert '"line":5' not in result.output
    assert "只返回未读过的行" in result.output


@pytest.mark.asyncio
async def test_a_changed_file_still_reports_the_reason(setup):
    tmp_path, sb, fst, tool = setup
    (tmp_path / "g.txt").write_text(
        "\n".join(f"content{index}" for index in range(1, 101)) + "\n"
    )
    marks = [(1, 100, "old-version")]

    from ai_coding.tools.base import ToolContext

    result = await tool.execute(
        {"path": "g.txt"},
        ToolContext(workspace=str(tmp_path), read_marks=lambda _key: tuple(marks)),
    )

    assert result.success is True
    assert "文件已变化" in result.output
    assert result.data is not None and result.data["changed"] is True


@pytest.mark.asyncio
async def test_without_a_marker_source_every_read_returns_the_body(setup):
    """Direct tool calls (and sub-agents without a ledger) must keep working."""
    tmp_path, sb, fst, tool = setup
    (tmp_path / "f.txt").write_text("hello\nworld\n")

    first = await tool.execute({"path": "f.txt"})
    second = await tool.execute({"path": "f.txt"})

    assert "hello" in first.output and "hello" in second.output
