"""TDD (r17/T4): a user's @path must reach the model as content.

Same lifecycle as the skills mention (r16/E-3): resolved from the last user
message each iteration, injected as a volatile block, gone with the next user
message.  A token that names no existing file is not a reference (``cc @alice``
is prose); a token that resolves to a skill is claimed by the skill path first.
"""

from __future__ import annotations

import asyncio

from ai_coding.core.file_mentions import resolve_file_mentions
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


def _assembler(workspace, skills=None) -> RequestAssembler:
    class _Compactor:
        async def compress(self, history, **kwargs):
            return history

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


def _request_text(workspace, user_text: str, skills=None) -> str:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content=user_text, timestamp="t"))
    assembler = _assembler(workspace, skills)

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
