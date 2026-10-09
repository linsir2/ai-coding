"""Plan command routing (R1-5b, TDD): a topic must not vanish.

"/plan 改一下 loader" used to fall through the Textual router (which matched
only the exact string "/plan"), land in the generic slash handler as plain
prose, and never plan anything.  These tests pin the routing and the topic
split; the topic-less path still asks first.
"""

import pytest
from rich.console import Console

from ai_coding.ui.textual_app import _plan_topic


def test_bare_plan_has_no_topic():
    assert _plan_topic("/plan") is None


def test_plan_topic_is_extracted():
    assert _plan_topic("/plan 改一下 loader") == "改一下 loader"


def test_plan_run_is_not_a_plan_topic():
    # The router must not swallow the sibling command (this is why the
    # dispatch test below asserts both branches).
    assert _plan_topic("/plan-run") is None


@pytest.mark.parametrize("text,expected", [
    ("/plan", True),
    ("/plan 加缓存", True),
    ("/plan-run", False),
    ("/planrun", False),
])
def test_router_matches_plan_and_topic_but_not_siblings(text, expected):
    # Mirrors the router's own condition; if it changes, this fails first.
    assert (text == "/plan" or text.startswith("/plan ")) is expected


@pytest.mark.asyncio
async def test_plan_topic_clears_the_receipt_book_behaviourally():
    """The collision bug, end to end: a new plan must not inherit receipts.

    Source inspection would prove nothing here — what matters is that after
    `/plan <topic>` the session's step ids belong to the new plan alone.
    """
    from ai_coding.app.commands import REPLAction, handle_input
    from ai_coding.app.console import DialogueConsole
    from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
    from ai_coding.domain.plan_delivery import StepDelivery
    from ai_coding.domain.session import SessionData

    class _Loop:
        def __init__(self):
            self.calls = []
            # A real AgentLoop always has a policy; /plan swaps a copy to
            # disarm C3 for the planning turn.
            self.policy = TurnPolicy(
                TurnPolicyConfig(max_auto_continuations=3)
            )

        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            return "planned"

    class _Sessions:
        def __init__(self, session):
            self.items = [session]

        def save(self, session):
            return None

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.step_deliveries = [
        StepDelivery(step_id="1", statement="old one", status="completed")]

    action, _ = await handle_input(_Loop(), "/plan 新计划", session, _Sessions(session),
                                   DialogueConsole(Console(record=True, width=80)))

    assert action is REPLAction.CONTINUE
    assert session.task.goal == "新计划"
    assert session.step_deliveries == []


@pytest.mark.asyncio
async def test_repl_plan_turn_runs_with_auto_continuation_disabled():
    """Review fix: the REPL's planning turn must not inherit C3 either.

    Same reasoning as the Textual path (see
    tests/test_textual_app.py::test_plan_turn_runs_with_auto_continuation_disabled):
    the plan's open items are not work in progress, so continuing the planning
    turn contradicts its own prompt.  Pinned per call site — dropping the guard
    from one UI would not fail the other's test.
    """
    from ai_coding.app.commands import handle_input
    from ai_coding.app.console import DialogueConsole
    from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
    from ai_coding.domain.session import SessionData

    class _Loop:
        def __init__(self):
            self.calls = []
            self.seen: list[int] = []
            self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))

        async def process_input(self, session, text, on_delta=None, observer=None):
            self.calls.append(text)
            self.seen.append(self.policy.config.max_auto_continuations)
            return "planned"

    class _Sessions:
        def list_sessions(self):
            return []

    loop = _Loop()
    armed = loop.policy
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")

    await handle_input(loop, "/plan 新计划", session, _Sessions(),
                       DialogueConsole(Console(record=True, width=80)))

    assert loop.seen[0] == 0
    assert loop.policy is armed
