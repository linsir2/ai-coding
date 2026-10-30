"""Structured handoff (R1-3, TDD): a sub-agent conclusion as an evidence packet.

A free-text conclusion forces the parent to parse prose for machine facts
(C1's parent hand-copied "25/25" out of a paragraph).  With ``output_schema``
the child is instructed to return JSON, the runner extracts it forgivingly,
and the packet carries ``facts`` + ``schema_valid`` — while the raw text is
never discarded (an hour of work must not die over a missing brace).
"""

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.events import BackgroundTaskDone
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.subagent import SubagentResult, extract_json_facts
from ai_coding.tools.base import ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.subagent_tool import SubAgentTool

SCHEMA = {"type": "object", "required": ["failed", "passed"]}


# ---- pure extraction (domain) ------------------------------------------------


def test_extract_plain_json_object():
    facts, valid, errors = extract_json_facts(
        '{"failed": ["t1"], "passed": 3}', SCHEMA
    )
    assert valid is True and errors == ()
    assert facts == {"failed": ["t1"], "passed": 3}


def test_extract_strips_code_fences():
    text = '```json\n{"failed": [], "passed": 25}\n```'
    facts, valid, _ = extract_json_facts(text, SCHEMA)
    assert valid is True and facts == {"failed": [], "passed": 25}


def test_extract_finds_json_inside_prose():
    text = 'Done. Result: {"failed": ["t9"], "passed": 24} cheers'
    facts, valid, _ = extract_json_facts(text, SCHEMA)
    assert valid is True and facts == {"failed": ["t9"], "passed": 24}


def test_extract_missing_required_names_them():
    facts, valid, errors = extract_json_facts('{"passed": 25}', SCHEMA)
    assert valid is False and facts is None
    assert any("failed" in error for error in errors)


def test_extract_tolerates_extra_keys():
    facts, valid, _ = extract_json_facts(
        '{"failed": [], "passed": 1, "note": "flaky env"}', SCHEMA
    )
    assert valid is True and facts is not None and facts["note"] == "flaky env"


def test_extract_rejects_non_object_top_level():
    facts, valid, errors = extract_json_facts('[1, 2]', SCHEMA)
    assert valid is False and facts is None and errors != ()


def test_extract_rejects_unparseable_text():
    facts, valid, errors = extract_json_facts("all green, no json here", SCHEMA)
    assert valid is False and facts is None and errors != ()


def test_extract_without_required_accepts_any_object():
    facts, valid, _ = extract_json_facts('{"anything": 1}', {"type": "object"})
    assert valid is True and facts == {"anything": 1}


# ---- runner plumbing ----------------------------------------------------------


class _Engine(ChatEngine):
    def __init__(self, text="ok"):
        self._text = text
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages)})
        return StepResult(text=self._text, finish_reason="stop")

    async def aclose(self):
        return None


def _runner(engine, **over):
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: engine,
        "build_tools": lambda wd: ToolRegistry(),
        "timeout_seconds": 600.0,
        "settings": AIConfig(stop_policy="natural"),
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


@pytest.mark.asyncio
async def test_schema_instruction_reaches_child_prompt():
    engine = _Engine('{"failed": [], "passed": 1}')
    await _runner(engine).run("run tests", output_schema=SCHEMA)

    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert any("failed" in c and "JSON" in c for c in contents)


@pytest.mark.asyncio
async def test_structured_conclusion_lands_on_packet():
    engine = _Engine('{"failed": ["t9"], "passed": 24}')
    result = await _runner(engine).run("run tests", output_schema=SCHEMA)

    assert result.status == "completed"
    assert result.facts == {"failed": ["t9"], "passed": 24}
    assert result.schema_valid is True
    assert result.schema_errors == ()


@pytest.mark.asyncio
async def test_broken_json_keeps_raw_work():
    engine = _Engine("all green, trust me")
    result = await _runner(engine).run("run tests", output_schema=SCHEMA)

    assert result.status == "completed"  # the work stands; only the shape failed
    assert result.conclusion == "all green, trust me"
    assert result.facts is None
    assert result.schema_valid is False
    assert result.schema_errors != ()


@pytest.mark.asyncio
async def test_no_schema_leaves_tristate_none():
    engine = _Engine("plain prose")
    result = await _runner(engine).run("run tests")

    assert result.facts is None
    assert result.schema_valid is None


# ---- tool surface --------------------------------------------------------------


class _CannedRunner:
    def __init__(self, result: SubagentResult) -> None:
        self.result = result
        self.run_calls: list[dict] = []
        self.spawn_calls: list[dict] = []

    async def run(self, task, name, **kwargs):
        self.run_calls.append({"task": task, "name": name, **kwargs})
        return self.result

    def spawn_background(self, task, name, **kwargs):
        self.spawn_calls.append({"task": task, "name": name, **kwargs})
        return "run-1"


def _ctx() -> ToolContext:
    return ToolContext(
        messages=(ChatMessage(role="user", content="parent view", timestamp="t"),),
        instructions="PARENT INSTRUCTIONS",
        workspace="/tmp/ws",
        session_id="sid",
    )


@pytest.mark.asyncio
async def test_tool_passes_schema_to_runner_and_reports_facts():
    result = SubagentResult(
        status="completed", conclusion='{"failed": [], "passed": 2}',
        facts={"failed": [], "passed": 2}, schema_valid=True,
    )
    tool = SubAgentTool(runner=_CannedRunner(result))

    out = await tool.execute({"task": "t", "output_schema": SCHEMA}, _ctx())

    assert out.success is True
    assert out.data["facts"] == {"failed": [], "passed": 2}
    assert out.data["schema_valid"] is True
    assert "facts:" in out.output and "passed=2" in out.output


@pytest.mark.asyncio
async def test_tool_invalid_schema_still_succeeds_with_warning():
    result = SubagentResult(
        status="completed", conclusion="prose only",
        facts=None, schema_valid=False, schema_errors=("missing required keys: failed",),
    )
    tool = SubAgentTool(runner=_CannedRunner(result))

    out = await tool.execute({"task": "t", "output_schema": SCHEMA}, _ctx())

    assert out.success is True  # shape failed, work did not
    assert out.data["schema_valid"] is False
    assert "prose only" in out.output  # raw kept
    assert "failed" in out.output  # the miss is named


@pytest.mark.asyncio
async def test_tool_rejects_oversize_schema_without_calling_runner():
    runner = _CannedRunner(SubagentResult(status="completed"))
    tool = SubAgentTool(runner=runner)

    big = {"type": "object", "required": ["x"], "description": "y" * 5000}
    out = await tool.execute({"task": "t", "output_schema": big}, _ctx())

    assert out.success is False
    assert out.error == "schema_too_large"
    assert runner.run_calls == []


@pytest.mark.asyncio
async def test_tool_rejects_non_dict_schema():
    runner = _CannedRunner(SubagentResult(status="completed"))
    tool = SubAgentTool(runner=runner)

    out = await tool.execute({"task": "t", "output_schema": "[1]"}, _ctx())

    assert out.success is False
    assert out.error == "schema_invalid"
    assert runner.run_calls == []


@pytest.mark.asyncio
async def test_tool_background_path_accepts_schema():
    runner = _CannedRunner(SubagentResult(status="completed"))
    tool = SubAgentTool(runner=runner)

    out = await tool.execute(
        {"task": "t", "background": True, "output_schema": SCHEMA}, _ctx()
    )

    assert out.success is True
    assert runner.spawn_calls[0]["output_schema"] == SCHEMA


# ---- ledger + background pointer --------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_packet_carries_facts(tmp_path):
    engine = _Engine('{"failed": [], "passed": 5}')
    result = await _runner(engine, ledger_root=tmp_path).run(
        "run tests", output_schema=SCHEMA
    )

    assert result.ledger_path is not None
    payload = json.loads(open(result.ledger_path, encoding="utf-8").read())
    assert payload["facts"] == {"failed": [], "passed": 5}
    assert payload["schema_valid"] is True


@pytest.mark.asyncio
async def test_completion_event_carries_ledger_pointer(tmp_path):
    engine = _Engine('{"failed": [], "passed": 5}')
    seen: list = []
    runner = _runner(engine, ledger_root=tmp_path)
    runner.spawn_background("run tests", name="probe", emit=seen.append,
                            output_schema=SCHEMA)
    for _ in range(200):
        rows = [r for r in runner.task_list() if r.status != "running"]
        if rows:
            break
        import asyncio
        await asyncio.sleep(0.05)

    assert len(seen) == 1 and isinstance(seen[0], BackgroundTaskDone)
    assert seen[0].ledger_path is not None
    payload = json.loads(open(seen[0].ledger_path, encoding="utf-8").read())
    assert payload["facts"] == {"failed": [], "passed": 5}



# ---- r25: the frozen return contract -----------------------------------------

TYPED = {
    "type": "object",
    "required": ["summary"],
    "properties": {
        "summary": {"type": "string"},
        "confidence": {"type": "string"},
        "notes": {"type": "string"},
    },
}


def test_a_declared_type_is_enforced_not_just_presence():
    facts, valid, errors = extract_json_facts('{"summary": 123}', TYPED)

    assert facts is None and valid is False
    assert "summary" in errors[0] and "string" in errors[0]
    assert "int" in errors[0]


def test_array_item_types_are_enforced():
    schema = {
        "type": "object",
        "required": ["key_files"],
        "properties": {
            "key_files": {"type": "array", "items": {"type": "string"}}
        },
    }

    facts, valid, errors = extract_json_facts('{"key_files": "a.py"}', schema)
    assert valid is False and "key_files" in errors[0] and "array" in errors[0]

    facts, valid, errors = extract_json_facts('{"key_files": ["a.py", 3]}', schema)
    assert valid is False and "index 1" in errors[0]


def test_undeclared_keys_stay_presence_only():
    """No ``properties`` in the schema means only required keys are checked."""
    facts, valid, _ = extract_json_facts('{"failed": [], "passed": "many"}', SCHEMA)
    assert valid is True and facts == {"failed": [], "passed": "many"}


def test_the_packet_wire_carries_its_version():
    payload = SubagentResult(status="completed").to_dict()

    assert payload["v"] == 1
    # Every field the old shape had is still there: additive only.
    assert {
        "status", "conclusion", "todos", "artifacts", "iterations", "workdir",
        "ledger_path", "error", "context_mode", "truncated", "exit_reason",
        "usage", "facts", "schema_valid", "schema_errors", "notes", "confidence",
    } <= set(payload)


@pytest.mark.asyncio
async def test_a_schema_call_gets_exactly_one_format_instruction():
    engine = _Engine('{"summary": "ok"}')
    await _runner(engine).run("look", output_schema=TYPED)

    text = "\n".join(m.content or "" for m in engine.requests[0]["messages"])
    assert "[response format" in text
    assert "JSON" in text
    # The Chinese protocol asks for prose lines after the JSON -- two mutually
    # exclusive instructions in one request.
    assert "不确定协议" not in text


@pytest.mark.asyncio
async def test_a_free_form_call_keeps_the_uncertainty_protocol():
    engine = _Engine("done")
    await _runner(engine).run("look")

    text = "\n".join(m.content or "" for m in engine.requests[0]["messages"])
    assert "不确定协议" in text
    assert "[response format" not in text


@pytest.mark.asyncio
async def test_optional_schema_keys_are_named_for_the_child():
    """The child never sees the schema object, so optional keys must be named."""
    engine = _Engine('{"summary": "ok"}')
    await _runner(engine).run("look", output_schema=TYPED)

    text = "\n".join(m.content or "" for m in engine.requests[0]["messages"])
    assert "Optional keys" in text
    assert "confidence" in text and "notes" in text


@pytest.mark.asyncio
async def test_declared_confidence_and_notes_land_on_the_packet():
    engine = _Engine('{"summary": "ok", "confidence": "low", "notes": "flaky"}')
    result = await _runner(engine).run("look", output_schema=TYPED)

    assert result.schema_valid is True
    assert result.confidence == "low"
    assert result.notes == "flaky"


@pytest.mark.asyncio
async def test_the_packet_keeps_the_raw_status_and_the_adapter_maps_it():
    """One contract, two adapters: the packet never rewrites ``status``.

    ``subagent`` maps it to its own error vocabulary; the research fan-out
    (tests/test_research_fanout.py) keeps ``timeout`` verbatim.  Pinning the
    subagent half here stops the two adapters drifting apart again.
    """
    timed_out = SubagentResult(status="timeout", error="child_timeout")
    tool = SubAgentTool(runner=_CannedRunner(timed_out))

    out = await tool.execute({"task": "t"}, _ctx())

    assert out.data["status"] == "timeout"
    assert out.data["error"] == "child_timeout"
    assert out.success is False


def test_a_wrong_optional_type_is_dropped_not_fatal():
    """One malformed optional field must not discard a good summary."""
    schema = {
        "type": "object",
        "required": ["summary"],
        "properties": {
            "summary": {"type": "string"},
            "findings": {"type": "array", "items": {"type": "string"}},
        },
    }
    facts, valid, errors = extract_json_facts(
        '{"summary": "ok", "findings": {"a": 1}}', schema
    )

    assert valid is False
    assert facts == {"summary": "ok"}
    assert "findings" in errors[0] and "array" in errors[0]


@pytest.mark.asyncio
async def test_the_trailer_carries_property_shapes():
    """Names alone let a real run return ``findings`` as an object."""
    engine = _Engine('{"summary": "ok"}')
    await _runner(engine).run("look", output_schema=TYPED)

    text = "\n".join(m.content or "" for m in engine.requests[0]["messages"])
    assert "summary: string" in text
    assert "confidence: string" in text and "notes: string" in text


def test_array_shapes_are_rendered_for_the_child():
    from ai_coding.core.research_fanout import RESEARCH_SCHEMA
    from ai_coding.core.subagent import _format_instruction

    text = _format_instruction(RESEARCH_SCHEMA)
    assert "summary: string" in text
    assert "key_files: string[]" in text
    assert "findings: string[]" in text
