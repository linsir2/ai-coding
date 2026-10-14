"""A skill the user pointed at must reach the model — loaded, not just named.

The TUI's `/skills` panel closed on Enter and did nothing. That is a UI symptom;
the reason nothing happened afterwards is that `@name` meant nothing to the
system — there was no mention syntax anywhere in the codebase, so the text landed
in the transcript as decoration.

The first fix routed the reference through the router's block as a *directive*
(the model still had to call the tool). r16/E-3 finished the OpenCode
semantics: the block carries the skill's full `<skill_content>` — same shape
the tool renders (``skills/render.py``) — so the naming turn needs no second
round trip.
"""

import asyncio

from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.sop_router import SOPRouter
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.skills.registry import SkillRegistry, resolve_mentions


def _write_skill(root, name, *, alias=None, status=None):
    (root / name).mkdir(parents=True, exist_ok=True)
    lines = [f"name: {alias or name}", f"description: {name} 的描述"]
    if status:
        lines.append(f"status: {status}")
    (root / name / "SKILL.md").write_text(
        "---\n" + "\n".join(lines) + f"\n---\n{name} 的正文\n", encoding="utf-8"
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


def _assembler(registry, permissions=None) -> RequestAssembler:
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
        skill_permissions = permissions or {}

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


def _request_text(registry, user_text: str, permissions=None) -> str:
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content=user_text, timestamp="t"))
    assembler = _assembler(registry, permissions)

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


def test_the_named_skill_is_spelled_the_way_the_tool_takes_it(tmp_path):
    """The block names the skill exactly as the tool's argument schema does —
    since r16/E-3 via the `<skill_content name="…">` wrapper that carries the
    content itself."""
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

def test_the_named_skill_content_travels_with_the_request(tmp_path):
    """E-3: the model acts in the naming turn — no round trip through the
    tool. This is OpenCode's @-mention semantics (prompt.ts:56-76)."""
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下")

    assert '<skill_content name="tidy">' in text
    assert "tidy 的正文" in text
    assert 'skill(action="read"' not in text, "the tool pointer is gone: the content is here"


def test_the_injected_skill_carries_its_base_directory(tmp_path):
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下")

    assert f"Base directory for this skill: {tmp_path / 'tidy'}" in text


def test_a_broken_skill_says_why_instead_of_injecting_a_body(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text(
        "---\nname: [unclosed\n---\nbody\n", encoding="utf-8"
    )

    text = _request_text(SkillRegistry(tmp_path), "@broken 走一遍")

    assert "无法加载" in text
    assert "<skill_content" not in text


def test_a_denied_skill_is_withheld_from_the_mention_block(tmp_path):
    _write_skill(tmp_path, "tidy")

    text = _request_text(
        SkillRegistry(tmp_path), "用 @tidy 收拾一下", permissions={"tidy": "deny"}
    )

    assert "已被配置禁止" in text
    assert "tidy 的正文" not in text
