"""TDD (M1): background skill review.

Covers: SkillRegistry.write(), ReviewResult, _parse_review_response,
run_skill_review(), and AgentLoop integration (_maybe_schedule_skill_review,
_run_skill_review).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig
from ai_coding.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# SkillRegistry.write()
# ---------------------------------------------------------------------------


class TestSkillRegistryWrite:
    """SkillRegistry.write() — atomic SKILL.md creation."""

    def test_writes_skill_file(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        path = reg.write("my-skill", "---\nname: my-skill\ndescription: test\n---\n\n# Body\n")
        assert path is not None
        assert path.exists()
        assert path == tmp_path / "my-skill" / "SKILL.md"
        assert "name: my-skill" in path.read_text()

    def test_creates_parent_directory(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        path = reg.write("new-skill", "content")
        assert path is not None
        assert (tmp_path / "new-skill").is_dir()

    def test_rejects_name_with_slash(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        assert reg.write("invalid/name", "content") is None

    def test_rejects_dot_dot(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        assert reg.write("../etc", "content") is None

    def test_rejects_empty_name(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        assert reg.write("", "content") is None

    def test_write_is_idempotent(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        reg.write("skill", "content v1")
        reg.write("skill", "content v2")
        assert (tmp_path / "skill" / "SKILL.md").read_text() == "content v2"
        assert reg.names() == ["skill"]

    def test_write_is_atomic_no_partial_file_on_error(self, tmp_path):
        """If write fails mid-way, no partial .tmp file should remain."""
        reg = SkillRegistry(tmp_path)
        # Write valid content first
        path = reg.write("skill", "---\nname: skill\n---\n\n# Title\n")
        assert path.exists()
        # Check no .tmp files remain
        tmp_files = list(tmp_path.rglob("*.tmp"))
        assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# ReviewResult
# ---------------------------------------------------------------------------


class TestReviewResult:
    """ReviewResult data contract."""

    def test_frozen_dataclass(self):
        from ai_coding.core.skill_review import ReviewResult
        r = ReviewResult(created=True, skill_name="test", skill_path="/tmp/test", reason="good")
        assert r.created is True
        assert r.skill_name == "test"
        with pytest.raises(AttributeError):
            r.created = False  # frozen

    def test_defaults(self):
        from ai_coding.core.skill_review import ReviewResult
        r = ReviewResult(created=False)
        assert r.skill_name == ""
        assert r.skill_path == ""
        assert r.reason == ""


# ---------------------------------------------------------------------------
# _parse_review_response
# ---------------------------------------------------------------------------


class TestParseReviewResponse:
    """_parse_review_response — tolerant JSON parsing."""

    def test_plain_json(self):
        from ai_coding.core.skill_review import _parse_review_response
        raw = '{"create": true, "name": "test-skill"}'
        result = _parse_review_response(raw)
        assert result is not None
        assert result["create"] is True

    def test_fenced_code_block(self):
        from ai_coding.core.skill_review import _parse_review_response
        raw = '```json\n{"create": false, "reason": "nothing to save"}\n```'
        result = _parse_review_response(raw)
        assert result is not None
        assert result["create"] is False

    def test_fenced_without_language(self):
        from ai_coding.core.skill_review import _parse_review_response
        raw = '```\n{"create": true}\n```'
        result = _parse_review_response(raw)
        assert result is not None

    def test_empty_string(self):
        from ai_coding.core.skill_review import _parse_review_response
        assert _parse_review_response("") is None

    def test_non_json(self):
        from ai_coding.core.skill_review import _parse_review_response
        assert _parse_review_response("no json here") is None

    def test_json_array_instead_of_object(self):
        from ai_coding.core.skill_review import _parse_review_response
        assert _parse_review_response('[{"create": true}]') is None


# ---------------------------------------------------------------------------
# run_skill_review
# ---------------------------------------------------------------------------


def _make_model_config() -> ModelConfig:
    return ModelConfig(
        name="test-model",
        base_url="http://localhost",
        api_key="test-key",
    )


def _make_messages(content: str = "请帮我调试一个复杂的 pytest 失败") -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=content, timestamp="t1"),
        ChatMessage(role="assistant", content="好的，我来排查", timestamp="t2"),
        ChatMessage(role="tool", content="test output...", timestamp="t3", tool_call_id="c1"),
        ChatMessage(role="assistant", content="问题已修复", timestamp="t4"),
    ]


class TestRunSkillReview:
    """run_skill_review() — the core review logic."""

    @pytest.mark.asyncio
    async def test_skips_empty_messages(self, tmp_path):
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review([], [], _make_model_config(), tmp_path)
        assert result.created is False
        assert result.reason == "no messages"

    @pytest.mark.asyncio
    async def test_skips_empty_conversation_content(self, tmp_path):
        from ai_coding.core.skill_review import run_skill_review
        # Tool messages with blank content are skipped by _messages_to_text
        messages = [ChatMessage(role="tool", content="  \t ", timestamp="t", tool_call_id="c1")]
        result = await run_skill_review(messages, [], _make_model_config(), tmp_path)
        assert result.created is False

    @pytest.mark.asyncio
    async def test_model_says_no_create(self, tmp_path, monkeypatch):
        """Model returns create=false → no skill file written."""
        async def fake_chat(model_config, system, user, max_tokens):
            return json.dumps({"create": False, "reason": "nothing worth saving"})

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(_make_messages(), [], _make_model_config(), tmp_path)
        assert result.created is False
        assert result.reason == "nothing worth saving"
        # No skill files created
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_creates_skill_file(self, tmp_path, monkeypatch):
        """Model returns create=true → SKILL.md written."""
        skill_content = (
            "---\n"
            "name: debug-pytest\n"
            "description: Debug pytest failures systematically\n"
            "---\n\n"
            "## When to use\nUse when pytest fails with complex errors.\n\n"
            "## Steps\n1. Read the error\n2. Run with -v\n3. Fix\n\n"
            "## Verification\nRun pytest again\n"
        )

        async def fake_chat(model_config, system, user, max_tokens):
            return json.dumps({
                "create": True,
                "name": "debug-pytest",
                "description": "Debug pytest failures systematically",
                "content": skill_content,
                "reason": "5-step debugging workflow likely to repeat",
            })

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(_make_messages(), [], _make_model_config(), tmp_path)
        assert result.created is True
        assert result.skill_name == "debug-pytest"
        assert result.skill_path == str(tmp_path / "debug-pytest" / "SKILL.md")
        # Verify file content
        written = (tmp_path / "debug-pytest" / "SKILL.md").read_text()
        assert written == skill_content

    @pytest.mark.asyncio
    async def test_rejects_duplicate_skill_name(self, tmp_path, monkeypatch):
        """Skill with same name already exists → no overwrite."""
        # Pre-create the skill
        (tmp_path / "debug-pytest").mkdir()
        (tmp_path / "debug-pytest" / "SKILL.md").write_text("existing content")

        async def fake_chat(model_config, system, user, max_tokens):
            return json.dumps({
                "create": True,
                "name": "debug-pytest",
                "content": "new content",
                "reason": "should not overwrite",
            })

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(
            _make_messages(), ["debug-pytest"], _make_model_config(), tmp_path
        )
        assert result.created is False
        assert "already exists" in result.reason
        # Original file unchanged
        assert (tmp_path / "debug-pytest" / "SKILL.md").read_text() == "existing content"

    @pytest.mark.asyncio
    async def test_rejects_invalid_skill_name(self, tmp_path, monkeypatch):
        """Invalid name (with slash) → rejected."""
        async def fake_chat(model_config, system, user, max_tokens):
            return json.dumps({
                "create": True,
                "name": "invalid/name",
                "content": "---\nname: ---\n\n# x\n",
                "reason": "should reject",
            })

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(_make_messages(), [], _make_model_config(), tmp_path)
        assert result.created is False
        assert "invalid skill name" in result.reason

    @pytest.mark.asyncio
    async def test_rejects_short_content(self, tmp_path, monkeypatch):
        """Content too short → rejected."""
        async def fake_chat(model_config, system, user, max_tokens):
            return json.dumps({
                "create": True,
                "name": "short-skill",
                "content": "short",
                "reason": "should reject",
            })

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(_make_messages(), [], _make_model_config(), tmp_path)
        assert result.created is False
        assert "content too short" in result.reason

    @pytest.mark.asyncio
    async def test_handles_llm_failure(self, tmp_path, monkeypatch):
        """LLM call raises → graceful degradation."""
        async def fake_chat(model_config, system, user, max_tokens):
            raise RuntimeError("network error")

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(_make_messages(), [], _make_model_config(), tmp_path)
        assert result.created is False
        assert "LLM error" in result.reason

    @pytest.mark.asyncio
    async def test_handles_unparseable_response(self, tmp_path, monkeypatch):
        """Model returns non-JSON → graceful degradation."""
        async def fake_chat(model_config, system, user, max_tokens):
            return "I think there's no skill to save here."

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        result = await run_skill_review(_make_messages(), [], _make_model_config(), tmp_path)
        assert result.created is False
        assert "unparseable" in result.reason

    @pytest.mark.asyncio
    async def test_existing_skills_in_prompt(self, tmp_path, monkeypatch):
        """Existing skill names are sent in the prompt to avoid duplicates."""
        captured_prompts = []

        async def fake_chat(model_config, system, user, max_tokens):
            captured_prompts.append(system)
            return json.dumps({"create": False, "reason": "test"})

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        await run_skill_review(
            _make_messages(), ["existing-skill-1", "existing-skill-2"],
            _make_model_config(), tmp_path,
        )
        assert len(captured_prompts) == 1
        assert "existing-skill-1" in captured_prompts[0]
        assert "existing-skill-2" in captured_prompts[0]


# ---------------------------------------------------------------------------
# AgentLoop integration
# ---------------------------------------------------------------------------


class TestAgentLoopSkillReview:
    """AgentLoop._maybe_schedule_skill_review and _run_skill_review."""

    def test_counter_increments_each_call(self):
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig(skill_review_interval=5)
        loop.skills = SkillRegistry(Path("/tmp/fake"))
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

        loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 1
        loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 2

    def test_does_not_spawn_below_interval(self):
        """Below interval → no task spawned."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig(skill_review_interval=5)
        loop.skills = SkillRegistry(Path("/tmp/fake"))
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

        # Call 4 times — should not reach interval
        for _ in range(4):
            loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 4

    def test_spawns_at_interval(self, monkeypatch):
        """At interval → counter resets, task spawned."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        spawned = []

        def fake_create_task(coro, **kw):
            spawned.append(kw.get("name"))
            coro.close()  # prevent "coroutine never awaited" warning
            return SimpleNamespace()

        monkeypatch.setattr(asyncio, "create_task", fake_create_task)

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig(skill_review_interval=3)
        loop.skills = SkillRegistry(Path("/tmp/fake"))
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

        # Call 3 times — should trigger on 3rd
        for _ in range(2):
            loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 2
        assert len(spawned) == 0

        loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 0  # reset
        assert len(spawned) == 1
        assert "skill-review:s" in spawned[0]

    def test_disabled_when_interval_zero(self):
        """interval=0 → disabled, no spawn."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig(skill_review_interval=0)
        loop.skills = SkillRegistry(Path("/tmp/fake"))
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

        for _ in range(20):
            loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 0  # never increments

    def test_disabled_when_skills_none(self):
        """skills=None → disabled (e.g. sub-agent path)."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig(skill_review_interval=1)
        loop.skills = None
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

        loop._maybe_schedule_skill_review(session)
        assert session.skill_review_counter == 0

    @pytest.mark.asyncio
    async def test_run_review_advances_cursor(self, tmp_path, monkeypatch):
        """After review creates skill → cursor advances."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.core.skill_review import ReviewResult
        from ai_coding.domain.session import SessionData

        async def fake_review(messages, existing_skills, model_config, skill_dir):
            return ReviewResult(
                created=True,
                skill_name="test-skill",
                skill_path=str(skill_dir / "test-skill" / "SKILL.md"),
                reason="test",
            )

        monkeypatch.setattr("ai_coding.core.skill_review.run_skill_review", fake_review)

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig()
        loop.skills = SkillRegistry(tmp_path)
        loop.engine = SimpleNamespace(model_config=_make_model_config())
        loop._emit = lambda event: None

        session = SessionData(
            session_id="s", title="", created_time="t", last_access_time="t",
            messages=_make_messages(),
        )
        session.skill_review_cursor = 0

        await loop._run_skill_review(session)
        assert session.skill_review_cursor == len(session.messages)

    @pytest.mark.asyncio
    async def test_run_review_no_new_messages(self, tmp_path, monkeypatch):
        """Cursor already at end → no review."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        called = []
        async def fake_review(messages, *a, **kw):
            called.append(True)
            from ai_coding.core.skill_review import ReviewResult
            return ReviewResult(created=False)

        monkeypatch.setattr("ai_coding.core.skill_review.run_skill_review", fake_review)

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig()
        loop.skills = SkillRegistry(tmp_path)
        loop.engine = SimpleNamespace(model_config=_make_model_config())
        loop._emit = lambda event: None

        session = SessionData(
            session_id="s", title="", created_time="t", last_access_time="t",
            messages=_make_messages(),
        )
        session.skill_review_cursor = len(session.messages)  # already at end

        await loop._run_skill_review(session)
        assert len(called) == 0

    @pytest.mark.asyncio
    async def test_run_review_handles_exception(self, tmp_path, monkeypatch):
        """Review raises → caught, doesn't propagate."""
        from ai_coding.config.models import AIConfig
        from ai_coding.core.agent_loop import AgentLoop
        from ai_coding.domain.session import SessionData

        async def fake_review(messages, *a, **kw):
            raise RuntimeError("LLM exploded")

        monkeypatch.setattr("ai_coding.core.skill_review.run_skill_review", fake_review)

        loop = AgentLoop.__new__(AgentLoop)
        loop.settings = AIConfig()
        loop.skills = SkillRegistry(tmp_path)
        loop.engine = SimpleNamespace(model_config=_make_model_config())
        loop._emit = lambda event: None

        session = SessionData(
            session_id="s", title="", created_time="t", last_access_time="t",
            messages=_make_messages(),
        )
        # Should not raise
        await loop._run_skill_review(session)
