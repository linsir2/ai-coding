"""TDD (M1-1): OpenAI Agents SDK adapter — offline-verifiable construction only.

The network path (execute_turn) is intentionally NOT unit-tested; it is exercised
manually via the ``ask`` CLI with a real API key.
"""

from ai_coding.ai.agents_sdk_service import AgentsSDKChatService
from ai_coding.domain.model_config import ModelConfig


def _service() -> AgentsSDKChatService:
    return AgentsSDKChatService(
        ModelConfig(name="gpt-test", base_url="https://example.test/v1", api_key="k")
    )


def test_build_agent_name_and_model():
    svc = _service()
    agent = svc._build_agent()
    assert agent.name == "gpt-test"
    assert agent.instructions  # non-empty default instructions


def test_build_agent_uses_sdk_model_wrapper():
    svc = _service()
    agent = svc._build_agent()
    # the agent must carry a real OpenAI-compatible model, not the raw string
    assert type(agent.model).__name__ in {
        "OpenAIChatCompletionsModel",
        "OpenAIResponsesModel",
    }
