"""Defect tripwires for r17 P1 (written during the 20c1468 acceptance review).

Each test asserts the *contract*, not the current behaviour.  They carry
``xfail(strict=False)`` so the suite stays green while the defect is open, and
they flip to XPASS the moment someone fixes it -- which is the signal to delete
the marker.  Nothing here mocks the seam under test: every case reproduces
through the production code path.

Each xfail reason names the file:line that owns the fix.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.snapshots import SnapshotStore
from ai_coding.domain.message import ChatMessage, ImageRef
from ai_coding.domain.run import user_message
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.service.session_service import SessionService
from ai_coding.tools.grep_tool import GrepTool
from ai_coding.tools.htmltext import html_to_text
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.webfetch_tool import FetchRefused, WebfetchTool, _is_public_url
from ai_coding.tools.websearch_tool import WebsearchTool
from ai_coding.tools.write_tool import WriteTool

# --------------------------------------------------------------- T3: SSRF


#: A host that passes the guard but redirects into the user's own network --
#: the exact shape of the bypass: hop 0 is public, hop 1 is 127.0.0.1.
#: Only the DNS lookup behind the guard is real; the hop itself is injected.
_REDIRECTOR = "https://example.com/start"


@pytest.mark.asyncio
async def test_a_redirect_into_a_private_address_must_be_refused():
    """The guard ran on the first URL only (webfetch_tool.py:127) while stdlib
    urlopen followed the 302 on its own (webfetch_tool.py:65), so a public host
    that redirects to 127.0.0.1 handed the model the internal response body."""
    from ai_coding.tools.webfetch_tool import _http_fetch

    asked: list[str] = []

    async def empty() -> AsyncIterator[bytes]:  # the seam's body iterator
        return
        yield b""  # pragma: no cover - makes this an async generator

    async def send(client, url):  # noqa: ARG001 - the one-hop seam
        asked.append(url)
        return 302, {"location": "http://127.0.0.1:8080/latest/meta-data/"}, empty()

    with pytest.raises(FetchRefused) as refused:
        await _http_fetch(_REDIRECTOR, 5.0, max_bytes=1_000_000, max_redirects=5, send=send)

    assert "127.0.0.1" in str(refused.value) or "redirect" in str(refused.value).lower()
    assert len(asked) == 1, f"the private hop was actually requested: {asked}"


@pytest.mark.asyncio
async def test_max_bytes_stops_the_read_mid_stream():
    """``max_bytes`` was a filter applied after ``response.read()`` had already
    pulled the whole body into memory (webfetch_tool.py:67/145)."""
    from ai_coding.tools.webfetch_tool import _http_fetch

    received = 0

    async def chunks():
        nonlocal received
        for _ in range(20):
            received += 512 * 1024
            yield b"x" * (512 * 1024)

    async def send(client, url):  # noqa: ARG001
        return 200, {}, chunks()

    with pytest.raises(FetchRefused):
        await _http_fetch("https://example.com/big", 5.0, max_bytes=1024, send=send)

    assert received <= 512 * 1024, f"read {received} bytes before giving up"


@pytest.mark.asyncio
async def test_too_many_redirects_is_refused():
    """A redirect loop must end in a refusal, not a hang."""
    from ai_coding.tools.webfetch_tool import _http_fetch

    hops = 0

    async def empty() -> AsyncIterator[bytes]:
        return
        yield b""  # pragma: no cover - makes this an async generator

    async def send(client, url):  # noqa: ARG001
        nonlocal hops
        hops += 1
        return 302, {"location": "https://example.com/loop"}, empty()

    with pytest.raises(FetchRefused):
        await _http_fetch("https://example.com/loop", 5.0, send=send)

    assert hops <= 6, f"followed {hops} redirects"


def test_the_direct_private_url_is_still_refused():
    """The guard that does exist must keep working -- this is the control."""
    assert WebfetchTool() is not None
    ok, why = _is_public_url("http://127.0.0.1/x")
    assert ok is False and "SSRF" in why


# ---------------------------------------------------------------- T5: images


def _bare_loop(workspace: Path) -> AgentLoop:
    """A loop with nothing but ``workspace`` set -- the only field ``_load_image``
    reads.  Built this way so the test cannot be moved by collaborators it does
    not care about."""

    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = str(workspace)
    return loop


def test_an_absolute_path_outside_the_workspace_must_not_be_read(tmp_path):
    """``Path(workspace) / "/etc/hostname"`` is ``/etc/hostname`` in Python
    (agent_loop.py:1292) -- no resolve(), no containment, no Sandbox."""
    loop = _bare_loop(tmp_path)

    data_url = loop._load_image(ImageRef(path="/etc/hostname", mime="image/png", name="p"))

    assert data_url is None, f"read outside the workspace: {str(data_url)[:40]}"


def test_a_dot_dot_path_outside_the_workspace_must_not_be_read(tmp_path):
    """Same seam, relative spelling -- the way a session file carries it."""
    loop = _bare_loop(tmp_path)
    ref = ImageRef(path="../../../../etc/hostname", mime="image/png", name="p")

    data_url = loop._load_image(ref)

    assert data_url is None, f"read outside the workspace: {str(data_url)[:40]}"


def test_a_session_message_without_a_mime_degrades_instead_of_breaking_the_request(tmp_path):
    """``session.py`` defaults a missing mime to ``""``, and the old loader
    interpolated it unconditionally -- producing ``data:;base64,...``, which is a
    non-empty string, so mapping sent it as an image and the provider 400'd the
    whole turn.  The refusal now happens at the byte出口, so this walks the real
    chain: session dict -> load_image -> content parts."""
    from ai_coding.ai.mapping import to_chat_message
    from ai_coding.domain.session import _message_from_dict

    loop = _bare_loop(tmp_path)
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    msg = _message_from_dict(
        {"role": "user", "content": "hi", "timestamp": "t", "images": [{"path": "a.png"}]}
    )

    out = to_chat_message(msg, image_loader=loop._load_image)

    parts = out["content"] if isinstance(out["content"], list) else []
    assert not [p for p in parts if p.get("type") == "image_url"], (
        "an image part with an empty mime reached the provider"
    )
    # The note is keyed on ``ref.name`` (mapping.py:69), which this session dict
    # does not carry -- so the contract asserted here is "a readable refusal
    # note exists", not its exact wording.
    assert any("无法读取" in str(p.get("text", "")) for p in parts), (
        f"the refusal did not degrade to a readable note: {parts}"
    )


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("link-out.png", "a symlink that leaves the workspace"),
        ("nested/deep.png", "a file that simply is not there"),
    ],
)
def test_paths_the_workspace_owns_nothing_of_are_refused(tmp_path, path, label):
    """``Sandbox.resolve`` follows the symlink before deciding, so a link that
    points out of the workspace is refused by the same rule as ``..``."""
    from ai_coding.attachments import load_image

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nnot really")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "link-out.png").symlink_to(outside)
    ref = ImageRef(path=path, mime="image/png", name=path)

    assert load_image(ws, ref) is None, f"{label} was read"


def test_a_mime_outside_the_whitelist_is_refused(tmp_path):
    """The whitelist is the same table ``@path`` extraction uses; a mime the
    model was never promised must not become an ``image_url`` part."""
    from ai_coding.attachments import load_image

    (tmp_path / "diagram.bmp").write_bytes(b"BM not really")
    ref = ImageRef(path="diagram.bmp", mime="image/bmp", name="diagram")

    assert load_image(tmp_path, ref) is None


def test_an_image_over_the_budget_is_refused_before_the_bytes_are_read(tmp_path):
    """The budget is checked with ``stat``, so an oversized file costs a stat and
    not its whole size in memory."""
    from ai_coding.attachments import DEFAULT_MAX_BYTES, load_image

    (tmp_path / "big.png").write_bytes(b"\x89PNG" + b"x" * 4096)
    ref = ImageRef(path="big.png", mime="image/png", name="big")

    assert load_image(tmp_path, ref, max_bytes=1024) is None, "over budget but read"
    assert load_image(tmp_path, ref) is not None, "the same file under budget failed"
    assert DEFAULT_MAX_BYTES == 20 * 1024 * 1024, "the paste budget moved"


# ------------------------------------------------------------- T3: htmltext


def test_a_void_element_must_not_swallow_the_whole_document():
    """``_SKIP`` holds ``embed`` (htmltext.py:24) but ``_VOID`` does not
    (htmltext.py:22), and ``<embed>`` never closes -- so the suppression counter
    never returns to zero and the model receives an empty page.  This is the
    meta/link bug again, one tag over."""
    html = "<html><body><embed src='x.pdf'><p>REAL CONTENT</p></body></html>"

    assert "REAL CONTENT" in html_to_text(html), "the document vanished"


# ------------------------------------------------------------------ T2: grep


@pytest.mark.asyncio
async def test_an_unusable_rg_must_not_be_reported_as_no_matches(tmp_path):
    """``grep_tool.py:186`` never inspects ``outcome.error`` /
    ``returncode is None``.  A missing or broken binary therefore returns
    ``success=True`` with "(no matches)" -- a confident wrong answer."""
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")
    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=True, rg_path="/nonexistent/rg")

    result = await tool.execute({"pattern": "target"})

    assert result.success is False or "no matches" not in result.output, (
        f"the runner failed and the tool answered {result.output!r}"
    )


def test_the_fallback_does_not_block_the_event_loop(tmp_path):
    """``grep_tool.py:140`` returns ``self._grep_with_python(...)`` without
    ``await``, so the whole os.walk runs on the event loop -- the SSE bridge and
    the TUI stop for its duration."""
    import asyncio

    (tmp_path / "many").mkdir()
    for i in range(200):
        (tmp_path / "many" / f"f{i}.py").write_text("target\n" * 20, encoding="utf-8")
    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=False)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    async def main():
        background = asyncio.create_task(ticker())
        await tool.execute({"pattern": "target"})
        background.cancel()

    asyncio.run(main())
    assert ticks >= 3, f"the event loop was blocked: {ticks} ticks in the whole scan"


# -------------------------------------------------------------- T1: undo/redo


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", *ident, "commit", "-qm", "i", "--allow-empty"], cwd=root, check=True
    )
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "a"], cwd=root, check=True)
    return root


def _loop(tmp_path: Path, root: Path) -> tuple[AgentLoop, SessionService]:
    from ai_coding.ai.engine import ChatEngine
    from ai_coding.domain.loop import StepResult

    class _Eng(ChatEngine):
        async def step(  # type: ignore[override]
            self, messages, *, tools, instructions=None, on_delta=None, **kwargs
        ):
            return StepResult(text="ok")

    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        _Eng(), sessions, ToolRegistry(), workspace=str(root), snapshots=SnapshotStore(root)
    )
    return loop, sessions


@pytest.mark.asyncio
async def test_undo_must_work_after_a_nudge_was_injected(tmp_path):
    """Auto-continuation, fatigue nudges and steering all write role="user"
    (agent_loop.py:485/745).  ``undo_last_turn`` takes the *last* one as the turn,
    so the key misses the snapshot: history unrolls, zero files are restored, and
    the composer receives an internal system string."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop.process_input(session, "REAL USER TURN")
    (root / "a.txt").write_text("TURN CHANGED IT\n", encoding="utf-8")
    # Mirrors what the loop writes at agent_loop.py:486 -- a harness message in
    # the user's voice, which is exactly what the flag is for.
    session.add_message(
        user_message("[系统] 还有未完成的任务项，请继续。", "t", synthetic=True)
    )

    report = await loop.undo_last_turn(session)

    assert report["files"] == ["a.txt"], "no file was restored"
    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert report["text"] == "REAL USER TURN"


@pytest.mark.asyncio
async def test_redo_after_a_new_turn_must_not_duplicate_history(tmp_path):
    """A new turn neither clears the in-memory tail nor the ``<sid>/redo`` ref,
    so redo re-applies a stale tail and rolls the workspace back under it."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop.process_input(session, "TURN ONE")
    (root / "a.txt").write_text("AFTER TURN ONE\n", encoding="utf-8")
    await loop.undo_last_turn(session)
    await loop.process_input(session, "TURN TWO")
    (root / "a.txt").write_text("AFTER TURN TWO\n", encoding="utf-8")

    await loop.redo_last_turn(session)

    assert [m.content for m in session.messages if m.role == "user"] == ["TURN TWO"]
    assert (root / "a.txt").read_text(encoding="utf-8") == "AFTER TURN TWO\n"


@pytest.mark.asyncio
async def test_undo_must_not_delete_a_file_the_user_created(tmp_path):
    """The baseline is 'untracked at capture time'; it has no notion of who wrote
    a file afterwards, so /undo deletes what the user typed mid-turn and reports
    it as restored."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop.process_input(session, "TURN")
    (root / "draft.txt").write_text("I typed this while it ran\n", encoding="utf-8")

    await loop.undo_last_turn(session)

    assert (root / "draft.txt").exists(), "undo deleted a file the user created"


@pytest.mark.asyncio
async def test_restore_without_a_report_deletes_nothing(tmp_path):
    """The r17 defect was that restore *inferred* what the turn created, so a
    missing baseline made it delete every untracked file in the workspace.  With
    no report there is nothing to delete, by construction."""
    root = _repo(tmp_path)
    (root / "old_notes.md").write_text("mine for months\n", encoding="utf-8")
    store = SnapshotStore(root)
    await store.capture("s/1")
    (root / "created.md").write_text("agent\n", encoding="utf-8")

    restored = await store.restore("s/1")

    assert (root / "old_notes.md").exists(), "restore wiped a pre-snapshot file"
    assert (root / "created.md").exists(), "restore deleted a file nobody reported"
    assert "old_notes.md" not in restored and "created.md" not in restored


@pytest.mark.asyncio
async def test_undo_must_rewind_the_session_turn_counter(tmp_path):
    """turn_count drives working memory, source_turn and last_compacted_turn;
    after an undo it permanently overstates the session, across restarts."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop.process_input(session, "TURN")
    after_turn = session.turn_count
    await loop.undo_last_turn(session)

    assert session.turn_count < after_turn


@pytest.mark.asyncio
async def test_undo_deletes_the_file_the_turn_created(tmp_path):
    """The capability itself must survive the safety fix: a file the turn's own
    write tool created is still removed."""
    from ai_coding.domain.loop import StepResult
    from ai_coding.domain.message import ToolCallRef

    root = _repo(tmp_path)
    from ai_coding.security.sandbox import Sandbox

    class _WriterEngine(ChatEngine):
        def __init__(self):
            self.served = False

        async def step(self, messages, *, tools, instructions=None, on_delta=None, **kw):
            if not self.served:
                self.served = True
                return StepResult(tool_calls=[ToolCallRef(
                    id="c1", name="write",
                    arguments=json.dumps({"path": "created.txt", "content": "agent\n"}))])
            return StepResult(text="写好了")

    sessions = SessionService(tmp_path / "sessions")
    tools = ToolRegistry()
    tools.register(WriteTool(Sandbox(root), FileStateTracker()))
    loop = AgentLoop(
        _WriterEngine(), sessions, tools, workspace=str(root), snapshots=SnapshotStore(root),
        gate=PermissionGate(allowed_tools=("write",)),
    )
    session = sessions.create()

    await loop.process_input(session, "建个文件")
    assert (root / "created.txt").exists(), "the write tool did not run"

    await loop.undo_last_turn(session)

    assert not (root / "created.txt").exists(), "the turn's own file was not removed"


@pytest.mark.asyncio
async def test_a_file_the_human_typed_during_the_turn_is_left_alone(tmp_path):
    """The same turn, same moment: a file the *human* created mid-turn stays."""
    from ai_coding.domain.loop import StepResult
    from ai_coding.domain.message import ToolCallRef
    from ai_coding.security.sandbox import Sandbox

    root = _repo(tmp_path)

    class _WriterEngine(ChatEngine):
        def __init__(self):
            self.served = False

        async def step(self, messages, *, tools, instructions=None, on_delta=None, **kw):
            if not self.served:
                self.served = True
                return StepResult(tool_calls=[ToolCallRef(
                    id="c1", name="write",
                    arguments=json.dumps({"path": "agent.txt", "content": "agent\n"}))])
            return StepResult(text="写好了")

    sessions = SessionService(tmp_path / "sessions")
    tools = ToolRegistry()
    tools.register(WriteTool(Sandbox(root), FileStateTracker()))
    loop = AgentLoop(
        _WriterEngine(), sessions, tools, workspace=str(root), snapshots=SnapshotStore(root),
        gate=PermissionGate(allowed_tools=("write",)),
    )
    session = sessions.create()

    await loop.process_input(session, "建个文件")
    (root / "typed-mid-turn.txt").write_text("mine\n", encoding="utf-8")

    await loop.undo_last_turn(session)

    assert (root / "typed-mid-turn.txt").exists(), "undo deleted the human's own file"
    assert not (root / "agent.txt").exists(), "the agent's file should still go"


@pytest.mark.asyncio
async def test_artifacts_survive_a_restart(tmp_path):
    """A safety net that stops working after a restart is not a safety net."""
    from ai_coding.domain.message import ToolArtifact

    loop, sessions = _loop(tmp_path, _repo(tmp_path))
    session = sessions.create()
    session.add_message(
        ChatMessage(role="tool", content="<tool_output/>", timestamp="t",
                    tool_call_id="c1", tool_name="write",
                    artifacts=(ToolArtifact(path="created.txt", kind="file"),))
    )
    sessions.save(session)

    reloaded = sessions.load(session.session_id)
    tool_msg = [m for m in reloaded.messages if m.role == "tool"][0]

    assert [a.path for a in tool_msg.artifacts] == ["created.txt"], (
        "the report of what the turn wrote did not survive the restart"
    )


@pytest.mark.asyncio
async def test_the_write_tool_reports_a_workspace_relative_path(tmp_path):
    """Not the model's spelling: ``./a/../a.txt`` must not become the key undo
    compares against the filesystem with."""
    from ai_coding.security.sandbox import Sandbox

    (tmp_path / "a").mkdir()
    tool = WriteTool(Sandbox(tmp_path), FileStateTracker())

    result = await tool.execute({"path": "./a/../b.txt", "content": "x"})

    assert result.success and [a.path for a in result.artifacts] == ["b.txt"]


@pytest.mark.asyncio
async def test_undo_in_one_session_does_not_leak_into_another(tmp_path):
    """One loop serves every session the TUI switches between; the staged tail
    must not travel with it."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    first, second = sessions.create(), sessions.create()
    await loop.process_input(first, "SESSION A SECRET")
    await loop.process_input(second, "SESSION B")

    await loop.undo_last_turn(second)
    report = await loop.redo_last_turn(first)

    assert report["ok"] is False, "session A redid session B's staged turn"
    assert [m.content for m in first.messages if m.role == "user"] == [
        "SESSION A SECRET"
    ], "A's own history was disturbed by B's undo"


@pytest.mark.asyncio
async def test_the_undo_report_reaches_the_session_that_asked(tmp_path):
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    first, second = sessions.create(), sessions.create()
    await loop.process_input(first, "A 的回合")

    await loop.undo_last_turn(first)

    assert loop.last_undo_for(first.session_id) is not None
    assert loop.last_undo_for(second.session_id) is None, (
        "A's report is addressable from another session"
    )


@pytest.mark.asyncio
async def test_the_redo_ref_does_not_survive_a_new_turn(tmp_path):
    """OC: sending a new prompt while a revert is staged commits the revert."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()
    await loop.process_input(session, "TURN ONE")
    await loop.undo_last_turn(session)
    assert await loop.snapshots.exists(f"{session.session_id}/redo")

    await loop.process_input(session, "TURN TWO")

    assert not await loop.snapshots.exists(f"{session.session_id}/redo"), (
        "the redo snapshot outlived the turn that should have committed it"
    )


@pytest.mark.asyncio
async def test_undo_says_why_no_file_was_restored(tmp_path):
    """The contract said "明说"; r17 reported an empty file list, which reads
    the same as "nothing had changed"."""
    plain = tmp_path / "plain"
    plain.mkdir()
    loop, sessions = _loop(tmp_path, plain)
    session = sessions.create()
    await loop.process_input(session, "no git here")

    report = await loop.undo_last_turn(session)

    assert report["ok"] is True, "the conversation half still unrolls without git"
    assert report["reason"] == "no-snapshot", report
    assert report["files"] == [], "no file moved, and the report must say so"


@pytest.mark.asyncio
async def test_undo_while_a_turn_is_running_is_refused(tmp_path):
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()
    await loop.process_input(session, "TURN")
    await loop._turn_lock.acquire()
    try:
        report = await loop.undo_last_turn(session)
    finally:
        loop._turn_lock.release()

    assert report == {"ok": False, "reason": "a turn is running"}
    assert [m.content for m in session.messages if m.role == "user"] == ["TURN"], (
        "undo rewrote the history while a turn was still running"
    )


@pytest.mark.asyncio
async def test_redo_does_not_claim_success_when_restore_failed(tmp_path):
    """r17 swallowed restore failures and printed "已恢复上一回合" regardless."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()
    await loop.process_input(session, "TURN")

    async def _boom(*args, **kwargs):
        raise OSError("git exploded")

    loop.snapshots.restore = _boom
    await loop.undo_last_turn(session)
    report = await loop.redo_last_turn(session)

    assert report["ok"] is False or report.get("restored") is not None, report


@pytest.mark.asyncio
async def test_capture_returns_none_when_the_ref_write_fails(tmp_path):
    """r17/F6 was a two-part failure: the baseline ref was best-effort while the
    undo ref was not, so a partial capture looked like a good one and the undo
    went on to delete things.  The baseline is gone; this pins that the one
    remaining ref is all-or-nothing -- no ref, no snapshot."""
    root = _repo(tmp_path)
    # an invalid ref name makes update-ref fail, which is the shape of the
    # failure being pinned (disk full, ref locked, permissions)
    store = SnapshotStore(root, base_ref="refs/ai-coding/undo/bad..name")

    assert await store.capture("s/1") is None
    assert await store.exists("s/1") is False


@pytest.mark.asyncio
async def test_a_broken_rg_binary_does_not_answer_no_matches(tmp_path):
    """``run_process_async`` reports a failed spawn as ``returncode=None`` with
    an ``error``; r17 never looked, and empty stdout rendered as "(no matches)"."""
    from types import SimpleNamespace

    async def runner(argv, *, cwd, timeout):  # noqa: ARG001
        return SimpleNamespace(
            returncode=None, stdout="", stderr="", timed_out=False,
            error="FileNotFoundError: rg",
        )

    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=True, rg_path="/nope/rg", runner=runner)
    (tmp_path / "a.py").write_text("target\n", encoding="utf-8")

    result = await tool.execute({"pattern": "target"})

    assert result.success is False, f"a broken binary answered {result.output!r}"


@pytest.mark.asyncio
async def test_a_rg_error_exit_code_is_not_an_empty_result(tmp_path):
    """rg exits 2 for errors (permissions, bad regex, vanished path); r17 only
    special-cased the parse error and read every other non-zero as "no hits"."""
    from types import SimpleNamespace

    async def runner(argv, *, cwd, timeout):  # noqa: ARG001
        return SimpleNamespace(returncode=2, stdout="", stderr="Permission denied",
                               timed_out=False, error=None)

    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=True, rg_path="/bin/rg", runner=runner)

    result = await tool.execute({"pattern": "target"})

    assert result.success is False, f"an error exit answered {result.output!r}"


@pytest.mark.asyncio
async def test_a_void_tag_in_the_skip_set_would_swallow_the_page():
    """A void element in the suppression set never decrements the counter, so
    everything after it is dropped.  r17 fixed this for meta/link and left
    ``embed``; this walks the whole void-element list so the next one cannot
    arrive with the same bug."""
    from ai_coding.tools.htmltext import _SKIP, _VOID

    void_elements = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    overlap = void_elements & _SKIP
    assert not overlap, f"void tags in the skip set: {sorted(overlap)}"
    assert {"meta", "link", "embed"} <= _VOID, _VOID


@pytest.mark.asyncio
async def test_websearch_keeps_the_good_entries_and_drops_the_malformed():
    """r17 raised out of execute on a non-dict entry; dispatch caught it and the
    Python error text went into the model's context."""
    def opener(url, timeout, data):  # noqa: ARG001
        return "application/json", json.dumps(
            {"results": [None, "not a dict", {"title": "T", "url": "https://e"}]}
        ).encode()

    result = await WebsearchTool(api_key="k", opener=opener).execute({"query": "q"})

    assert result.success is True, result.output
    assert "[T](https://e)" in result.output, result.output
    assert "not a dict" not in result.output, result.output


# ------------------------------------------------------------- T4: mentions


@pytest.mark.asyncio
async def test_the_referenced_file_must_survive_a_nudge_in_the_same_turn(tmp_path):
    """The block is rebuilt from the last role="user" message; a nudge or
    steering line replaces it, so the file the user explicitly attached vanishes
    mid-turn.  Contract: gone with the next *user* message."""
    root = _repo(tmp_path)
    (root / "notes.txt").write_text("unique-payload-42\n", encoding="utf-8")
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop.process_input(session, "@notes.txt")
    # Mirrors what the loop writes at agent_loop.py:486 -- a harness message in
    # the user's voice, which is exactly what the flag is for.
    session.add_message(
        user_message("[系统] 还有未完成的任务项，请继续。", "t", synthetic=True)
    )
    assembler = loop.assembler
    from ai_coding.core.request_assembly import RequestAssembler

    current = RequestAssembler._extract_user_input(session)
    block = assembler._referenced_files_block(current)

    assert "unique-payload-42" in block, f"the file vanished after a nudge: {block!r}"


@pytest.mark.asyncio
async def test_steering_does_not_become_the_undo_subject(tmp_path):
    """A steering line is the harness talking (``agent_loop.py:745``), so ``/undo``
    must not hand it back to the composer as if the user had typed it."""
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop.process_input(session, "REAL USER TURN")
    (root / "a.txt").write_text("TURN CHANGED IT\n", encoding="utf-8")
    session.add_message(
        ChatMessage(role="user", content="[steering] actually use pytest", timestamp="t",
                    synthetic=True)
    )

    report = await loop.undo_last_turn(session)

    assert report["text"] == "REAL USER TURN"
    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert report["files"] == ["a.txt"]


def test_a_synthetic_flag_survives_a_restart(tmp_path):
    """If the flag were memory-only, a resumed session would hand a nudge back
    to the composer and rebuild mentions from it."""
    from ai_coding.domain.session import _message_from_dict, _message_to_dict

    real = ChatMessage(role="user", content="human words", timestamp="t")
    harness = ChatMessage(role="user", content="[系统] 继续。", timestamp="t", synthetic=True)

    assert _message_from_dict(_message_to_dict(real)).synthetic is False
    assert _message_from_dict(_message_to_dict(harness)).synthetic is True


def test_an_old_session_file_reads_every_user_message_as_human():
    """Backward compatibility: files written before the flag existed must still
    load, and their user messages must stay undo subjects."""
    from ai_coding.domain.session import _message_from_dict

    old = _message_from_dict({"role": "user", "content": "from r17", "timestamp": "t"})

    assert old.synthetic is False


@pytest.mark.asyncio
async def test_undo_trims_the_digest_of_the_undone_turn(tmp_path):
    """Otherwise the working-memory ring keeps a line for a turn that no longer
    exists, and the next turn at the same number would appear twice."""
    from ai_coding.domain.turn_digest import TurnDigest

    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()
    await loop.process_input(session, "TURN")
    session.turn_digests.append(TurnDigest(turn=session.turn_count, request="TURN", outcome="done"))

    await loop.undo_last_turn(session)

    assert [d.turn for d in session.turn_digests] == [], "the undone turn kept its digest"


@pytest.mark.asyncio
async def test_the_model_never_sees_the_synthetic_flag(tmp_path):
    """The flag exists for the runtime, not the provider: the wire format must be
    byte-identical to r17."""
    from ai_coding.ai.mapping import history_to_chat_messages

    history = [
        ChatMessage(role="user", content="说点什么", timestamp="t", synthetic=True),
        ChatMessage(role="assistant", content="好", timestamp="t"),
    ]

    for message in history_to_chat_messages(history):
        assert "synthetic" not in message, f"the flag leaked to the provider: {message}"


def test_compaction_anchors_its_tail_on_a_real_user_message(tmp_path):
    """``split_tail`` keeps the most recent exchange verbatim, starting at a user
    boundary.  A trailing nudge would make that boundary the *harness's* line,
    pushing the human's own instruction into the summary (r18/F3)."""
    from ai_coding.core.context_compression import ContextManager

    compressor = ContextManager.__new__(ContextManager)
    compressor._max_context_tokens = 100_000
    compressor._l4_tail_tokens = 4_000

    messages = [
        ChatMessage(role="user", content="真正的问题", timestamp="t"),
        ChatMessage(role="assistant", content="答", timestamp="t"),
        ChatMessage(
            role="user", content="[系统] 还有未完成的任务项，请继续。", timestamp="t",
            synthetic=True,
        ),
    ]

    head, tail = compressor.split_tail(messages)

    assert tail, "the compressor must never return an empty tail"
    assert tail[0].content == "真正的问题", (
        f"the verbatim tail starts at {tail[0].content!r}, not the human message"
    )


# ------------------------------------------------------------ T3: websearch


@pytest.mark.asyncio
async def test_websearch_must_not_crash_on_an_unexpected_payload_shape():
    """``websearch_tool.py:125`` assumes a list of dicts; anything else raises
    inside the tool and the Python error text reaches the model."""

    def opener(url, timeout, data):  # noqa: ARG001
        return "application/json", json.dumps({"results": ["not a dict"]}).encode()

    result = await WebsearchTool(api_key="k", opener=opener).execute({"query": "q"})

    assert result.success is False, f"a malformed payload raised instead: {result.output!r}"


@pytest.mark.asyncio
async def test_websearch_must_say_when_the_payload_had_no_results_field():
    """A provider contract change is currently indistinguishable from 'nothing
    found' -- the model concludes the web has no answer."""

    def opener(url, timeout, data):  # noqa: ARG001
        return "application/json", b'{"answer": "x"}'

    result = await WebsearchTool(api_key="k", opener=opener).execute({"query": "q"})

    assert result.success is False, "a shape change was reported as 'no results'"


@pytest.mark.asyncio
async def test_websearch_must_cap_the_result_count():
    """``max(1, limit)`` has no ceiling, so the model can ask the provider for an
    unbounded page."""
    captured: dict = {}

    def opener(url, timeout, data):  # noqa: ARG001
        captured["n"] = json.loads(data).get("max_results")
        return "application/json", json.dumps({"results": []}).encode()

    tool = WebsearchTool(api_key="k", opener=opener)
    await tool.execute({"query": "q", "max_results": 10000})

    assert captured["n"] is not None and captured["n"] <= 20, (
        f"asked the provider for {captured['n']} results"
    )
