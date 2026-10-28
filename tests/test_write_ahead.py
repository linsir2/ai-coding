"""M-C1: write-ahead intent + result reuse (TDD, red first).

A ``mutates_world`` call is recorded in a sidecar *before* it runs (status
``running``) and settled *after* (``ok``/``failed``, with its output/data/
artifacts).  A call the model regenerates -- same ``(tool, digest)`` -- inside
the same turn is answered from the record instead of running twice.  The reuse
domain is the turn: the sidecar is cleared when the turn closes, so a later
turn runs it again.  The sidecar never goes through ``TurnCloser.save``, which
would drop the in-flight tool call.
"""

from __future__ import annotations

import json

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.guard import call_fingerprint
from ai_coding.core.write_ahead import WriteAheadLog
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolArtifact, ToolCall, ToolCallRef, ToolResult
from ai_coding.domain.write_ahead import WriteAheadIntent
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


class _ScriptEngine(ChatEngine):
    """Replays a script of tool-call batches, then answers with text.

    The script restarts at every new user message, so one engine can drive
    several turns (the reuse domain is the turn, not the engine).
    """

    def __init__(self, batches: list[list[tuple[str, str, str]]]) -> None:
        self._batches = [list(batch) for batch in batches]
        self._index = 0
        self._user_messages = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        users = sum(1 for m in messages if getattr(m, "role", "") == "user")
        if users > self._user_messages:
            self._user_messages = users
            self._index = 0
        if self._index < len(self._batches):
            batch = self._batches[self._index]
            self._index += 1
            if batch:
                return StepResult(
                    text=None,
                    tool_calls=[
                        ToolCallRef(id=call_id, name=name, arguments=args)
                        for call_id, name, args in batch
                    ],
                    finish_reason="tool_calls",
                )
        return StepResult(text="done", finish_reason="stop")


class _WriteTool(BaseTool):
    """A mutating tool that counts executions and can probe the sidecar."""

    def __init__(self, name: str = "write", *, probe=None) -> None:
        self._name = name
        self._probe = probe
        self.executions = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "fake mutating tool"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        self.executions += 1
        if self._probe is not None:
            self._probe(params, ctx)
        return ToolResult(
            success=True,
            # Numbered per execution: a reuse must hand back the *recorded*
            # output ("written #1"), not a fresh run's ("written #2").
            output=f"written #{self.executions}",
            data={"path": "a.py"},
            artifacts=(ToolArtifact(path="a.py", kind="write"),),
        )


def _loop(engine, service, workspace, tool) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentLoop(
        engine,
        service,
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(stop_policy="natural", turn_timeout_seconds=0),
        workspace=str(workspace),
    )


def _session(service: SessionService):
    session = service.create(title="s")
    service.save(session)
    return session


def _records(service: SessionService, session_id: str):
    return service._read_write_ahead(session_id)


class _ExplodingEngine(ChatEngine):
    """Runs one batch, then raises an exception the turn body does not name."""

    def __init__(self, batches: list[list[tuple[str, str, str]]]) -> None:
        self._inner = _ScriptEngine(batches)
        self._steps = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self._steps += 1
        if self._steps > 1:
            raise RuntimeError("boom")
        return await self._inner.step(
            messages, tools=tools, instructions=instructions, on_delta=on_delta
        )


# ---- the claim happens before the call runs --------------------------------


@pytest.mark.asyncio
async def test_a_side_effect_tool_is_claimed_before_it_runs(tmp_path):
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    seen: dict = {}

    def probe(_params, _ctx):
        seen["records"] = _records(service, session.session_id)

    tool = _WriteTool(probe=probe)
    loop = _loop(
        _ScriptEngine([[("c1", "write", '{"path":"a.py"}')]]),
        service,
        tmp_path,
        tool,
    )

    await loop.process_input(session, "go")

    records = seen["records"]
    assert records is not None and len(records) == 1
    assert records[0].status == "running"
    assert records[0].tool == "write"
    assert records[0].call_id == "c1"


# ---- the outcome is settled after the call ---------------------------------


@pytest.mark.asyncio
async def test_the_outcome_is_settled_after_the_call(tmp_path):
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    seen: dict = {}

    class _Probe(ChatEngine):
        def __init__(self):
            self._n = 0

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self._n += 1
            if self._n == 1:
                return StepResult(
                    text=None,
                    tool_calls=[ToolCallRef(id="c1", name="write", arguments='{"path":"a.py"}')],
                    finish_reason="tool_calls",
                )
            seen["records"] = _records(service, session.session_id)
            return StepResult(text="done", finish_reason="stop")

    loop = _loop(_Probe(), service, tmp_path, _WriteTool())
    await loop.process_input(session, "go")

    record = seen["records"][0]
    assert record.status == "ok"
    assert record.output == "written #1"
    assert record.data == {"path": "a.py"}
    assert [a["path"] for a in record.artifacts] == ["a.py"]


# ---- a live turn re-runs the call; only a resume reuses --------------------


@pytest.mark.asyncio
async def test_a_regenerated_call_in_a_live_turn_runs_again(tmp_path):
    """The record is crash recovery, not a same-turn call cache.

    Reusing inside the turn that wrote the record is how the model came to read
    a file it had just edited, see the pre-edit bytes, and conclude the edit had
    failed (eval-08 stage F: ten ``cat`` calls, identical output and duration).
    """
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    tool = _WriteTool()
    engine = _ScriptEngine(
        [
            [("c1", "write", '{"path":"a.py"}')],
            [("c2", "write", '{"path":"a.py"}')],
        ]
    )
    loop = _loop(engine, service, tmp_path, tool)

    await loop.process_input(session, "go")

    assert tool.executions == 2, "a live turn must really run the call again"


# ---- the reuse carries the data and artifacts ------------------------------


@pytest.mark.asyncio
async def test_the_reuse_carries_the_data_and_artifacts(tmp_path):
    """A resumed reuse has to be indistinguishable from a fresh run."""
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    call = ToolCall(id="c0", name="write", params={"path": "a.py"})
    service.mark_write_ahead(
        session.session_id,
        [
            WriteAheadIntent(
                call_id="c0",
                tool="write",
                digest=call_fingerprint(call),
                status="ok",
                output="written #1",
                data={"path": "a.py"},
                artifacts=[{"path": "a.py", "kind": "write"}],
            ).to_dict()
        ],
    )
    reloaded = service.load(session.session_id)
    assert reloaded is not None
    reloaded.open_turn = 1

    tool = _WriteTool()
    loop = _loop(
        _ScriptEngine([[("c2", "write", '{"path":"a.py"}')]]), service, tmp_path, tool
    )
    await loop.resume_turn(reloaded)

    tool_messages = {m.tool_call_id: m for m in reloaded.messages if m.role == "tool"}
    reused = tool_messages["c2"]
    # The reuse hands back the *recorded* output, not a fresh run's.
    assert reused.content is not None and "written #1" in reused.content
    assert "written #2" not in reused.content
    assert "<data>" in reused.content
    assert "a.py" in reused.content
    # A reuse is marked so a trace can tell it from a fresh run.
    assert '"replayed": true' in reused.content
    assert [a.path for a in reused.artifacts] == ["a.py"]


@pytest.mark.asyncio
async def test_a_live_turn_cannot_loop_on_the_same_call(tmp_path):
    """The other half of R4: once a live call really runs, the guard sees it.

    Before this change a repeated ``bash`` was answered from the record, so
    ``DuplicateCallGuard.after`` never ran and the third identical call was
    allowed -- eval-08 stage F made ten of them.  Now the third is refused.
    """
    from types import SimpleNamespace

    from ai_coding.config.models import GuardConfig
    from ai_coding.core.dispatch import ToolDispatcher
    from ai_coding.core.guard import build_guards
    from ai_coding.security.permission_gate import PermissionGate
    from ai_coding.tools.base import ToolContext

    tool = _WriteTool()
    registry = ToolRegistry()
    registry.register(tool)
    dispatcher = ToolDispatcher(
        registry,
        PermissionGate(),
        AutoApproveCallback(),
        guards=build_guards(SimpleNamespace(guard=GuardConfig(duplicate_threshold=2))),
    )
    ref = ToolCallRef(id="c1", name="write", arguments='{"path":"a.py"}')
    ctx = ToolContext(workspace=str(tmp_path))

    outcomes = []
    for _ in range(3):
        outcomes.extend(await dispatcher.dispatch_batch([ref], ctx=ctx))

    assert tool.executions == 2, "the third identical call must not run"
    assert outcomes[-1].denied, "the third identical call must be refused"
    assert "already succeeded" in (outcomes[-1].result.output or "")


# ---- the reuse domain is the turn, not the session -------------------------


@pytest.mark.asyncio
async def test_a_later_turn_does_not_reuse_a_previous_turns_call(tmp_path):
    """Prove: the sidecar is cleared at turn close, so a later turn runs again."""
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    tool = _WriteTool()
    loop = _loop(_ScriptEngine([[("c1", "write", '{"path":"a.py"}')]]), service, tmp_path, tool)

    await loop.process_input(session, "go")
    await loop.process_input(session, "again")

    assert tool.executions == 2, "the sidecar leaked into the next turn"


@pytest.mark.asyncio
async def test_an_aborted_turn_leaves_no_records_for_the_next_one(tmp_path):
    """The reuse domain is the turn even when the turn dies badly.

    The turn body names only a few exceptions; anything else propagates without
    ``close``.  Without the ``finally``, the next turn in the same process
    would start from this one's records -- crash recovery turned into a
    same-process call cache.
    """
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    tool = _WriteTool()
    loop = _loop(_ExplodingEngine([[("c1", "write", "{}")]]), service, tmp_path, tool)

    with pytest.raises(RuntimeError):
        await loop.process_input(session, "go")

    assert tool.executions == 1
    assert _records(service, session.session_id) is None, (
        "an aborted turn left its records behind"
    )


@pytest.mark.asyncio
async def test_a_crashed_turns_settled_call_is_reused_after_a_restart(tmp_path):
    """The point of the whole mechanism: the process died with the call's
    outcome on the sidecar, so the resumed turn must not run it again.

    The recovery entry point is ``resume_turn``, not ``process_input``: only a
    turn that is *continued* after a crash may answer from the record.  A live
    turn must really run its calls (eval-08 stage F: ten replayed ``cat`` calls
    showed the pre-edit file and the model concluded its edit had failed).
    """
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    call = ToolCall(id="c0", name="write", params={"path": "a.py"})
    service.mark_write_ahead(
        session.session_id,
        [
            WriteAheadIntent(
                call_id="c0",
                tool="write",
                digest=call_fingerprint(call),
                status="ok",
                output="written #1",
                data={"path": "a.py"},
                artifacts=[{"path": "a.py", "kind": "write"}],
            ).to_dict()
        ],
    )
    reloaded = service.load(session.session_id)
    assert reloaded is not None
    # A turn that never closed is what makes it resumable.
    reloaded.open_turn = 1

    tool = _WriteTool()
    loop = _loop(
        _ScriptEngine([[("c1", "write", '{"path":"a.py"}')]]), service, tmp_path, tool
    )
    await loop.resume_turn(reloaded)

    assert tool.executions == 0, "the crashed turn's call ran a second time"


# ---- the claim never goes through the session save -------------------------


@pytest.mark.asyncio
async def test_the_claim_does_not_disturb_the_session_file(tmp_path):
    """P0-1: a mid-batch session save runs ``ensure_tool_pairing`` and drops the
    in-flight carrier; the sidecar must leave the session file alone."""
    service = SessionService(root=tmp_path / "sessions")
    session = _session(service)
    seen: dict = {}

    class _Probe(ChatEngine):
        def __init__(self):
            self._n = 0

        async def step(self, messages, *, tools, instructions=None, on_delta=None):
            self._n += 1
            if self._n == 1:
                return StepResult(
                    text=None,
                    tool_calls=[ToolCallRef(id="c1", name="write", arguments='{"path":"a.py"}')],
                    finish_reason="tool_calls",
                )
            path = service.root / f"{session.session_id}.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            seen["calls"] = [
                c
                for m in raw["messages"]
                if m.get("toolCalls")
                for c in m["toolCalls"]
            ]
            return StepResult(text="done", finish_reason="stop")

    loop = _loop(_Probe(), service, tmp_path, _WriteTool())
    await loop.process_input(session, "go")

    assert [c["id"] for c in seen["calls"]] == ["c1"], "the pending call was dropped"


# ---- the digest is the one call fingerprint --------------------------------


def test_the_digest_is_call_fingerprint(tmp_path):
    service = SessionService(root=tmp_path / "sessions")
    session = service.create(title="s")
    log = WriteAheadLog(session, service)

    call = ToolCall(id="c1", name="write", params={"path": "a.py"})
    log.claim(call)

    records = _records(service, session.session_id)
    assert records is not None and len(records) == 1
    assert records[0].digest == call_fingerprint(call)


def test_the_log_is_a_noop_when_disabled(tmp_path):
    service = SessionService(root=tmp_path / "sessions")
    session = service.create(title="s")
    log = WriteAheadLog(session, service, enabled=False)

    call = ToolCall(id="c1", name="write", params={"path": "a.py"})
    assert log.claim(call) is None
    assert _records(service, session.session_id) is None


def test_a_resumed_turn_only_replays_what_the_crash_left_behind(tmp_path):
    """I2/D4: the replay scope is the *interrupted* turn's records.

    A resumed turn keeps running for several batches, and the records it
    settles itself land in ``session.write_ahead`` too.  Without a snapshot
    those become replayable for the later batches: the model edits a file,
    re-reads it, and is handed the pre-edit output -- the eval-08 stage F loop,
    reproduced inside a resume.
    """
    service = SessionService(root=tmp_path / "sessions")
    session = service.create(title="s")
    crashed = ToolCall(id="c0", name="write", params={"path": "old.py"})
    fresh = ToolCall(id="c1", name="write", params={"path": "new.py"})

    first = WriteAheadLog(session, service)
    first.claim(crashed)
    first.settle(crashed, ToolResult(success=True, output="OLD"))
    first.claim(fresh)
    first.settle(fresh, ToolResult(success=True, output="NEW"))

    scope = frozenset({call_fingerprint(crashed)})
    log = WriteAheadLog(session, service, replay_scope=scope)

    assert log.claim(crashed, allow_replay=True) is not None
    assert log.claim(fresh, allow_replay=True) is None, (
        "a call settled by the resumed turn must run again, not be replayed"
    )
