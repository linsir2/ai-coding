"""TDD (C3): the wave handoff and the wave-boundary replan.

The handoff is frozen: mechanical facts (done/skipped/files/proofs) come from
the executor and the model contributes only interfaces/decisions/pitfalls/hint.
The replan rewrites what is left, asks a human, and restores the snapshot on
refusal or when nobody can approve.
"""

import asyncio
from types import SimpleNamespace

import pytest

from ai_coding.core.events import TaskUpdated
from ai_coding.core.plan_runner import PlanReport
from ai_coding.core.wave_planner import Wave
from ai_coding.core.wave_runner import WaveItem, WaveRunner
from ai_coding.domain.loop import TurnStatus
from ai_coding.domain.plan_wave import (
    WAVE_HANDOFF_HINT_MAX_CHARS,
    WAVE_HANDOFF_ITEM_MAX_CHARS,
    WAVE_HANDOFF_MAX_ITEMS,
    WaveHandoff,
)
from ai_coding.domain.task import TaskState
from ai_coding.security.user_prompt import UserAnswer
from ai_coding.tools.base import ToolContext
from ai_coding.tools.todo_write_tool import TodoWriteTool
from ai_coding.tools.wave_report_tool import WaveReportTool
from tests.test_wave_runner import _session_with, _StubLoop


class _Observer:
    def __init__(self):
        self.events = []
        self.wave_states = []

    def on_event(self, event):
        # The event carries the live task, so snapshot the transient fact now.
        if isinstance(event, TaskUpdated):
            self.wave_states.append(bool(getattr(event.task, "wave_active", False)))
        self.events.append(event)


class _Prompt:
    def __init__(self, answer: UserAnswer | None = None):
        self._answer = answer or UserAnswer(answered=False, reason="no_user")
        self.asked = 0
        self.details: list[str] = []

    async def ask(self, question, options=(), *, detail="", intent="", data=None):
        self.asked += 1
        self.details.append(detail)
        return self._answer


class _CancelOnTurn(_StubLoop):
    """The operator pressed Esc: the turn (and the wave/replan) is cancelled."""

    def __init__(self, actions=(), *, mutate_before_cancel: bool = False):
        super().__init__(actions)
        self._mutate = mutate_before_cancel

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        if self._mutate:
            session.task.update("2", status="dropped")
        raise asyncio.CancelledError()


class _Loop(_StubLoop):
    """Adds the C3 actions: report a handoff, edit the list in a replan turn."""

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        action = self._actions[0] if self._actions else "complete"
        if action.startswith("report") or action.startswith("replan"):
            self._actions.pop(0)
            session.turn_count += 1
            self.calls.append(text)
            self.sessions.append(session)
            if action.startswith("report"):
                await WaveReportTool().execute(
                    {
                        "interfaces": ["def f(): ..."],
                        "decisions": ["keep the schema frozen"],
                        "pitfalls": ["do not retry completed items"],
                        "next_hint": "start from the new interface",
                        "needs_replan": action == "report-needs-replan",
                    },
                    ToolContext(task=session.task),
                )
                if action != "report-only":
                    target = self._target(session.task, text)
                    if target is not None:
                        target.status = "completed"
            elif action == "replan-drop":
                session.task.update("2", status="dropped")
            elif action == "replan-add":
                session.task.add("new step")
            self.last_outcome = SimpleNamespace(
                status=TurnStatus.COMPLETED, text=action, usage=None
            )
            return "ok"
        return await super().process_input(
            session, text, on_delta=on_delta, observer=observer
        )


# ---- the frozen handoff contract --------------------------------------


def test_model_fields_are_capped_and_mechanical_facts_win():
    handoff = WaveHandoff.build(
        done=("1",),
        files=("a.py",),
        report={
            "files": ["evil.py"],
            "interfaces": ["iface"],
            "decisions": ["d"],
            "pitfalls": ["p"],
            "next_hint": "h",
        },
    )

    assert handoff.files == ("a.py",)
    assert handoff.interfaces == ("iface",)
    assert handoff.next_hint == "h"


def test_build_truncates_over_cap_entries_and_line_count():
    report = {
        "interfaces": ["x" * (WAVE_HANDOFF_ITEM_MAX_CHARS + 1)]
        + [f"i{index}" for index in range(WAVE_HANDOFF_MAX_ITEMS)],
        "next_hint": "y" * (WAVE_HANDOFF_HINT_MAX_CHARS + 1),
    }

    handoff = WaveHandoff.build(report=report)

    assert len(handoff.interfaces) == WAVE_HANDOFF_MAX_ITEMS
    assert all(len(line) <= WAVE_HANDOFF_ITEM_MAX_CHARS for line in handoff.interfaces)
    assert len(handoff.next_hint) <= WAVE_HANDOFF_HINT_MAX_CHARS


def test_render_is_trimmed_from_the_oldest_experience():
    handoff = WaveHandoff.build(
        done=("1",),
        files=("a.py",),
        report={
            "interfaces": ["i1", "i2", "i3"],
            "decisions": ["d1", "d2", "d3"],
            "pitfalls": ["p1", "p2", "p3"],
        },
    )

    text = handoff.render(max_chars=40)

    assert len(text) <= 40
    assert text.endswith("…")
    # The newest line survives longest; the mechanical facts never drop.
    assert "已完成：1" in text
    assert "p1" not in text


def test_handoff_round_trips_through_task_state():
    task = _session_with("a").task
    handoff = WaveHandoff.build(done=("1",), files=("a.py",), report={"interfaces": ["f"]})
    task.wave_handoff = handoff.to_dict()

    restored = TaskState.from_dict(task.to_dict())

    assert restored.wave_handoff == handoff.to_dict()
    assert WaveHandoff.from_dict(restored.wave_handoff).interfaces == ("f",)


def test_a_task_without_a_handoff_writes_no_key():
    task = TaskState(goal="g")
    task.add("a")

    assert "waveHandoff" not in task.to_dict()


# ---- the wave_report tool ---------------------------------------------


@pytest.mark.asyncio
async def test_wave_report_refuses_over_cap_input():
    task = TaskState(goal="g")
    task.armed_profile = "plan"
    task.wave_active = True
    tool = WaveReportTool()

    too_many = await tool.execute(
        {"interfaces": [f"i{index}" for index in range(WAVE_HANDOFF_MAX_ITEMS + 1)]},
        ToolContext(task=task),
    )
    too_long = await tool.execute(
        {"decisions": ["d" * (WAVE_HANDOFF_ITEM_MAX_CHARS + 1)]}, ToolContext(task=task)
    )
    ok = await tool.execute(
        {"interfaces": [f"i{index}" for index in range(WAVE_HANDOFF_MAX_ITEMS)]},
        ToolContext(task=task),
    )

    assert too_many.error == "invalid_report"
    assert too_long.error == "invalid_report"
    assert ok.success is True
    assert task.wave_report["interfaces"][0] == "i0"


@pytest.mark.asyncio
async def test_wave_report_is_only_callable_inside_a_wave():
    task = TaskState(goal="g")
    task.armed_profile = "plan"
    tool = WaveReportTool()

    result = await tool.execute({"interfaces": ["x"]}, ToolContext(task=task))

    assert result.success is False
    assert result.error == "not_in_wave"


# ---- the runner: wave lifetime and the handoff ------------------------


@pytest.mark.asyncio
async def test_wave_marks_itself_live_and_stores_the_structured_handoff():
    session = _session_with("a")
    loop = _Loop(["report-needs-replan"])
    observer = _Observer()

    report = await WaveRunner(loop).run(session, observer=observer)

    assert report.completed == ["1"]
    handoff = WaveHandoff.from_dict(session.task.wave_handoff)
    assert handoff.done == ("1",)
    assert handoff.interfaces == ("def f(): ...",)
    assert session.task.wave_note.startswith("已完成：1")
    assert session.task.wave_active is False
    assert session.task.wave_items == ()
    # The first emit says a wave is running; the last one says it ended.
    assert True in observer.wave_states
    assert observer.wave_states[-1] is False


@pytest.mark.asyncio
async def test_the_next_wave_seed_carries_the_structured_handoff_once():
    session = _session_with("a", "b")
    session.task.wave_handoff = WaveHandoff.build(
        done=("1",), files=("a.py",), report={"interfaces": ["def f(): ..."]}
    ).to_dict()
    loop = _Loop(["complete", "complete"])

    await WaveRunner(loop).run(session)

    # The seed is the wave session's first user message, not the per-item prompt.
    seed = loop.sessions[0].messages[0].content
    assert "[上一波交接]" in seed
    assert "接口：" in seed and "def f(): ..." in seed
    # It is injected once, at the wave head.
    assert seed.count("[上一波交接]") == 1
    # The model must be told to hand over, or the structured half stays empty
    # in natural runs (measured: real waves produced only the mechanical note).
    assert "wave_report" in seed
    assert "[上一波交接]" not in loop.calls[1]


# ---- the runner: wave-boundary replan --------------------------------


@pytest.mark.asyncio
async def test_deadlock_replans_and_runs_the_rewritten_list_after_approval():
    session = _session_with("a", "b", deps={"1": ["2"], "2": ["1"]})
    loop = _Loop(["replan-drop", "complete"])
    prompt = _Prompt(UserAnswer(answered=True, text="继续执行"))

    report = await WaveRunner(loop, user_prompt=prompt).run(session)

    assert report.replanned == 1
    assert report.completed == ["1"]
    assert session.task.get("2").status == "dropped"
    assert prompt.asked == 1
    assert "b" in prompt.details[0]


@pytest.mark.asyncio
async def test_a_rejected_replan_restores_the_snapshot_and_stops():
    session = _session_with("a", "b", deps={"1": ["2"], "2": ["1"]})
    loop = _Loop(["replan-drop", "complete"])
    prompt = _Prompt(UserAnswer(answered=True, text="停止计划"))

    report = await WaveRunner(loop, user_prompt=prompt).run(session)

    assert report.replanned == 1
    assert report.stopped.startswith("replan rejected")
    assert session.task.get("2").status == "pending"
    assert report.completed == []
    assert len(loop.calls) == 1


@pytest.mark.asyncio
async def test_without_a_prompt_channel_a_replan_is_never_applied_silently():
    session = _session_with("a", "b", deps={"1": ["2"], "2": ["1"]})
    loop = _Loop(["replan-drop", "complete"])

    report = await WaveRunner(loop).run(session)

    assert report.stopped.startswith("replan rejected")
    assert session.task.get("2").status == "pending"
    assert report.completed == []


@pytest.mark.asyncio
async def test_plan_max_replans_zero_disables_the_turn_entirely():
    session = _session_with("a", "b", deps={"1": ["2"], "2": ["1"]})
    loop = _Loop(["replan-drop", "complete"])
    loop.settings.plan_max_replans = 0
    prompt = _Prompt(UserAnswer(answered=True, text="继续执行"))

    report = await WaveRunner(loop, user_prompt=prompt).run(session)

    assert report.replanned == 0
    assert prompt.asked == 0
    assert loop.calls == []
    assert report.stopped.startswith("deadlock")


@pytest.mark.asyncio
async def test_needs_replan_runs_the_turn_before_the_next_wave():
    # Item 2 cannot run (unknown dependency 99), so the plan is not finished
    # when the wave asks for a replan.
    session = _session_with("a", "b", deps={"2": ["99"]})
    loop = _Loop(["report-needs-replan", "replan-noop"])
    prompt = _Prompt(UserAnswer(answered=True, text="停止计划"))

    report = await WaveRunner(loop, user_prompt=prompt).run(session)

    assert report.replanned == 1
    assert report.stopped.startswith("needs_replan")
    assert "没有修改" in report.stopped
    assert prompt.asked == 0


# ---- todo_write inside a replan ---------------------------------------


@pytest.mark.asyncio
async def test_todo_write_refuses_terminal_items_in_a_replan_turn():
    task = TaskState(goal="g")
    task.add("done")
    task.add("left")
    task.items[0].status = "completed"
    tool = TodoWriteTool()

    terminal = await tool.execute(
        {"action": "update", "id": "1", "content": "rewrite history"},
        ToolContext(task=task, replan=True),
    )
    complete = await tool.execute(
        {"action": "update", "id": "2", "status": "completed"},
        ToolContext(task=task, replan=True),
    )
    drop = await tool.execute(
        {"action": "update", "id": "2", "status": "dropped"},
        ToolContext(task=task, replan=True),
    )

    assert terminal.error == "replan_terminal_item"
    assert complete.error == "replan_not_allowed"
    assert drop.success is True
    assert task.items[0].content == "done"


# ---- cancellation and the replan turn's boundary ---------------------


@pytest.mark.asyncio
async def test_a_cancelled_wave_clears_the_live_wave_facts():
    """C3: Esc skips the wave-end block, but the transient facts must not
    survive -- the run's closing emit would advertise a wave that is gone."""
    session = _session_with("a")
    task = session.task
    task.armed = True
    task.armed_profile = "plan"
    runner = WaveRunner(_CancelOnTurn())

    with pytest.raises(asyncio.CancelledError):
        await runner._run_wave(
            session, task, Wave(index=0, items=(task.get("1"),)),
            PlanReport(), None, on_delta=None, observer=None,
        )

    assert task.wave_active is False
    assert task.wave_items == ()
    assert task.wave_report is None


@pytest.mark.asyncio
async def test_a_cancelled_replan_restores_the_unapproved_list():
    """C3: the replan turn may already have rewritten the list; a cancel is
    not an approval, so the snapshot goes back before the error propagates."""
    session = _session_with("a", "b")
    task = session.task
    task.armed = True
    task.armed_profile = "plan"
    runner = WaveRunner(_CancelOnTurn(mutate_before_cancel=True))
    item = WaveItem(id="replan-1", wave=Wave(index=0, items=()), replan="deadlock")

    with pytest.raises(asyncio.CancelledError):
        await runner._run_replan(
            session, task, item, PlanReport(), None, on_delta=None, observer=None,
        )

    assert [entry.status for entry in task.items] == ["pending", "pending"]


def test_the_replan_turn_cannot_touch_the_world():
    """C3: "only todo_write" is a boundary, not a prompt sentence.  The
    read-only posture keeps todo_write (non-mutating) and drops write/bash."""
    session = _session_with("a")
    runner = WaveRunner(_StubLoop([]))

    replan = runner._make_replan_session(session, session.task, "deadlock")

    assert replan.readonly is True
    assert replan.replan_mode is True
    assert replan.ephemeral is True


def test_a_standard_todo_write_still_edits_terminal_items():
    """The guard is the replan context's, not a new global rule."""
    task = TaskState(goal="g")
    task.add("a")
    task.items[0].status = "completed"

    result = TodoWriteTool()._update(
        {"id": "1", "content": "renamed"}, task, replan=False
    )

    assert result.success is True
    assert task.items[0].content == "renamed"
