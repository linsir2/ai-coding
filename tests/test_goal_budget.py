"""G1 (D-4 v2): the goal episode's token budget — configured and honored.

Two real defects stand behind this file: the budget itself (v1 filed it for
v2), and the *phantom* beside it — ``goal_max_rounds`` was read duck-typed
while ``AIConfig`` had no such field and is ``extra="ignore"``, so a yaml key
for it was silently swallowed.
"""

from types import SimpleNamespace

import pytest

from ai_coding.config.loader import ConfigLoader
from ai_coding.config.models import AIConfig
from ai_coding.core.goal_runner import GoalRunner
from ai_coding.core.turn_policy import TurnPolicy
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.task import TaskState


def test_goal_budget_defaults():
    cfg = AIConfig()
    assert cfg.goal_max_rounds == 20
    assert cfg.goal_token_budget == 0  # off by default: opt-in, like every cap


def test_goal_budget_keys_load_from_yaml():
    cfg = ConfigLoader.app_from_doc(
        {"ai": {"goal_max_rounds": 5, "goal_token_budget": 1234}}
    )
    assert cfg.ai.goal_max_rounds == 5
    assert cfg.ai.goal_token_budget == 1234


@pytest.mark.asyncio
async def test_the_runner_reads_the_real_config():
    """The phantom, closed: this loop's settings is a real ``AIConfig``; with
    the field missing it silently fell back to 20 and ran 20 rounds."""
    class _Loop:
        def __init__(self) -> None:
            self.settings = AIConfig(goal_max_rounds=2, goal_token_budget=0)
            self.policy = TurnPolicy()
            self.closer = SimpleNamespace(save=lambda session: None)
            self.calls: list[str] = []

        async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
            self.calls.append(text)
            session.messages.append(
                ChatMessage(role="tool", content="ok", timestamp="", tool_call_id="c1")
            )
            return "done"

    loop = _Loop()
    session = SimpleNamespace(task=TaskState(goal="g"), messages=[], session_id="s", open_turn=None)

    report = await GoalRunner(loop).run(session)

    assert report.rounds == 2, "the AIConfig cap is honored"
    assert report.stop == "budget"
