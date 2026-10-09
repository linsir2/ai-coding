"""M1 Quality Improvements: prompt, validation, message filtering."""

from __future__ import annotations

import json

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig


def _cfg() -> ModelConfig:
    return ModelConfig(name="test-model", base_url="http://localhost", api_key="test-key")


# ---------------------------------------------------------------------------
# Improvement 1: Better prompt with examples
# ---------------------------------------------------------------------------


class TestPromptQuality:
    """Verify the review prompt contains concrete examples and clear format."""

    def test_prompt_has_positive_examples(self):
        """Prompt should show what a GOOD skill looks like."""
        from ai_coding.core.skill_review import _REVIEW_SYSTEM_PROMPT
        # Must have at least one concrete positive example
        assert "例如" in _REVIEW_SYSTEM_PROMPT or "例如" in _REVIEW_SYSTEM_PROMPT or "example" in _REVIEW_SYSTEM_PROMPT.lower()

    def test_prompt_has_negative_examples(self):
        """Prompt should show what a BAD skill looks like."""
        from ai_coding.core.skill_review import _REVIEW_SYSTEM_PROMPT
        # Must have at least one concrete negative example
        assert "不保存" in _REVIEW_SYSTEM_PROMPT or "不要" in _REVIEW_SYSTEM_PROMPT

    def test_prompt_specifies_output_format(self):
        """Prompt must specify exact JSON output format."""
        from ai_coding.core.skill_review import _REVIEW_SYSTEM_PROMPT
        assert '"create"' in _REVIEW_SYSTEM_PROMPT
        assert '"name"' in _REVIEW_SYSTEM_PROMPT
        assert '"content"' in _REVIEW_SYSTEM_PROMPT
        assert '"reason"' in _REVIEW_SYSTEM_PROMPT

    def test_prompt_specifies_content_structure(self):
        """Prompt must specify what the SKILL.md content should contain."""
        from ai_coding.core.skill_review import _REVIEW_SYSTEM_PROMPT
        # Must mention frontmatter and sections
        assert "frontmatter" in _REVIEW_SYSTEM_PROMPT or "---" in _REVIEW_SYSTEM_PROMPT
        assert "使用场景" in _REVIEW_SYSTEM_PROMPT or "When to use" in _REVIEW_SYSTEM_PROMPT
        assert "步骤" in _REVIEW_SYSTEM_PROMPT or "Steps" in _REVIEW_SYSTEM_PROMPT
        assert "验证" in _REVIEW_SYSTEM_PROMPT or "Verification" in _REVIEW_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Improvement 2: Skill content validation
# ---------------------------------------------------------------------------


class TestSkillContentValidation:
    """Validate SKILL.md content has proper structure."""

    def test_valid_skill_passes(self):
        from ai_coding.core.skill_review import _validate_skill_content
        content = (
            "---\n"
            "name: test-skill\n"
            "description: A test skill\n"
            "version: 1\n"
            "---\n\n"
            "## 使用场景\n"
            "当需要测试时使用\n\n"
            "## 步骤\n"
            "1. 第一步\n"
            "2. 第二步\n\n"
            "## 验证\n"
            "运行测试确认通过\n"
        )
        is_valid, reason = _validate_skill_content(content)
        assert is_valid is True, f"Expected valid, got: {reason}"

    def test_missing_frontmatter_fails(self):
        from ai_coding.core.skill_review import _validate_skill_content
        content = "x" * 150  # Long enough but no frontmatter
        is_valid, reason = _validate_skill_content(content)
        assert is_valid is False
        assert "frontmatter" in reason.lower() or "---" in reason

    def test_missing_name_fails(self):
        from ai_coding.core.skill_review import _validate_skill_content
        content = (
            "---\n"
            "description: A test skill\n"  # No name field
            + "x" * 100 + "\n"  # Pad to pass length check
            "---\n\n"
            "## 使用场景\n"
            "当需要测试时使用\n\n"
            "## 步骤\n"
            "1. 第一步\n"
            "2. 第二步\n\n"
            "## 验证\n"
            "运行测试确认通过\n"
        )
        is_valid, reason = _validate_skill_content(content)
        assert is_valid is False
        assert "name" in reason.lower()

    def test_missing_when_to_use_fails(self):
        from ai_coding.core.skill_review import _validate_skill_content
        content = (
            "---\n"
            "name: test-skill\n"
            "description: test\n"
            "---\n\n"
            + "x" * 100 + "\n"  # Pad to pass length check
            "## 步骤\n"
            "1. 第一步\n"
        )
        is_valid, reason = _validate_skill_content(content)
        assert is_valid is False
        assert "使用场景" in reason or "when" in reason.lower()

    def test_missing_steps_fails(self):
        from ai_coding.core.skill_review import _validate_skill_content
        content = (
            "---\n"
            "name: test-skill\n"
            "description: test\n"
            "---\n\n"
            "## 使用场景\n"
            "当需要测试时使用\n"
            + "x" * 100 + "\n"  # Pad to pass length check
        )
        is_valid, reason = _validate_skill_content(content)
        assert is_valid is False
        assert "步骤" in reason or "steps" in reason.lower()

    def test_too_short_fails(self):
        from ai_coding.core.skill_review import _validate_skill_content
        content = "---\nname: x\ndescription: y\n---\n\n## 使用场景\nz\n\n## 步骤\n1. a\n"
        is_valid, reason = _validate_skill_content(content)
        assert is_valid is False
        assert "too short" in reason.lower() or "长度" in reason


# ---------------------------------------------------------------------------
# Improvement 3: Message filtering
# ---------------------------------------------------------------------------


class TestMessageFiltering:
    """Verify that noise messages are filtered out."""

    def test_filters_pure_tool_results(self):
        """Tool results with only noise (exit codes, empty) should be filtered."""
        from ai_coding.core.skill_review import _filter_messages
        messages = [
            ChatMessage(role="user", content="创建一个新的 FastAPI 项目", timestamp="t1"),
            ChatMessage(role="tool", content="Exit code: 0", timestamp="t2", tool_call_id="c1"),
            ChatMessage(role="tool", content="  \n  ", timestamp="t3", tool_call_id="c2"),
            ChatMessage(role="assistant", content="项目创建成功，包含所有必要文件", timestamp="t4"),
        ]
        filtered = _filter_messages(messages)
        # Should keep user and assistant, filter noise tool results
        assert len(filtered) == 2
        assert filtered[0].role == "user"
        assert filtered[1].role == "assistant"

    def test_keeps_meaningful_tool_results(self):
        """Tool results with actual content should be kept."""
        from ai_coding.core.skill_review import _filter_messages
        messages = [
            ChatMessage(role="user", content="读取项目的 models.py 文件", timestamp="t1"),
            ChatMessage(role="tool", content="def hello():\n    print('hello')", timestamp="t2", tool_call_id="c1"),
            ChatMessage(role="assistant", content="这是一个 Python 函数定义", timestamp="t3"),
        ]
        filtered = _filter_messages(messages)
        assert len(filtered) == 3  # all kept

    def test_filters_very_short_messages(self):
        """Messages with very short content (1 char) are filtered."""
        from ai_coding.core.skill_review import _filter_messages
        messages = [
            ChatMessage(role="user", content="好", timestamp="t1"),  # 1 char, filtered
            ChatMessage(role="assistant", content="好的，我来处理这个请求", timestamp="t2"),
            ChatMessage(role="user", content="o", timestamp="t3"),  # 1 char, filtered
        ]
        filtered = _filter_messages(messages)
        # Only the substantive assistant message remains
        assert len(filtered) == 1
        assert filtered[0].content == "好的，我来处理这个请求"

    def test_preserves_order(self):
        """Filtering should preserve message order."""
        from ai_coding.core.skill_review import _filter_messages
        messages = [
            ChatMessage(role="user", content="执行计划中的第一步", timestamp="t1"),
            ChatMessage(role="tool", content="Exit code: 0", timestamp="t2", tool_call_id="c1"),
            ChatMessage(role="assistant", content="第一步已完成", timestamp="t3"),
            ChatMessage(role="user", content="执行计划中的第二步", timestamp="t4"),
        ]
        filtered = _filter_messages(messages)
        assert len(filtered) == 3
        assert filtered[0].content == "执行计划中的第一步"
        assert filtered[1].content == "第一步已完成"
        assert filtered[2].content == "执行计划中的第二步"


# ---------------------------------------------------------------------------
# Integration: improved run_skill_review
# ---------------------------------------------------------------------------


class TestImprovedRunSkillReview:
    """Test the improved review with better prompt, validation, and filtering."""

    @pytest.mark.asyncio
    async def test_validates_skill_content_before_writing(self, tmp_path, monkeypatch):
        """If model returns create=true but content is invalid, don't write."""
        async def fake_chat(model_config, system, user, max_tokens):
            return json.dumps({
                "create": True,
                "name": "bad-skill",
                "description": "bad",
                "content": "too short",  # Will fail validation
                "reason": "should fail",
            })

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        messages = [
            ChatMessage(role="user", content="请帮我调试一个复杂的 pytest 失败问题", timestamp="t1"),
            ChatMessage(role="assistant", content="好的，我来排查问题", timestamp="t2"),
            ChatMessage(role="tool", content="test output...", timestamp="t3", tool_call_id="c1"),
            ChatMessage(role="assistant", content="问题已修复", timestamp="t4"),
        ]
        result = await run_skill_review(messages, [], _cfg(), tmp_path)
        assert result.created is False  # Content validation failed
        # No file written
        assert not (tmp_path / "bad-skill").exists()

    @pytest.mark.asyncio
    async def test_filters_noise_before_review(self, tmp_path, monkeypatch):
        """Noise messages should be filtered before sending to LLM."""
        captured_text = []

        async def fake_chat(model_config, system, user, max_tokens):
            captured_text.append(user)
            return json.dumps({"create": False, "reason": "test"})

        monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)
        from ai_coding.core.skill_review import run_skill_review
        messages = [
            ChatMessage(role="user", content="创建 FastAPI 项目", timestamp="t1"),
            ChatMessage(role="tool", content="Exit code: 0", timestamp="t2", tool_call_id="c1"),
            ChatMessage(role="tool", content="  \n  ", timestamp="t3", tool_call_id="c2"),
            ChatMessage(role="assistant", content="项目创建成功", timestamp="t4"),
        ]
        await run_skill_review(messages, [], _cfg(), tmp_path)
        assert len(captured_text) == 1
        # The noise tool results should not appear in the prompt
        assert "Exit code: 0" not in captured_text[0]
