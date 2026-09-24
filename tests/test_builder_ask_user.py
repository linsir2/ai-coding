"""ask_user is registered exactly where somebody can answer it (R6c).

The tool list is the first harness: a sub-agent has no human, and neither has the
one-shot ``ask`` command, so neither should advertise a question they cannot answer
(docs/r6-analysis.md 2.3).
"""

from __future__ import annotations

from ai_coding.config.models import AppConfig
from ai_coding.security.user_prompt import UnavailableUserPromptCallback
from ai_coding.tools.builder import build_tool_registry


def _cfg() -> AppConfig:
    return AppConfig()


def test_ask_user_is_registered_when_a_human_can_answer():
    registry = build_tool_registry(
        _cfg(), ".", None, user_prompt=UnavailableUserPromptCallback()
    )
    assert registry.get("ask_user") is not None


def test_ask_user_is_absent_without_a_prompt_channel():
    assert build_tool_registry(_cfg(), ".", None).get("ask_user") is None


def test_ask_user_can_be_switched_off_by_config():
    cfg = _cfg()
    cfg.tools.ask_user.enabled = False
    registry = build_tool_registry(
        cfg, ".", None, user_prompt=UnavailableUserPromptCallback()
    )
    assert registry.get("ask_user") is None


def test_subagent_stacks_never_get_ask_user():
    registry = build_tool_registry(
        _cfg(),
        ".",
        None,
        include_subagent=False,
        user_prompt=UnavailableUserPromptCallback(),
    )
    assert registry.get("ask_user") is None
