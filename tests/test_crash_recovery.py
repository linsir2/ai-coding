"""Crash recovery (补充式方案 06, P2-P4, TDD).

The existing session layer is a sound snapshot; it was missing three facts that
only matter when the process dies: whether a turn was open, whether an approval
was pending, and what ``/undo`` removed.  These tests pin the facts and the
behaviour that reads them -- each written Red before the implementation.
"""

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.base import BaseTool
from ai_coding.tools.registry import ToolRegistry


def _session(session_id: str = "s") -> SessionData:
    return SessionData(
        session_id=session_id, title="", created_time="t", last_access_time="t"
    )


class _ProbeEngine(ChatEngine):
    """Asks for one tool call, then probes the session file on the next step.

    That second step is the first moment a checkpoint has run, which is when
    the open-turn marker is expected to be on disk -- the design rides the
    existing save rather than adding one, so a crash before any checkpoint has
    no output to lose either.
    """

    def __init__(self, service: SessionService, session_id: str) -> None:
        self._service = service
        self._session_id = session_id
        self.calls = 0
        self.open_turn_on_second_step: int | None = -1

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="read", arguments='{"path":"x"}')],
                finish_reason="tool_calls",
            )
        loaded = self._service.load(self._session_id)
        self.open_turn_on_second_step = None if loaded is None else loaded.open_turn
        return StepResult(text="done", finish_reason="stop")


def _loop(engine, service: SessionService, workspace) -> AgentLoop:
    return AgentLoop(
        engine,
        service,
        ToolRegistry(),
        settings=AIConfig(stop_policy="natural", turn_timeout_seconds=0),
        workspace=str(workspace),
    )


@pytest.mark.asyncio
async def test_open_turn_is_on_disk_after_the_first_checkpoint_and_gone_after(tmp_path):
    """The marker has to reach the file *during* the turn, or a crash between
    the last checkpoint and the close would go undetected -- and it must be
    gone once the turn closes, or every later load would cry crash."""
    service = SessionService(root=tmp_path / "sessions")
    probe = _ProbeEngine(service, "s")
    loop = _loop(probe, service, tmp_path)

    session = _session()
    await loop.process_input(session, "go")

    assert probe.open_turn_on_second_step == 1, "the open turn was not persisted mid-turn"
    assert session.open_turn is None, "the in-memory turn stayed open after close"
    loaded = service.load("s")
    assert loaded is not None and loaded.open_turn is None, "the file still says open"


def test_open_turn_on_load_is_reported_as_recovered(tmp_path):
    """A file that says a turn is open is the crash signal; ``load`` derives
    the one-shot notice from it, so the server can say so without guessing."""
    service = SessionService(root=tmp_path / "sessions")
    crashed = _session("crashed")
    crashed.open_turn = 4
    service.save(crashed)

    loaded = service.load("crashed")
    assert loaded is not None
    assert loaded.open_turn == 4
    assert loaded.recovered_interrupted is True

    clean = _session("clean")
    service.save(clean)
    fresh = service.load("clean")
    assert fresh is not None and fresh.recovered_interrupted is False


# ---- P3: the approval wait is the one place the snapshot was actually wrong ----
#
# The fix is a marker file beside the session, not a session field: the wait
# sits inside the tool batch, and a mid-batch session save runs
# ``ensure_tool_pairing``, which drops an assistant carrier whose result has
# not been written back -- destroying the pending call the marker preserves.


class _WriteTool(BaseTool):
    """A registry entry the gate does not pre-approve, so a call to it asks."""

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return "fake write"

    @property
    def params_json_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx=None):  # type: ignore[override]
        from ai_coding.domain.message import ToolResult

        return ToolResult(success=True, output="written")


class _OneCallEngine(ChatEngine):
    """One tool call, then a final answer."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if self.calls == 1:
            return StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name=self._name, arguments="{}")],
                finish_reason="tool_calls",
            )
        return StepResult(text="done", finish_reason="stop")


class _ProbeApproval(ApprovalCallback):
    """Reads the session while it is being asked -- i.e. mid-wait."""

    def __init__(self, service: SessionService, session_id: str) -> None:
        self._service = service
        self._session_id = session_id
        self.pending_while_asked: object = "unset"

    async def approve(self, tool_name, params, reason):
        loaded = self._service.load(self._session_id)
        self.pending_while_asked = None if loaded is None else loaded.pending_approval
        return ApprovalResult.YES


@pytest.mark.asyncio
async def test_approval_is_durable_before_the_wait(tmp_path):
    """A crash during the wait used to lose the pending call, because the wait
    is ahead of every checkpoint.  It must be on disk while the question is
    open, and gone once it is answered."""
    service = SessionService(root=tmp_path / "sessions")
    approval = _ProbeApproval(service, "s")
    registry = ToolRegistry()
    registry.register(_WriteTool())
    loop = AgentLoop(
        _OneCallEngine("write"),
        service,
        registry,
        approval=approval,
        settings=AIConfig(stop_policy="natural", turn_timeout_seconds=0),
        workspace=str(tmp_path),
    )

    session = _session()
    # The session file has to exist for ``load`` to read the marker; in the
    # real flow the first checkpoint has already written it.  (Before that
    # first save there is nothing to recover anyway -- the batch's own results
    # are not on disk either.)
    service.save(session)
    await loop.process_input(session, "go")

    assert isinstance(approval.pending_while_asked, dict), (
        "the pending approval was not durable while the question was open"
    )
    assert approval.pending_while_asked["toolName"] == "write"
    assert approval.pending_while_asked["callId"] == "c1"
    assert session.pending_approval is None, "the request survived the answer"
    assert service.load("s").pending_approval is None, "the marker outlived the answer"


@pytest.mark.asyncio
async def test_the_pending_call_survives_the_marker_write(tmp_path):
    """The marker must not cost the session anything: the call the human is
    being asked about is still in the history afterwards."""
    service = SessionService(root=tmp_path / "sessions")
    registry = ToolRegistry()
    registry.register(_WriteTool())
    loop = AgentLoop(
        _OneCallEngine("write"),
        service,
        registry,
        approval=_ProbeApproval(service, "s"),
        settings=AIConfig(stop_policy="natural", turn_timeout_seconds=0),
        workspace=str(tmp_path),
    )

    session = _session()
    await loop.process_input(session, "go")

    calls = [c for m in session.messages if m.tool_calls for c in m.tool_calls]
    assert [c.id for c in calls] == ["c1"], "the pending call was dropped"
    results = [m for m in session.messages if m.role == "tool"]
    assert len(results) == 1 and results[0].tool_call_id == "c1"


def test_pending_approval_is_read_back_from_the_marker(tmp_path):
    """A crash during the wait leaves the marker behind; ``load`` surfaces it
    so the front end can ask again, and clearing it stops the report."""
    service = SessionService(root=tmp_path / "sessions")
    session = _session("crashed")
    service.save(session)
    service.mark_pending_approval(
        "crashed",
        {"callId": "c9", "toolName": "bash", "params": {"command": "x"}, "reason": "r"},
    )

    loaded = service.load("crashed")
    assert loaded is not None and loaded.pending_approval is not None
    assert loaded.pending_approval["toolName"] == "bash"

    service.mark_pending_approval("crashed", None)
    cleared = service.load("crashed")
    assert cleared is not None and cleared.pending_approval is None


# ---- P4: the undo history and the created-file bytes survive a restart --------


class _NoopEngine(ChatEngine):
    """Never called: these tests drive undo/redo directly."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="ok")


class _RecordingSessions(SessionService):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.saves = 0

    def save(self, session) -> None:
        self.saves += 1
        super().save(session)


def _git_repo(tmp_path):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init", "--allow-empty",
        ],
        cwd=root, check=True,
    )
    return root


@pytest.mark.asyncio
async def test_undo_state_reaches_the_session_and_redo_works_after_a_restart(tmp_path):
    """``/redo`` used to die with the process: the removed messages lived in a
    loop-local dict.  They belong to the session, so a fresh loop can finish
    what an older one started."""
    from ai_coding.domain.message import ChatMessage

    sessions = SessionService(root=tmp_path / "sessions")
    loop = AgentLoop(_NoopEngine(), sessions, ToolRegistry(), workspace=str(tmp_path))
    session = sessions.create()
    session.add_message(ChatMessage(role="user", content="do it", timestamp="t"))
    session.turn_count = 1
    session.add_message(ChatMessage(role="assistant", content="done", timestamp="t"))

    report = await loop.undo_last_turn(session)
    assert report["ok"] is True
    assert session.messages == []
    assert session.undo_state is not None, "the undo state was not put on the session"

    # A restart: new service, new loop, session read back from disk.
    sessions2 = SessionService(root=tmp_path / "sessions")
    reloaded = sessions2.load(session.session_id)
    assert reloaded is not None
    assert reloaded.undo_state is not None, "the undo state did not reach the file"

    fresh_loop = AgentLoop(_NoopEngine(), sessions2, ToolRegistry(), workspace=str(tmp_path))
    redone = await fresh_loop.redo_last_turn(reloaded)

    assert redone["ok"] is True
    assert [m.content for m in reloaded.messages] == ["do it", "done"]
    assert reloaded.turn_count == 1
    assert reloaded.undo_state is None, "the redo left the staged state behind"


@pytest.mark.asyncio
async def test_created_file_bytes_survive_in_the_undo_sidecar(tmp_path):
    """A redo cannot rebuild a file the turn created from git -- a stash never
    held untracked files.  Undo keeps the bytes; they must outlive the process
    just like the messages do."""
    from ai_coding.core.snapshots import SnapshotStore
    from ai_coding.domain.message import ChatMessage, ToolArtifact

    root = _git_repo(tmp_path)
    sessions = _RecordingSessions(tmp_path / "sessions")
    loop = AgentLoop(
        _NoopEngine(), sessions, ToolRegistry(),
        snapshots=SnapshotStore(root), workspace=str(root),
    )
    session = sessions.create()
    await loop._capture_snapshot(session)  # the pre-turn snapshot
    session.add_message(ChatMessage(role="user", content="造一个文件", timestamp="t"))
    session.turn_count = 1
    made = root / "made.txt"
    made.write_text("content\n", encoding="utf-8")
    session.add_message(
        ChatMessage(
            role="assistant",
            content="done",
            timestamp="t",
            artifacts=(ToolArtifact(path="made.txt", kind="file"),),
        )
    )

    await loop.undo_last_turn(session)
    assert not made.exists(), "undo did not remove the file the turn created"

    # A restart: the bytes have to come from disk, not from process memory.
    sessions2 = SessionService(root=tmp_path / "sessions")
    reloaded = sessions2.load(session.session_id)
    assert reloaded is not None
    fresh_loop = AgentLoop(
        _NoopEngine(), sessions2, ToolRegistry(),
        snapshots=SnapshotStore(root), workspace=str(root),
    )
    redone = await fresh_loop.redo_last_turn(reloaded)

    assert redone["ok"] is True
    assert made.is_file(), "redo could not put the created file back"
    assert made.read_text(encoding="utf-8") == "content\n"


def test_the_pending_marker_is_not_mistaken_for_a_session(tmp_path):
    """Real-chain verification caught this: the marker sat next to the session
    files, and ``list_sessions`` globs ``*.json`` -- so /resume listed a phantom
    empty session.  It now lives in its own directory, where the scan cannot
    reach it."""
    service = SessionService(root=tmp_path / "sessions")
    session = service.create(title="real")
    service.save(session)
    service.mark_pending_approval(
        session.session_id, {"callId": "c1", "toolName": "bash"}
    )

    listed = service.list_sessions()

    assert [s.session_id for s in listed] == [session.session_id]


def test_deleting_a_session_takes_its_marker_with_it(tmp_path):
    """An orphan marker would keep reporting a pending approval for a session
    that no longer exists."""
    service = SessionService(root=tmp_path / "sessions")
    session = service.create(title="doomed")
    service.save(session)
    service.mark_pending_approval(session.session_id, {"callId": "c1", "toolName": "bash"})

    assert service.delete(session.session_id) is True

    assert service.load(session.session_id) is None
    assert not (tmp_path / "sessions" / "pending" / f"{session.session_id}.json").exists()
