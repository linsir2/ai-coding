"""R20 A: the read ledger — what this session has actually read, and where the
gaps are.

RC-2: the memory layer stored conclusions, not evidence.  The checkpoint says
"读取 ai_coding/tools/present_plan_tool.py 源码" with no line numbers, so after one
compaction the model cannot tell "I read this file" from "I never opened it" — and
re-read the same 441-line file 11 times at overlapping offsets.

These tests are the red phase: they are written before ``domain/read_ledger.py``
exists, so the module import fails and every row is red.
"""

from __future__ import annotations

import pytest

from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.read_ledger import (
    ReadMark,
    covers,
    extract_claims,
    merge_mark,
    missing_ranges,
    normalize,
    paths_in,
    render,
    same_file,
    unsupported_claims,
    unsupported_quotes,
)
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState

# ------------------------------------------------------- the tail block (A5/A6)


class _Compactor:
    """Rewrites the history, standing in for a real compaction."""

    def __init__(self, rewrite: bool = True) -> None:
        self.rewrite = rewrite
        self.calls = 0

    async def compress(self, history, *, deadline, on_summary=None, force=False, pinned=()):
        self.calls += 1
        if not self.rewrite:
            return history
        return [*history, ChatMessage(role="user", content="[压缩后的历史]", timestamp="")]


class _Memory:
    def block(self) -> tuple[str, list[str]]:
        return "", []


class _Reporter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def report_context(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return f"canonical-{len(self.calls)}"

    def tokens_spent(self) -> int:
        return 0


class _Settings:
    working_memory_chars = 1200
    auto_compact = True


def _assembler(*, rewrite: bool = True) -> tuple[RequestAssembler, _Reporter, _Compactor]:
    reporter = _Reporter()
    compactor = _Compactor(rewrite)

    async def _instructions(deadline, session=None):
        return None

    return (
        RequestAssembler(
            compactor=compactor,
            policy=TurnPolicy(TurnPolicyConfig()),
            memory=_Memory(),
            reporter=reporter,
            build_instructions=_instructions,
            settings=_Settings(),
            emit=lambda event: None,
            workspace="",
        ),
        reporter,
        compactor,
    )


@pytest.mark.asyncio
async def test_the_ledger_block_rides_the_request():
    """A5: the harness computes the block from session state every iteration, so
    it is present in the request regardless of what the history holds."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content="开始", timestamp="t"))
    session.note_read("a.py", 1, 500, 3081, workspace="")
    assembler, reporter, _compactor = _assembler()
    await assembler.assemble(
        session=session,
        task=TaskState(goal="长任务"),
        books=TurnBooks(),
        turn_number=1,
        hook_context="",
        deadline=None,
        index=1,
        previous_request=None,
    )
    blocks = reporter.calls[-1]["tail_blocks"]
    assert any(
        name == "read_ledger" and size > 0 for name, size in blocks
    ), f"the read-ledger block never reached the request: {blocks}"


@pytest.mark.asyncio
async def test_a_read_after_compaction_still_shows_in_the_ledger():
    """A6: compaction rewrites the history, not the ledger.  A block that lived
    in the history would be summarised away; this one is rebuilt from the
    session, so the model still knows where the gaps are."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content="开始", timestamp="t"))
    session.note_read("a.py", 1, 500, 3081, workspace="")
    assembler, reporter, compactor = _assembler(rewrite=True)
    await assembler.assemble(
        session=session,
        task=TaskState(goal="长任务"),
        books=TurnBooks(),
        turn_number=1,
        hook_context="",
        deadline=None,
        index=1,
        previous_request=None,
    )
    assert compactor.calls >= 1, "the compactor must have rewritten the history"
    contents = [m.content or "" for m in reporter.calls[-1]["request"]]
    assert any("read-ledger" in c and "a.py" in c for c in contents)


@pytest.mark.asyncio
async def test_an_empty_ledger_adds_no_block():
    """Nothing to say must mean not in the request: an empty block is a message
    the model pays for on every iteration."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content="开始", timestamp="t"))
    assembler, reporter, _compactor = _assembler()
    await assembler.assemble(
        session=session,
        task=TaskState(goal="长任务"),
        books=TurnBooks(),
        turn_number=1,
        hook_context="",
        deadline=None,
        index=1,
        previous_request=None,
    )
    assert not any(name == "read_ledger" for name, _ in reporter.calls[-1]["tail_blocks"])


# ------------------------------------------------------------------ normalise


def test_a_relative_path_stays_relative():
    assert normalize("ai_coding/core/agent_loop.py", "/ws") == "ai_coding/core/agent_loop.py"


def test_an_absolute_path_inside_the_workspace_becomes_relative():
    """Measured on T2: the same session received both
    ``/workspace/ai-coding/ai_coding/domain/plan_delivery.py`` and
    ``ai_coding/core/agent_loop.py`` for the very same files."""
    assert normalize("/ws/ai_coding/domain/plan_delivery.py", "/ws") == (
        "ai_coding/domain/plan_delivery.py"
    )


def test_a_leading_dot_slash_is_stripped():
    assert normalize("./a.py", "/ws") == "a.py"


def test_an_absolute_path_outside_the_workspace_falls_back_to_its_name():
    """Not our file, but the model may still name it; a basename keeps the
    citation check from declaring it "never seen"."""
    assert normalize("/etc/nginx/nginx.conf", "/ws") == "nginx.conf"


def test_same_file_accepts_all_three_spellings():
    """The ledger stores the workspace-relative path; the answer writes a
    basename; grep's output writes the relative path.  All three are one file."""
    assert same_file("ai_coding/domain/plan_delivery.py", "plan_delivery.py")
    assert same_file("plan_delivery.py", "ai_coding/domain/plan_delivery.py")
    assert same_file("/ws/ai_coding/domain/plan_delivery.py", "ai_coding/domain/plan_delivery.py")
    assert same_file("ai_coding/domain/plan_delivery.py", "ai_coding/domain/plan_delivery.py")


def test_same_file_rejects_a_different_file():
    assert not same_file("a.py", "b.py")
    assert not same_file("x/a.py", "y/b.py")


def test_two_same_named_files_in_different_directories_are_not_one_file():
    """Basename folding must not go both ways.  Folding ``b/foo.py`` onto a read
    of ``a/foo.py`` would answer a citation of the unread one from the read one,
    and would stop tier 1 ("no tool ever named this") from ever firing."""
    assert not same_file("a/foo.py", "b/foo.py")
    assert covers((ReadMark("a/foo.py", 1, 10, 10),), "b/foo.py", 1, 5) is False


def test_a_url_is_not_a_file_reference():
    """Without the left edge ``https://host/a/b.py`` yields ``host/a/b.py``."""
    assert paths_in("see https://example.com/a/b.py for details") == ()
    assert extract_claims("见 https://example.com/a/b.py 第 81 行") == []


def test_a_suffix_extension_is_not_swallowed():
    """``a.pyc`` is not ``a.py``; the right edge keeps the ``c`` out."""
    assert extract_claims("`a.pyc` 第 81 行") == []


# --------------------------------------------------------------------- marks


def test_overlapping_reads_merge_into_one_range():
    marks = merge_mark((), ReadMark("a.py", 1, 60, 441))
    marks = merge_mark(marks, ReadMark("a.py", 50, 100, 441))
    assert marks == (ReadMark("a.py", 1, 100, 441),)


def test_adjacent_reads_merge_too():
    marks = merge_mark((), ReadMark("a.py", 1, 60, 441))
    marks = merge_mark(marks, ReadMark("a.py", 61, 120, 441))
    assert marks == (ReadMark("a.py", 1, 120, 441),)


def test_separate_ranges_stay_separate():
    marks = merge_mark((), ReadMark("a.py", 1, 60, 441))
    marks = merge_mark(marks, ReadMark("a.py", 300, 434, 441))
    assert marks == (ReadMark("a.py", 1, 60, 441), ReadMark("a.py", 300, 434, 441))


def test_two_files_do_not_merge():
    marks = merge_mark((), ReadMark("a.py", 1, 60, 441))
    marks = merge_mark(marks, ReadMark("b.py", 1, 60, 441))
    assert len(marks) == 2


def test_the_ledger_drops_the_oldest_when_it_overflows():
    marks: tuple[ReadMark, ...] = ()
    for i in range(5):
        marks = merge_mark(marks, ReadMark(f"f{i}.py", 1, 10, 10), max_marks=3)
    assert [m.path for m in marks] == ["f2.py", "f3.py", "f4.py"]


def test_a_read_records_the_range_it_returned():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.note_read("a.py", 1, 60, 441)
    assert session.read_ledger == (ReadMark("a.py", 1, 60, 441),)


def test_an_absolute_read_and_a_relative_read_share_one_key():
    """The regression that would have made every citation look uncovered.

    ``read_tool`` always knows its sandbox workspace and hands it over, so the
    two spellings collapse to one key.
    """
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.note_read(
        "/ws/ai_coding/domain/plan_delivery.py", 61, 110, 117, workspace="/ws"
    )
    session.note_read("ai_coding/domain/plan_delivery.py", 1, 60, 117, workspace="/ws")
    assert len(session.read_ledger) == 1
    assert session.read_ledger[0] == ReadMark("ai_coding/domain/plan_delivery.py", 1, 110, 117)


def test_the_ledger_survives_a_round_trip_through_the_session_file():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.note_read("a.py", 1, 60, 441)
    restored = SessionData.from_dict(session.to_dict())
    assert restored.read_ledger == session.read_ledger


def test_a_session_written_before_this_layer_reads_as_an_empty_ledger():
    """The whitelist rule: an old file has no key, so it must not gain one."""
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    data = session.to_dict()
    assert "readLedger" not in data
    assert SessionData.from_dict(data).read_ledger == ()


# ------------------------------------------------------------------- coverage


def test_a_citation_inside_the_read_range_is_covered():
    marks = (ReadMark("a.py", 1, 60, 441),)
    assert covers(marks, "a.py", 10, 20)


def test_a_citation_past_the_read_range_is_not_covered():
    marks = (ReadMark("a.py", 61, 110, 117),)
    assert not covers(marks, "a.py", 18, 46)


def test_a_partially_overlapping_citation_is_not_covered():
    """R20b: containment, not intersection.

    The model read 61-110 and cites 100-120.  Lines 111-120 were never read, so
    a claim about them is the T2 hallucination shape.  Intersection let it pass;
    containment does not.  Paged reads merge (merge_mark), so the normal
    "read the file in 500-line chunks then cite the whole thing" still passes.
    """
    marks = (ReadMark("a.py", 61, 110, 117),)
    assert not covers(marks, "a.py", 100, 120)


def test_a_citation_spanning_a_gap_is_not_covered():
    """The shape the old intersection rule let through: read the head and the
    tail of a file, then cite the middle."""
    marks = (ReadMark("a.py", 1, 60, 441), ReadMark("a.py", 300, 434, 441))
    assert not covers(marks, "a.py", 50, 350)


def test_adjacent_paged_reads_merge_so_the_whole_file_is_covered():
    """Why containment does not break normal paging: merge_mark coalesces
    adjacent runs, so reading 1-500 then 501-1000 leaves one 1-1000 mark."""
    marks = (ReadMark("a.py", 1, 500, 1000), ReadMark("a.py", 501, 1000, 1000))
    merged = merge_mark(marks[:1], marks[1])
    assert covers(merged, "a.py", 1, 1000)


# ---- R20b: tier 2 (seen but not read) ----


def test_a_citation_past_the_end_of_a_seen_file_is_challenged():
    """grep showed the file, so the model may name it -- but a line past the end
    of the file is not a citation, it is a guess."""
    claims = [("a.py", 999, 999)]
    assert unsupported_claims((), ("a.py",), claims, size_of=lambda p: 100) == claims


def test_a_citation_range_running_past_eof_is_challenged():
    """The v1 rule used ``start > total`` and missed this: start=90 is inside a
    100-line file while end=999 is not."""
    claims = [("a.py", 90, 999)]
    assert unsupported_claims((), ("a.py",), claims, size_of=lambda p: 100) == claims


def test_a_citation_inside_a_seen_file_is_still_accepted():
    claims = [("a.py", 3, 3)]
    assert unsupported_claims((), ("a.py",), claims, size_of=lambda p: 100) == []


def test_an_unreadable_file_does_not_block_a_citation():
    """size_of returns 0 for a path we cannot read (outside the workspace, gone,
    a directory).  That is a widening, not a safety property -- documented."""
    claims = [("a.py", 999, 999)]
    assert unsupported_claims((), ("a.py",), claims, size_of=lambda p: 0) == []


def test_a_never_touched_file_is_still_challenged_with_size_of():
    """Tier 1 returns before tier 2, so adding size_of must not weaken it."""
    claims = [("a.py", 1, 10)]
    assert unsupported_claims((), (), claims, size_of=lambda p: 100) == claims


def test_without_size_of_tier_two_stays_open():
    """The eval script and old session files have no size source; the check must
    degrade to the R20 behaviour rather than raise."""
    claims = [("a.py", 999, 999)]
    assert unsupported_claims((), ("a.py",), claims) == []
    assert unsupported_claims((), ("a.py",), claims, size_of=None) == []


def test_tier_two_measures_the_seen_spelling_not_the_abbreviated_one():
    """An answer abbreviates ``ai_coding/ai/llm_helpers.py`` to ``llm_helpers.py``.
    ``same_file`` matches, but a size lookup against the workspace root for the
    bare name finds nothing -- tier 2 then silently failed open (review finding).
    """
    seen = ["ai_coding/ai/llm_helpers.py"]
    claims = [("llm_helpers.py", 999, 999)]
    asked: list[str] = []

    def size_of(path: str) -> int:
        asked.append(path)
        return 100 if path == "ai_coding/ai/llm_helpers.py" else 0

    assert unsupported_claims((), seen, claims, size_of=size_of) == claims
    assert asked == ["ai_coding/ai/llm_helpers.py"]


def test_a_size_lookup_that_raises_does_not_take_the_turn_down():
    claims = [("a.py", 999, 999)]

    def size_of(path: str) -> int:
        raise OSError("gone")

    assert unsupported_claims((), ("a.py",), claims, size_of=size_of) == []


def test_an_inverted_range_is_read_as_the_range_it_meant():
    """``a.py:100-1`` is a typo.  With containment it would otherwise look
    "inside" any mark starting at or before 100 -- weaker than the old rule."""
    assert extract_claims("见 a.py:100-1") == [("a.py", 1, 100)]
    assert extract_claims("见 a.py:7") == [("a.py", 7, 7)]


def test_a_basename_citation_matches_the_ledger():
    marks = (ReadMark("ai_coding/domain/plan_delivery.py", 61, 110, 117),)
    assert covers(marks, "plan_delivery.py", 81, 81)


def test_a_never_read_file_is_not_covered():
    assert not covers((), "a.py", 1, 10)


# --------------------------------------------------------------- missing ranges


def test_the_ledger_names_the_missing_ranges():
    marks = (ReadMark("a.py", 1, 60, 441), ReadMark("a.py", 300, 434, 441))
    assert missing_ranges(marks, "a.py") == [(61, 299), (435, 441)]


def test_a_fully_read_file_has_no_gaps():
    marks = (ReadMark("a.py", 1, 441, 441),)
    assert missing_ranges(marks, "a.py") == []


def test_a_file_of_unknown_length_reports_no_gaps():
    """``total == 0`` means we never learned the size; inventing a gap would
    send the model chasing lines that do not exist."""
    assert missing_ranges((ReadMark("a.py", 1, 60, 0),), "a.py") == []


# --------------------------------------------------------------------- render


def test_an_empty_ledger_renders_nothing():
    assert render(()) == ""


def test_the_render_names_the_gaps():
    marks = (ReadMark("a.py", 1, 60, 441), ReadMark("a.py", 300, 434, 441))
    text = render(marks)
    assert "缺 61-299" in text
    assert "共 441 行" in text


def test_a_fully_read_file_says_complete():
    """The per-file line must say 完整 and ask for no gap.  (The block's own
    instruction line legitimately contains 缺, so the assertion is on the file
    line, not on the whole block.)"""
    text = render((ReadMark("a.py", 1, 441, 441),))
    file_line = next(line for line in text.splitlines() if line.startswith("- "))
    assert "完整" in file_line
    assert "缺" not in file_line


def test_the_render_wraps_itself_in_the_block_tags():
    text = render((ReadMark("a.py", 1, 10, 10),))
    assert text.startswith("<read-ledger>")
    assert text.rstrip().endswith("</read-ledger>")


def test_the_render_tells_the_model_to_read_only_the_gaps():
    text = render((ReadMark("a.py", 1, 10, 441),))
    assert "按缺口重读" in text


def test_the_render_caps_the_file_count():
    marks = tuple(ReadMark(f"f{i}.py", 1, 10, 10) for i in range(12))
    text = render(marks, max_files=3)
    assert "还有 9 个文件" in text


def test_a_char_budget_cuts_at_a_line_boundary_not_through_one():
    """A character cut leaves the model holding half of a file line, which it
    will then try to interpret.  Every surviving file line must be whole."""
    marks = tuple(ReadMark(f"dir{i}/long_file_name.py", 1, 10, 10) for i in range(40))
    text = render(marks, max_chars=120)
    file_lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(file_lines) < 40, "the budget must have cut something"
    assert all(line.endswith("）") for line in file_lines), file_lines


def test_the_file_count_survives_the_char_budget():
    """A truncation that dropped the count would leave the model believing eight
    files is all there is."""
    marks = tuple(ReadMark(f"dir{i}/long_file_name.py", 1, 10, 10) for i in range(40))
    text = render(marks, max_chars=120, max_files=3)
    assert "还有 37 个文件" in text


# ------------------------------------------------------------------ seen paths


def test_paths_in_pulls_the_paths_out_of_a_grep_result():
    """grep renders ``path:line: text``; glob renders bare paths.  Both are the
    model's evidence that a file exists, which is what tier 1 of the citation
    check needs."""
    out = "ai_coding/core/agent_loop.py:1229: if not step.tool_calls:\n" \
          "ai_coding/domain/plan_delivery.py:81: turns"
    assert set(paths_in(out)) == {
        "ai_coding/core/agent_loop.py",
        "ai_coding/domain/plan_delivery.py",
    }


def test_paths_in_ignores_ordinary_prose():
    assert paths_in("this is not a file reference") == ()


def test_paths_in_normalises_an_absolute_path():
    assert paths_in("/ws/a.py") == ("a.py",)


def test_the_session_accumulates_seen_paths_without_duplicates():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.note_seen(["a.py", "b.py"])
    session.note_seen(["b.py", "c.py"])
    assert session.seen_paths == ("a.py", "b.py", "c.py")


def test_seen_paths_survive_a_round_trip():
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.note_seen(["a.py"])
    assert SessionData.from_dict(session.to_dict()).seen_paths == ("a.py",)


# ------------------------------------------------------------ claim extraction


def test_a_path_then_a_chinese_line_number_is_extracted():
    claims = extract_claims("**`ai_coding/domain/plan_delivery.py`，第 81 行**")
    assert claims == [("ai_coding/domain/plan_delivery.py", 81, 81)]


def test_an_inline_path_line_form_is_extracted():
    assert extract_claims("见 `plan_delivery.py:81`") == [("plan_delivery.py", 81, 81)]


def test_an_inline_range_form_is_extracted():
    assert extract_claims("`plan_delivery.py:18-46`") == [("plan_delivery.py", 18, 46)]


def test_a_chinese_range_binds_to_the_last_path_seen():
    text = "`plan_delivery.py` 的构造函数（第 18-46 行的 __init__）"
    assert extract_claims(text) == [("plan_delivery.py", 18, 46)]


def test_a_chinese_line_number_without_a_space_is_extracted():
    assert extract_claims("`plan_delivery.py` 第81行") == [("plan_delivery.py", 81, 81)]


def test_english_line_forms_are_extracted():
    assert extract_claims("`a.py` line 81") == [("a.py", 81, 81)]
    assert extract_claims("`a.py` L81") == [("a.py", 81, 81)]
    assert extract_claims("`a.py` #L81") == [("a.py", 81, 81)]


def test_a_bare_line_number_without_a_path_is_ignored():
    """No path to bind it to: the model may be talking about anything."""
    assert extract_claims("第 81 行") == []


# ------------------------------------------------- the three-tier citation rule


def test_a_citation_inside_the_read_range_is_supported():
    marks = (ReadMark("plan_delivery.py", 61, 110, 117),)
    assert unsupported_claims(marks, (), [("plan_delivery.py", 81, 81)]) == []


def test_a_citation_outside_the_read_range_is_challenged():
    """Tier 3 -- the shape of T2's hallucination: the file was read, the range
    was not."""
    marks = (ReadMark("plan_delivery.py", 61, 110, 117),)
    assert unsupported_claims(marks, (), [("plan_delivery.py", 18, 46)]) == [
        ("plan_delivery.py", 18, 46)
    ]


def test_a_citation_to_a_file_no_tool_ever_named_is_challenged():
    """Tier 1 -- the strongest signal: the model named a file this session never
    touched at all."""
    assert unsupported_claims((), (), [("a.py", 1, 10)]) == [("a.py", 1, 10)]


def test_a_citation_to_a_grep_only_file_is_accepted():
    """Tier 2 -- grep/glob/bash show lines the model may legitimately cite, and
    the ledger cannot say which ones, so the check must stay quiet.

    This is the false-positive guard: T2 was "grep `[0]` first, then read
    61-110", and a rule that challenged every unread *file* would have bounced
    its answer once per grep hit.
    """
    assert unsupported_claims((), ("a.py",), [("a.py", 1, 10)]) == []


def test_two_files_in_one_answer_skip_the_check():
    """Ambiguity guard.  A paragraph naming two files binds every line number to
    the last one, so ``a.py`` would be judged as if it were ``b.py``.

    The asymmetry is the point: ``b.py`` *was* read and ``a.py`` was not, so a
    check that ignored the ambiguity would challenge a citation the model had
    every right to make.
    """
    marks = (ReadMark("b.py", 1, 10, 10),)
    claims = [("a.py", 1, 10), ("b.py", 1, 10)]
    assert unsupported_claims(marks, (), claims) == []


def test_two_spellings_of_one_file_are_not_two_files():
    """Found on the first real re-run: the answer wrote ``llm_helpers.py`` and
    ``ai_coding/ai/llm_helpers.py`` for one file, the guard read that as two, and
    a fully checkable answer was skipped.

    The file is *unread* here, so the correct answer is "challenge it" -- which
    is what distinguishes this from the ambiguity guard's skip.
    """
    claims = [
        ("ai_coding/ai/llm_helpers.py", 89, 89),
        ("llm_helpers.py", 130, 133),
    ]
    assert unsupported_claims((), (), claims) == [
        ("ai_coding/ai/llm_helpers.py", 89, 89),
        ("llm_helpers.py", 130, 133),
    ]


def test_a_bare_line_number_without_a_path_is_never_challenged():
    assert unsupported_claims((), (), []) == []


def test_the_real_t2_answer_is_challenged_on_exactly_one_citation():
    """End to end on the answer that shipped: seven citations, six inside the
    61-110 the model read, one (``18-46``) invented."""
    marks = (ReadMark("ai_coding/domain/plan_delivery.py", 61, 110, 117),)
    seen = ("ai_coding/domain/plan_delivery.py",)
    answer = (
        "**`ai_coding/domain/plan_delivery.py`，第 81 行**\n"
        "```python\n# 第 73-85 行\n```\n"
        "查看 from_dict（第 88-110 行）\n"
        "再看构造函数签名（第 18-46 行的 __init__）\n"
        "将第 81 行改为防御性写法\n"
    )
    bad = unsupported_claims(marks, seen, extract_claims(answer))
    assert bad == [("ai_coding/domain/plan_delivery.py", 18, 46)]


# ------------------------------------------- R21 §6: position != content


def test_a_quote_the_cited_range_holds_passes():
    text = "import os\ndef build():\n    return 1\n"
    answer = '`a.py:2-3` 写的是 "def build(): return 1"。'
    assert unsupported_quotes(answer, (), lambda path: text) == []


def test_a_quote_the_range_no_longer_holds_is_reported():
    """The rewrite case: the range was read, the file has since changed."""
    text = "import os\ndef build():\n    return 1\n"
    answer = '`a.py:2-3` 写的是 "def deploy(): return 99"。'
    bad = unsupported_quotes(answer, (), lambda path: text)
    assert [row[:3] for row in bad] == [("a.py", 2, 3)]


def test_a_close_paraphrase_passes():
    """A reflowed quote of ten-plus tokens is tolerated; a short one is not."""
    text = "the loader reads alpha beta gamma delta epsilon zeta eta theta\n"
    answer = '`a.py:1` 说的是 "the loader reads alpha beta gamma delta epsilon zeta eta thetaa"。'
    assert unsupported_quotes(answer, (), lambda path: text) == []


def test_a_short_fragment_is_not_compared_with_sequence_matching():
    text = "def build():\n"
    answer = '`a.py:1` 里是 "def bui1d():"。'
    assert unsupported_quotes(answer, (), lambda path: text)


def test_an_unreadable_file_widens_rather_than_challenges():
    answer = '`a.py:1` 里是 "anything at all here"。'
    assert unsupported_quotes(answer, (), lambda path: None) == []


def test_a_range_past_the_end_of_the_file_widens():
    answer = '`a.py:50-52` 里是 "anything at all here"。'
    assert unsupported_quotes(answer, (), lambda path: "one line\n") == []


def test_fullwidth_punctuation_and_a_line_number_prefix_still_match():
    """Short on purpose: under ten tokens, so only the prefix strip can match
    it -- the paraphrase allowance must not be what saves this case."""
    text = "def f(x) -> x\n"
    answer = '`a.py:1` 里是 "    1  def f（x） －＞ x"。'
    assert unsupported_quotes(answer, (), lambda path: text) == []


def test_the_ledger_spelling_is_what_gets_read():
    """The answer abbreviates ``src/a.py`` to ``a.py``; the reader only knows
    the full path, so resolving through the ledger is what makes this check
    possible at all."""
    marks = (ReadMark("src/a.py", 1, 5, 5),)
    text = "def build():\n    return 1\n"
    answer = '`a.py:1-2` 写的是 "def build(): return 1"。'
    calls: list[str] = []

    def reader(path: str) -> str | None:
        calls.append(path)
        return text if path == "src/a.py" else None

    assert unsupported_quotes(answer, marks, reader) == []
    assert calls == ["src/a.py"]


def test_an_answer_naming_two_files_is_skipped_entirely():
    text = "def build():\n    return 1\n"
    answer = '`a.py:1-2` 与 `b.py:1` 里写的是 "def deploy(): return 99"。'
    assert unsupported_quotes(answer, (), lambda path: text) == []
