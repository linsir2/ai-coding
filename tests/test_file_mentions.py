"""TDD (r17/T4): a user's @path must reach the model as content.

Same lifecycle as the skills mention (r16/E-3): resolved from the last user
message each iteration, injected as a volatile block, gone with the next user
message.  A token that names no existing file is not a reference (``cc @alice``
is prose); a token that resolves to a skill is claimed by the skill path first.
"""

from __future__ import annotations

import asyncio

from ai_coding.core.file_mentions import resolve_bare_paths, resolve_file_mentions
from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.skills.registry import SkillRegistry, iter_mentions


def test_iter_mentions_is_the_single_tokenizer():
    """One regex, two consumers: the skills resolver and the files resolver."""
    assert iter_mentions("看 @docs/api.md 和 @tidy") == ["docs/api.md", "tidy"]


def test_a_file_mention_resolves_when_it_exists(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.md").write_text("API\n", encoding="utf-8")

    assert resolve_file_mentions("看 @docs/api.md 里怎么写", tmp_path) == ["docs/api.md"]


def test_a_directory_mention_resolves(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x", encoding="utf-8")

    assert resolve_file_mentions("@pkg 里有什么", tmp_path) == ["pkg"]


def test_a_name_that_is_not_a_path_is_not_a_file_mention(tmp_path):
    assert resolve_file_mentions("cc @alice 看看", tmp_path) == []


def test_an_escaping_token_is_not_resolved(tmp_path):
    (tmp_path / "inside.txt").write_text("x", encoding="utf-8")

    assert resolve_file_mentions("@../secret.txt", tmp_path) == []
    assert resolve_file_mentions("@/etc/passwd", tmp_path) == []


def test_an_excluded_token_is_left_to_the_skill_path(tmp_path):
    (tmp_path / "docs").mkdir()

    assert resolve_file_mentions("@docs", tmp_path, exclude={"docs"}) == []


# ---- the block the model actually reads ------------------------------------


def _assembler(workspace, skills=None, window=None) -> RequestAssembler:
    class _Compactor:
        async def compress(self, history, **kwargs):
            return history

        # R22/H10: the assembler asks the compactor for the window the pipeline
        # actually compacts against (``/model`` can repoint it).  ``None`` here
        # is the "stub without an answer" case and must fall back to the default.
        context_window = window

    class _Reporter:
        def report_context(self, **kwargs):
            return "canonical"

        def tokens_spent(self) -> int:
            return 0

    class _Memory:
        def block(self):
            return "", []

    class _Settings:
        working_memory_chars = 1200

    async def _instructions(deadline, session=None):
        return None

    return RequestAssembler(
        compactor=_Compactor(),
        policy=TurnPolicy(TurnPolicyConfig()),
        memory=_Memory(),
        reporter=_Reporter(),
        build_instructions=_instructions,
        settings=_Settings(),
        emit=lambda event: None,
        skills=skills,
        workspace=str(workspace),
    )


def _request_text(workspace, user_text: str, skills=None, window=None) -> str:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content=user_text, timestamp="t"))
    assembler = _assembler(workspace, skills, window)

    async def _run():
        return await assembler.assemble(
            session=session,
            task=TaskState(goal=""),
            books=TurnBooks(),
            turn_number=1,
            hook_context="",
            deadline=None,
            index=0,
            previous_request=None,
        )

    return "\n".join(m.content or "" for m in asyncio.run(_run()).request)


def test_the_file_block_carries_the_content(tmp_path):
    (tmp_path / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")

    text = _request_text(tmp_path, "看 @notes.txt")

    assert "<referenced-files>" in text
    assert 'path="notes.txt"' in text
    assert "the answer is 42" in text


def test_the_file_block_is_capped(tmp_path):
    (tmp_path / "big.txt").write_text("y" * 100_000, encoding="utf-8")

    text = _request_text(tmp_path, "@big.txt")

    assert "[truncated" in text
    assert len(text) < 60_000


def test_a_directory_mention_lists_its_entries(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x", encoding="utf-8")

    text = _request_text(tmp_path, "@pkg")

    assert 'path="pkg"' in text and "a.py" in text


def test_no_mention_means_no_block(tmp_path):
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    text = _request_text(tmp_path, "重构一下这段代码")

    assert "<referenced-files>" not in text


def test_a_skill_mention_is_not_treated_as_a_file(tmp_path):
    """@tidy resolves to a skill and must not also be read as a path."""
    (tmp_path / "tidy").mkdir()
    (tmp_path / "tidy" / "SKILL.md").write_text(
        "---\ndescription: d\n---\nbody\n", encoding="utf-8"
    )

    registry = SkillRegistry(tmp_path)
    text = _request_text(tmp_path, "@tidy 用它", skills=registry)

    assert "<referenced-files>" not in text
    assert "<requested-skills>" in text


# ---- D5: a pasted path, with no @ to introduce it --------------------------
#
# opencode materializes pasted files before the turn is queued
# (docs/orchestration-research/03-opencode编排数据流.md:37,42); ours required
# the user to type `@` first, so pasting a path handed the model a string of
# characters it could not use.  These paths are resolved only inside the
# workspace, because the block reads with ``read_text`` and never goes through
# ``Sandbox`` -- an outside path belongs to the read tool and its approval.


def test_a_pasted_absolute_path_reaches_the_model(tmp_path):
    (tmp_path / "notes.txt").write_text("the answer is 42\n", encoding="utf-8")

    text = _request_text(tmp_path, f"看下 {tmp_path}/notes.txt")

    assert "<referenced-files>" in text
    assert "the answer is 42" in text


def test_a_pasted_relative_path_reaches_the_model(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("MARKER\n", encoding="utf-8")

    text = _request_text(tmp_path, "我们用 pkg/a.py 这个文件")

    assert "MARKER" in text


def test_a_pasted_path_outside_the_workspace_is_not_injected(tmp_path):
    """The block reads with ``read_text`` and never asks ``Sandbox``, so
    injecting an outside path would quietly step around the approval the read
    tool now asks for.  It stays the read tool's business."""
    secret = tmp_path.parent / "outside_secret.txt"
    secret.write_text("TOP SECRET\n", encoding="utf-8")

    text = _request_text(tmp_path, f"看下 {secret}")

    assert "TOP SECRET" not in text


def test_a_url_is_not_a_path(tmp_path):
    (tmp_path / "com").mkdir()
    (tmp_path / "com" / "a.py").write_text("x", encoding="utf-8")

    assert resolve_bare_paths("看 https://example.com/a/b.py", tmp_path) == []


def test_something_that_is_not_a_path_is_ignored(tmp_path):
    """``3/4`` and ``1.2/3.4`` match the shape; only existence tells them apart,
    and nothing on disk is called ``3`` or ``1.2``."""
    (tmp_path / "3").mkdir()

    assert resolve_bare_paths("ratio 是 3/4 左右", tmp_path) == []
    assert resolve_bare_paths("版本 1.2/3.4 发布", tmp_path) == []


def test_a_pasted_path_a_skill_claimed_stays_with_the_skill(tmp_path):
    (tmp_path / "tidy").mkdir()
    (tmp_path / "tidy" / "SKILL.md").write_text(
        "---\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    registry = SkillRegistry(tmp_path)

    text = _request_text(tmp_path, "@tidy 用它", skills=registry)

    assert "<requested-skills>" in text
    assert "<referenced-files>" not in text


def test_a_bare_path_and_an_at_path_are_deduplicated(tmp_path):
    (tmp_path / "a.py").write_text("ONCE\n", encoding="utf-8")

    assert resolve_bare_paths("看 a.py 还有 @a.py", tmp_path) == ["a.py"]


def test_a_bare_filename_the_user_is_talking_about_resolves(tmp_path):
    """A pasted ``main.py`` arrives without any directory part, and it is as real
    a reference as a full path."""
    (tmp_path / "main.py").write_text("ENTRY\n", encoding="utf-8")

    assert resolve_bare_paths("用 main.py 这个文件", tmp_path) == ["main.py"]


def test_prose_that_shares_the_shape_is_still_prose(tmp_path):
    """The extension allow-list is what keeps these out; existence alone would
    not, because a workspace does contain ``README.md``."""
    (tmp_path / "README.md").write_text("x", encoding="utf-8")

    for text in ("ratio 是 3/4 左右", "版本 1.2/3.4 发布", "e.g. 这个路径", "好的。"):
        assert resolve_bare_paths(text, tmp_path) == []


def test_a_file_over_the_remaining_budget_is_truncated_not_dropped(tmp_path):
    """R22/H10: the budget decides how much of a file the model sees, and every
    named file still arrives with a head plus the ``read`` call that continues it.

    Before this the block had a flat 40,000-character budget whatever the window
    was, so a 6k window received 40,138 characters on every iteration (96% of the
    request) and compaction could not touch it.
    """
    for name in "abc":
        (tmp_path / f"{name}.txt").write_text(f"MARKER_{name}\n" + _lines(200), encoding="utf-8")

    text = _request_text(tmp_path, "a.txt b.txt c.txt")

    for name in "abc":
        assert f'<file path="{name}.txt">' in text, f"{name}.txt was dropped"
    assert "budget exhausted" not in text
    assert "[truncated" in text and "read offset=" in text
    assert len(text) < 32_000


def _block(text: str) -> str:
    start = text.index("<referenced-files>")
    return text[start : text.index("</referenced-files>") + len("</referenced-files>")]


def _lines(count: int, width: int = 79) -> str:
    return "".join(f"{i:05d} " + "z" * (width - 6) + "\n" for i in range(1, count + 1))


def test_the_block_budget_scales_with_the_context_window(tmp_path):
    """A 6k window received 40,138 characters per request (S3, measured).

    The budget is 15% of the window in tokens; ``estimate_tokens`` says CJK is
    half a token per character, so the character count is not ``tokens × 4``.
    The assertion allows ~100 tokens of wrapper (header, tags, truncation note),
    which is fixed and independent of the window.
    """
    from ai_coding.core.context_compression import estimate_tokens

    (tmp_path / "big.txt").write_text(_lines(6000), encoding="utf-8")

    block = _block(_request_text(tmp_path, "@big.txt", window=6000))

    assert len(block) <= 4_200
    assert estimate_tokens(block) <= 1_050


def test_the_default_window_keeps_the_block_near_fifteen_percent(tmp_path):
    from ai_coding.core.context_compression import estimate_tokens

    (tmp_path / "big.txt").write_text(_lines(6000), encoding="utf-8")

    block = _block(_request_text(tmp_path, "@big.txt", window=48_000))

    assert len(block) <= 29_000
    assert estimate_tokens(block) <= 7_400


def test_a_huge_window_still_caps_the_block(tmp_path):
    """Ten thousand tokens is the ceiling the old 40,000 ASCII characters stood for."""
    for name in "abcd":
        (tmp_path / f"{name}.txt").write_text(_lines(6000), encoding="utf-8")

    block = _block(_request_text(tmp_path, "@a.txt @b.txt @c.txt @d.txt", window=200_000))

    assert len(block) <= 41_000


def test_a_tiny_file_after_a_huge_one_still_arrives(tmp_path):
    """The 21-byte file used to become a bare pointer once the first ate the budget."""
    (tmp_path / "big.txt").write_text(_lines(6000), encoding="utf-8")
    (tmp_path / "tiny.txt").write_text("TINY_SECOND_FILE_MARK\n", encoding="utf-8")

    text = _request_text(tmp_path, "@big.txt @tiny.txt", window=6000)

    assert "TINY_SECOND_FILE_MARK" in text


def test_the_truncation_note_says_what_it_holds_and_how_to_read_on(tmp_path):
    """The S3 failure: the model read a 497-line head of a 6000-line file and
    concluded the file was complete.  The note must name the range and the call
    that continues it."""
    import re

    lines = [f"{i:05d} " + "z" * 72 for i in range(1, 6001)]
    (tmp_path / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    block = _block(_request_text(tmp_path, "@big.txt", window=6000))

    shown = re.search(r"this text is lines 1-(\d+)", block)
    offset = re.search(r"read offset=(\d+)", block)
    assert shown and offset, block[-400:]
    # `read`'s offset is 0-based, so "after line N" is exactly offset=N.
    assert int(offset.group(1)) == int(shown.group(1))
    assert "6000 lines total" in block
