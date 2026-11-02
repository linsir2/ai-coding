"""P2-C1: TaskRequest is a thin identity envelope around the P2-A payload."""

import dataclasses

import pytest

from ai_coding.domain.orchestration import TaskRequest
from ai_coding.domain.subagent import SubagentTask


def _task(**overrides):
    objective = overrides.pop("objective", "analyze the parser")
    return SubagentTask.from_objective(objective, **overrides)


def test_only_the_contract_fields_exist():
    fields = {field.name for field in dataclasses.fields(TaskRequest)}
    assert fields == {
        "id",
        "task",
        "name",
        "kind",
        "origin_session",
        "parent_span",
    }


def test_for_child_wraps_without_copying_the_payload():
    task = _task()
    request = TaskRequest.for_child(task, name="probe", origin_session="s1")

    assert request.task is task
    assert request.kind == "child"
    assert request.name == "probe"
    assert request.origin_session == "s1"
    assert request.id


def test_render_delegates_to_the_payload():
    task = _task()
    request = TaskRequest.for_child(task)
    assert request.render() == task.render()


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        TaskRequest(id="x", task=_task(), kind="inline")


def test_a_fanout_request_must_be_read_only_and_not_isolated():
    with pytest.raises(ValueError):
        TaskRequest(id="x", task=_task(), kind="fanout")
    with pytest.raises(ValueError):
        TaskRequest(
            id="x",
            task=_task(readonly=False, isolate=False),
            kind="fanout",
        )
    request = TaskRequest(
        id="x",
        task=_task(readonly=True, isolate=False, agent="explore"),
        kind="fanout",
    )
    assert request.kind == "fanout"


def test_an_empty_id_is_refused():
    with pytest.raises(ValueError):
        TaskRequest(id="", task=_task())


def test_to_dict_names_the_identity_and_does_not_flatten_the_payload():
    task = _task()
    payload = TaskRequest.for_child(task, name="probe").to_dict()
    assert payload["name"] == "probe"
    assert payload["kind"] == "child"
    assert payload["task"] == task.to_dict()
