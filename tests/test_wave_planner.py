"""TDD (r24/C1): wave grouping — where the plan's context resets go."""

from ai_coding.core.wave_planner import (
    Wave,
    group_waves,
    is_heavy_verify,
    ready_in_wave,
    wave_is_open,
)
from ai_coding.domain.task import TaskState


def _task(*contents: str, deps: dict[str, list[str]] | None = None) -> TaskState:
    task = TaskState(goal="g")
    for content in contents:
        item = task.add(content)
        for dep in (deps or {}).get(item.id, []):
            item.depends_on.append(dep)
    return task


def test_empty_and_all_terminal_tasks_have_no_waves():
    assert group_waves(TaskState()) == []

    task = _task("a", "b")
    task.update("1", status="completed")
    task.update("2", status="dropped")
    assert group_waves(task) == []


def test_dependency_chain_stays_in_one_wave():
    task = _task("a", "b", "c", deps={"2": ["1"], "3": ["2"]})
    waves = group_waves(task)
    assert len(waves) == 1
    assert waves[0].ids == ("1", "2", "3")


def test_independent_items_are_capped_by_size():
    task = _task(*[f"item {i}" for i in range(6)])
    waves = group_waves(task, size=4)
    assert [wave.ids for wave in waves] == [("1", "2", "3", "4"), ("5", "6")]


def test_completed_items_do_not_consume_wave_slots():
    task = _task("done", "a", "b")
    task.update("1", status="completed")
    waves = group_waves(task, size=2)
    assert [wave.ids for wave in waves] == [("2", "3")]


def test_shared_declared_files_keep_independent_items_together():
    task = TaskState(goal="g")
    one = task.add("edit parser", files=["app/parser.py"])
    two = task.add("edit parser tests", files=["app/parser.py"])
    three = task.add("unrelated", files=["docs/readme.md"])

    waves = group_waves(task, size=2)
    assert [wave.ids for wave in waves] == [(one.id, two.id), (three.id,)]
    assert waves[0].files == ("app/parser.py",)


def test_the_cut_moves_left_to_keep_one_file_in_one_wave():
    """The item after the cap edits the same file as the item before it.

    Cutting there would make the next session re-read the file the previous
    one just changed, so the boundary moves one item left.
    """
    task = TaskState(goal="g")
    task.add("edit a", files=["a.py"])
    task.add("edit b", files=["b.py"])
    task.add("finish b", files=["b.py"])

    waves = group_waves(task, size=2)
    assert [wave.ids for wave in waves] == [("1",), ("2", "3")]


def test_a_single_item_wave_is_never_stripped_for_affinity():
    task = TaskState(goal="g")
    task.add("edit a", files=["a.py"])
    task.add("edit a again", files=["a.py"])

    assert [wave.ids for wave in group_waves(task, size=1)] == [("1",), ("2",)]


def test_heavy_verify_closes_its_wave():
    task = TaskState(goal="g")
    task.add("write feature")
    task.add("run the suite", verify="pytest tests/ -q")
    task.add("write docs")

    waves = group_waves(task)
    assert [wave.ids for wave in waves] == [("1", "2"), ("3",)]


def test_heavy_verify_detection():
    assert is_heavy_verify("pytest tests/ -q")
    assert is_heavy_verify("npm run test -- --watch=false")
    assert is_heavy_verify("cd web && pnpm test")
    assert not is_heavy_verify("python -c 'import app'")
    assert not is_heavy_verify("")


def test_size_splits_a_long_dependency_chain_in_order():
    task = _task(
        "a", "b", "c", "d", "e",
        deps={"2": ["1"], "3": ["2"], "4": ["3"], "5": ["4"]},
    )
    waves = group_waves(task, size=2)
    assert [wave.ids for wave in waves] == [("1", "2"), ("3", "4"), ("5",)]
    # Order inside every wave is the task list order.
    assert [item.id for wave in waves for item in wave.items] == ["1", "2", "3", "4", "5"]


def test_size_zero_falls_back_to_the_default():
    task = _task("a", "b")
    assert [wave.ids for wave in group_waves(task, size=0)] == [("1", "2")]


def test_ready_in_wave_honours_dependencies_and_skips():
    task = _task("a", "b")
    task.get("2").depends_on.append("1")
    task.update("1", status="completed")
    wave = group_waves(task)[0]
    assert ready_in_wave(task, wave).id == "2"

    task.update("2", status="skipped")
    assert ready_in_wave(task, wave) is None
    assert wave_is_open(task, wave) is False


def test_skipped_dependency_makes_the_dependent_not_ready():
    task = _task("a", "b")
    task.get("2").depends_on.append("1")
    task.update("1", status="skipped")
    wave = Wave(index=0, items=(task.get("1"), task.get("2")))
    assert ready_in_wave(task, wave) is None
    assert wave_is_open(task, wave) is True
