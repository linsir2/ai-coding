"""/plan refuses to start when its target lives outside the workspace."""

from types import SimpleNamespace

import pytest

from ai_coding.core.plan_runner import outside_workspace_paths, plan_only
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.session import SessionData


def test_finds_the_real_target(tmp_path):
    text = "把项目放到 /home/linsir365/projects/awesome-tool-kit/hotel 里并跑 pytest"
    assert outside_workspace_paths([text], str(tmp_path)) == [
        "/home/linsir365/projects/awesome-tool-kit/hotel"
    ]


def test_ignores_routes_urls_and_inside_paths(tmp_path):
    text = (
        f"GET /rooms 与 /health；参考 https://x.dev/home/a/b；"
        f"项目在 {tmp_path}/app；另见 /etc/passwd"
    )
    assert outside_workspace_paths([text], str(tmp_path)) == ["/etc/passwd"]


class _StubLoop:
    def __init__(self, workspace, *, items=()):
        self.workspace = workspace
        self.settings = SimpleNamespace(
            plan_step_max_iterations=8, plan_max_step_turns=0, stop_policy="todo_aware"
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self.calls = []
        self._items = tuple(items)

    async def process_input(self, session, text, on_delta=None, observer=None):
        self.calls.append(text)
        session.begin_plan("t")
        for content in self._items:
            session.task.add(content)
        return "planned"


def _session():
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_outside_topic_stops_before_any_planning(tmp_path):
    loop = _StubLoop(str(tmp_path))
    report = await plan_only(loop, _session(), topic="把项目建到 /home/x/y")
    assert report.stopped is not None and "/home/x/y" in report.stopped
    assert loop.calls == []


@pytest.mark.asyncio
async def test_outside_plan_item_stops_before_execution(tmp_path):
    loop = _StubLoop(str(tmp_path), items=("写代码到 /home/x/y/app.py",))
    report = await plan_only(loop, _session(), topic="实现一个小系统")
    assert report.stopped is not None and "/home/x/y/app.py" in report.stopped
    assert len(loop.calls) == 1  # planned once; the caller never runs it


def test_a_configured_extra_root_is_not_reported_as_outside(tmp_path):
    """Regression (B, found while adding D5): the warning asked "can the file
    tools reach this?", and answered with a Sandbox built from the workspace
    alone.  A directory the operator declared in ``tools.extra_workspace_roots``
    is reachable, so reporting it as unreachable told the model to switch
    workspaces for a path that already works."""
    other = tmp_path / "sibling"
    other.mkdir()
    text = f"看下 {other}/app.py"

    assert outside_workspace_paths([text], str(tmp_path), extra_roots=(other,)) == []
