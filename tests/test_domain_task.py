"""TDD (R1a-1): TaskState / TodoItem — the orchestration layer's plan object."""

import pytest

from ai_coding.domain.task import Blockers, TaskState, TodoItem


def test_todo_item_defaults():
    item = TodoItem(id="1", content="do it")
    assert item.status == "pending"
    assert item.depends_on == []
    assert item.evidence is None
    assert item.is_terminal() is False


def test_taskstate_add_assigns_incrementing_ids():
    task = TaskState(goal="g")
    a = task.add("first")
    b = task.add("second")
    assert (a.id, b.id) == ("1", "2")
    assert task.total == 2


def test_add_with_depends_on():
    task = TaskState()
    a = task.add("a")
    b = task.add("b", depends_on=[a.id])
    assert b.depends_on == ["1"]


def test_update_status():
    task = TaskState()
    task.add("a")
    item = task.update("1", status="in_progress")
    assert item.status == "in_progress"
    assert task.done_count == 0


def test_update_unknown_id_raises_keyerror():
    task = TaskState()
    with pytest.raises(KeyError):
        task.update("nope", status="completed")


def test_update_invalid_status_raises_valueerror():
    task = TaskState()
    task.add("a")
    with pytest.raises(ValueError):
        task.update("1", status="done")


def test_update_records_evidence():
    task = TaskState()
    task.add("a")
    item = task.update("1", status="completed", evidence="pytest passed")
    assert item.evidence == "pytest passed"
    assert item.is_terminal() is True


def test_open_items_excludes_terminal():
    task = TaskState()
    task.add("a")
    task.add("b")
    task.update("2", status="dropped")
    assert [i.id for i in task.open_items()] == ["1"]


def test_is_complete_when_all_terminal():
    task = TaskState()
    task.add("a")
    task.update("1", status="completed")
    assert task.is_complete() is True
    assert TaskState().is_complete() is True


def test_focus_prefers_unblocked():
    task = TaskState()
    task.add("blocked one", depends_on=["2"])
    task.add("free one")
    assert task.focus() is not None
    assert task.focus().id == "2"


def test_focus_falls_back_to_first_open():
    task = TaskState()
    task.add("waiting on a ghost", depends_on=["99"])
    focus = task.focus()
    assert focus is not None and focus.id == "1"


def test_focus_none_when_complete():
    task = TaskState()
    task.add("a")
    task.update("1", status="completed")
    assert task.focus() is None


def test_render_shape_and_markers():
    task = TaskState(goal="重构编排层")
    task.add("读通 agent_loop.py")
    task.add("重写循环")
    task.update("2", status="in_progress")
    out = task.render()
    assert out.startswith("<task-state>")
    assert out.endswith("</task-state>")
    assert "目标: 重构编排层" in out
    assert "进度: 0/2" in out
    assert "[ ] 1. 读通 agent_loop.py" in out
    assert "[>] 2. 重写循环" in out


def test_render_marks_next_step():
    task = TaskState(goal="g")
    task.add("first")
    task.add("second")
    out = task.render()
    assert "下一步: 1. first" in out


def test_render_blocked_suffix():
    task = TaskState(goal="g")
    task.add("dep")
    task.add("needs dep", depends_on=["1"])
    task.update("2", status="blocked")
    out = task.render()
    assert "[!] 2. needs dep (blocked by 1)" in out


def test_render_empty_task():
    out = TaskState(goal="g").render()
    assert "(无任务项)" in out


def test_taskstate_to_dict_from_dict_roundtrip():
    task = TaskState(goal="g")
    task.add("a", depends_on=["9"])
    task.update("1", status="completed", evidence="proof")
    restored = TaskState.from_dict(task.to_dict())
    assert restored.goal == "g"
    assert restored.total == 1
    item = restored.get("1")
    assert item is not None
    assert item.status == "completed"
    assert item.depends_on == ["9"]
    assert item.evidence == "proof"


def test_from_dict_tolerates_missing_keys():
    task = TaskState.from_dict({})
    assert task.goal == "" and task.items == []
    # entries without an id are dropped rather than producing a broken item
    task = TaskState.from_dict({"items": [{"content": "x"}, "junk"]})
    assert task.items == []
    # an unknown status degrades to pending instead of raising
    task = TaskState.from_dict({"items": [{"id": "1", "content": "x", "status": "??"}]})
    assert task.get("1") is not None and task.get("1").status == "pending"


# ---- R9a: the dependency query (docs/r9-analysis.md 3.1) ----


def test_r9_blockers_are_empty_when_dependencies_are_terminal():
    task = TaskState()
    task.add("dep")
    task.add("next", depends_on=["1"])
    task.update("1", status="completed")
    assert task.blockers("2") == Blockers()
    assert task.blockers("2").any is False


def test_r9_blockers_list_open_dependencies():
    task = TaskState()
    task.add("dep")
    task.add("next", depends_on=["1"])
    blockers = task.blockers("2")
    assert blockers.open == ("1",)
    assert blockers.unknown == () and blockers.cycle == ()
    assert blockers.any is True


def test_r9_blockers_report_unknown_ids():
    task = TaskState.from_dict(
        {"goal": "g", "items": [{"id": "1", "content": "a", "dependsOn": ["9"]}]}
    )
    assert task.blockers("1").unknown == ("9",)
    assert task.blockers("1").open == ()


def test_r9_blockers_detect_a_legacy_cycle():
    task = TaskState.from_dict(
        {
            "goal": "g",
            "items": [
                {"id": "1", "content": "a", "dependsOn": ["2"]},
                {"id": "2", "content": "b", "dependsOn": ["1"]},
            ],
        }
    )
    assert task.blockers("1").cycle == ("1", "2", "1")
    assert task.blockers("2").cycle == ("2", "1", "2")


def test_r9_dependency_invariant_reports_unknown_self_and_forward():
    task = TaskState.from_dict(
        {
            "goal": "g",
            "items": [
                {"id": "1", "content": "forward", "dependsOn": ["2"]},
                {"id": "2", "content": "self", "dependsOn": ["2"]},
                {"id": "3", "content": "ghost", "dependsOn": ["99"]},
                {"id": "4", "content": "clean"},
            ],
        }
    )
    assert task.dependency_invariant() == ("1", "2", "3")


def test_r9_render_and_focus_are_unchanged():
    """The domain keeps rendering and choosing exactly as before (R1 semantics)."""
    task = TaskState(goal="g")
    task.add("dep")
    task.add("needs dep", depends_on=["1"])
    assert task.focus() is not None and task.focus().id == "1"
    task.update("2", status="blocked")
    assert "[!] 2. needs dep (blocked by 1)" in task.render()


def test_todo_verify_round_trips_and_stays_absent_when_empty():
    """A proof command rides the item (R1-1); items without one write no key."""
    task = TaskState(goal="g")
    plain = task.add("write docs")
    assert plain.verify == ""
    assert "verify" not in plain.to_dict()
    item = task.add("fix it")
    item.verify = "pytest tests/ -q"
    assert TaskState.from_dict(task.to_dict()).get("2").verify == "pytest tests/ -q"


def test_clear_drops_the_goal_with_the_list():
    """A goal with no items is a stale artifact, not a plan (R1-5b).

    Measured: a plan turn ended with todo_write(clear), leaving
    `<task-state>` advertising the session's opening question as the
    objective of a list that no longer existed.
    """
    task = TaskState(goal="这个项目最近一次提交干嘛的")
    task.add("a")
    task.clear()

    assert task.items == [] and task.goal == ""
    out = task.render()
    assert "这个项目最近一次提交干嘛的" not in out
    assert "(无任务项)" in out


def test_render_display_strips_only_the_envelope():
    """UI 正文去掉 <task-state> 标签：那是 prompt 格式，泄漏到界面是噪音。

    正文一字不差 —— 模型那份（render()）和人看的这份是同一内容。
    """
    task = TaskState(goal="重构编排层")
    task.add("读通 agent_loop.py")
    task.add("重写循环")
    task.update("2", status="in_progress")
    full = task.render().split("\n")
    shown = task.render_display()
    assert shown == "\n".join(full[1:-1])
    assert "<task-state>" not in shown
    assert "[ ] 1. 读通 agent_loop.py" in shown
    assert "[>] 2. 重写循环" in shown


def test_goal_phase_round_trips_only_when_set():
    """D-4: the model's completion/blocked claim rides the task file.

    Same optional-key discipline as armed/roundsRun: empty means the key is
    not written, so every pre-D-4 session file reads back byte-identically.
    """
    task = TaskState(goal="把 jest 迁到 vitest")
    task.add("步骤")
    task.goal_phase = "complete"

    data = task.to_dict()
    assert data["goalPhase"] == "complete"
    assert TaskState.from_dict(data).goal_phase == "complete"

    assert "goalPhase" not in TaskState(goal="g").to_dict()
    assert TaskState.from_dict({"goal": "g", "items": []}).goal_phase == ""


def test_tokens_used_round_trips_and_arm_goal_resets_it():
    """G1: the episode's token account — same discipline as roundsRun (written
    only when non-zero; a new episode starts a fresh account)."""
    task = TaskState(goal="g")
    task.rounds_run = 3
    task.resume_count = 2
    task.tokens_used = 12_345

    data = task.to_dict()
    assert data["tokensUsed"] == 12_345
    assert TaskState.from_dict(data).tokens_used == 12_345
    assert "tokensUsed" not in TaskState(goal="g").to_dict()

    task.arm_goal("新目标")
    assert task.tokens_used == 0
    assert task.rounds_run == 0 and task.resume_count == 0


def test_clear_keeps_the_goal_while_an_episode_is_armed():
    """G2 live found this: a goal round calling todo_write(clear) wiped the
    episode's objective -- the round prompts after it read ``目标：`` empty,
    and /goal resume would have refused with "没有可续跑的目标" for a goal
    that was still running.  While armed, the goal is the episode's, not the
    list's title (R1-5b's rule still holds for a bare, unarmed list)."""
    task = TaskState(goal="迁移 X")
    task.add("step")
    task.arm_goal("迁移 X")

    task.clear()

    assert task.items == []
    assert task.goal == "迁移 X", "the armed episode keeps its objective"
    assert task.armed is True
