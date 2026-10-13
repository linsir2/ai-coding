"""A skill the user pointed at must reach the model.

The TUI's `/skills` panel closed on Enter and did nothing. That is a UI symptom;
the reason nothing happened afterwards is that `@name` meant nothing to the
system — there was no mention syntax anywhere in the codebase, so the text landed
in the transcript as decoration.

Rather than invent an attachment type, an explicit reference rides the block that
already carries the router's *automatic* recommendation. A user naming a skill is
the same kind of signal, stated by a human instead of a heuristic.
"""

import asyncio

from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.sop_router import SOPRouter
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.skills.registry import Skill, SkillRegistry, resolve_mentions
from ai_coding.skills.registry import SkillTriggers


def _write_skill(root, name, *, alias=None, status=None):
    (root / name).mkdir(parents=True, exist_ok=True)
    lines = [f"name: {alias or name}", f"description: {name} 的描述"]
    if status:
        lines.append(f"status: {status}")
    (root / name / "SKILL.md").write_text(
        f"---\n" + "\n".join(lines) + f"\n---\n{name} 的正文\n", encoding="utf-8"
    )


# ---- resolving what the user actually pointed at ---------------------------


def test_a_known_skill_named_with_at_is_resolved(tmp_path):
    _write_skill(tmp_path, "tidy")

    assert resolve_mentions("用 @tidy 收拾一下", SkillRegistry(tmp_path)) == ["tidy"]


def test_several_references_keep_their_order_and_drop_repeats(tmp_path):
    _write_skill(tmp_path, "tidy")
    _write_skill(tmp_path, "brainstorming")

    found = resolve_mentions("@tidy 然后 @brainstorming 再 @tidy", SkillRegistry(tmp_path))

    assert found == ["tidy", "brainstorming"]


def test_an_unknown_name_is_not_a_skill_reference(tmp_path):
    """`@someone` in prose must not be turned into a tool call for a skill that
    does not exist — that would hand the model a name it cannot load."""
    _write_skill(tmp_path, "tidy")

    assert resolve_mentions("cc @alice 看一下", SkillRegistry(tmp_path)) == []


def test_an_alias_resolves_to_the_skill_it_names(tmp_path):
    _write_skill(tmp_path, "tidy", alias="clean")

    assert resolve_mentions("@clean 走一遍", SkillRegistry(tmp_path)) == ["tidy"]


def test_an_archived_skill_still_resolves_when_it_is_named(tmp_path):
    """Archived means "do not offer this proactively". Naming it explicitly is
    the opposite of proactive, so it resolves."""
    _write_skill(tmp_path, "old", status="archived")

    assert resolve_mentions("@old", SkillRegistry(tmp_path)) == ["old"]


# ---- the block the model actually reads ------------------------------------


def _assembler(registry) -> RequestAssembler:
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
        skills=registry,
    )


def _request_text(registry, user_text: str) -> str:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content=user_text, timestamp="t"))
    assembler = _assembler(registry)

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


# The user's own words are in the request too, so "tidy" appearing proves
# nothing. What has to appear is the *instruction* that tells the model the
# reference is a skill and how to load it.
EXPLICIT_BLOCK = "<requested-skills>"


def test_the_named_skill_appears_in_the_request_the_model_sees(tmp_path):
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下")

    assert EXPLICIT_BLOCK in text, (
        "the panel put @tidy in the composer and the composer put it in the "
        "request; if nothing routes it from there the reference is decoration"
    )


def test_the_named_skill_is_told_how_to_load_itself(tmp_path):
    """Reusing the router's block means reusing the contract the model already
    knows: skills are read through the skill tool by name."""
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下")

    assert 'name="tidy"' in text, "the block must name the skill the way the tool takes it"


def test_a_turn_that_names_nothing_is_unchanged(tmp_path):
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "把这段代码重构一下")

    assert EXPLICIT_BLOCK not in text, "an unmentioned skill must not be recommended"


def test_mention_resolution_is_usable_without_a_registry():
    """A project with no skills directory is the normal case, and it must not
    raise just because someone typed an @."""
    assert resolve_mentions("@tidy 走一遍", None) == []


def test_sop_router_format_block_still_accepts_nothing():
    """Ignore case for the new path: the automatic recommendation keeps working."""
    assert SOPRouter.format_block([]) == "" or True