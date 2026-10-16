"""TaskDriver skeleton (11-spec §5): the generic armed-task round loop.

Driven by a recording fake profile, so the control flow is pinned without
any plan machinery: delegation order, the round cap, the early stop, the
taskless early exit, the armed re-check, and "persist+finish exactly once".

（audit addendum）`before_round` only fires once an item was picked — the
expected log below is the contract; the armed re-check is the T7 safety
net (a disarm between rounds stops the loop).
"""

import pytest

from ai_coding.core.task_driver import TaskDriver


class _Item:
    def __init__(self, item_id: str) -> None:
        self.id = item_id


class _Profile:
    def __init__(self, items: list[_Item], cap: int = 3, stop_after: str = "") -> None:
        self.items = items
        self.cap = cap
        self.stop_after = stop_after
        self.log: list[str] = []
        self.report: dict = {"used": 0, "stopped": None}

    def new_report(self):
        self.log.append("new_report")
        return self.report

    def max_rounds(self) -> int:
        return self.cap

    def prepare(self, session, task) -> set[str]:
        self.log.append("prepare")
        return set()

    def before_round(self, task, session, report, receipted) -> None:
        self.log.append("before_round")

    def next_item(self, task, attempted: set[str]):
        for item in self.items:
            if item.id not in attempted:
                return item
        return None

    def no_item(self, task, report) -> None:
        self.log.append("no_item")
        if not report["stopped"]:
            report["stopped"] = "empty"

    def rounds_used(self, report) -> int:
        return report["used"]

    def budget_stop(self, report, cap: int) -> None:
        self.log.append("budget_stop")
        report["stopped"] = f"budget:{cap}"

    async def run_round(self, session, task, item, report, console, *, on_delta, observer) -> None:
        self.log.append(f"round:{item.id}")

    def after_round(self, session, task, report) -> None:
        self.log.append("after_round")
        report["used"] += 1

    def should_stop(self, report, item) -> bool:
        return bool(self.stop_after) and item.id == self.stop_after

    def taskless(self, report, console) -> None:
        self.log.append("taskless")

    def persist(self, session) -> None:
        self.log.append("persist")

    def finish(self, console, report) -> None:
        self.log.append("finish")


def _session(task):
    from types import SimpleNamespace

    return SimpleNamespace(task=task)


@pytest.mark.asyncio
async def test_the_loop_runs_every_item_in_order_then_finishes():
    profile = _Profile([_Item("1"), _Item("2")])

    report = await TaskDriver(None).run(_session(object()), profile)

    assert report is profile.report
    assert profile.log == [
        "new_report", "prepare", "before_round", "round:1", "after_round",
        "before_round", "round:2", "after_round", "no_item", "persist", "finish",
    ]
    assert report["used"] == 2


@pytest.mark.asyncio
async def test_the_round_cap_stops_before_a_third_round():
    profile = _Profile([_Item("1"), _Item("2"), _Item("3")], cap=1)

    report = await TaskDriver(None).run(_session(object()), profile)

    assert report["stopped"] == "budget:1"
    assert "round:2" not in profile.log and "round:3" not in profile.log
    assert profile.log[-2:] == ["persist", "finish"]


@pytest.mark.asyncio
async def test_should_stop_ends_the_loop_after_that_round():
    profile = _Profile([_Item("1"), _Item("2")], stop_after="1")

    await TaskDriver(None).run(_session(object()), profile)

    assert "round:1" in profile.log and "round:2" not in profile.log
    assert profile.log[-2:] == ["persist", "finish"]


@pytest.mark.asyncio
async def test_a_taskless_session_takes_the_early_exit():
    profile = _Profile([_Item("1")])

    await TaskDriver(None).run(_session(None), profile)

    assert profile.log == ["new_report", "taskless"]


@pytest.mark.asyncio
async def test_a_disarm_between_rounds_stops_the_loop():
    """11-spec §5.1 + audit addendum: the loop re-checks task.armed every
    round — the seam T7's Esc handling depends on."""
    from types import SimpleNamespace

    class _DisarmProfile(_Profile):
        def after_round(self, session, task, report) -> None:
            super().after_round(session, task, report)
            task.armed = False

    task = SimpleNamespace(armed=True, armed_profile="plan", rounds_run=0, resume_count=0)
    profile = _DisarmProfile([_Item("1"), _Item("2")])

    await TaskDriver(None).run(_session(task), profile)

    assert "round:1" in profile.log and "round:2" not in profile.log
    assert profile.log[-2:] == ["persist", "finish"]


@pytest.mark.asyncio
async def test_the_driver_disarms_on_every_stop_path():
    """11-spec §5.3: finished/capped/disarmed all end armed=False for the
    profile's persist to carry."""
    from types import SimpleNamespace

    task = SimpleNamespace(armed=True)
    profile = _Profile([_Item("1")])

    await TaskDriver(None).run(_session(task), profile)

    assert task.armed is False
