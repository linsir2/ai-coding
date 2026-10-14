"""TDD (r17/T1): /undo and /redo at the loop level.

The contract: undo rolls the last user turn back — files to their pre-turn
content, history to before that message — and hands the text back so the TUI can
put it in the composer.  Redo puts both back.  Outside a git repo the file half
degrades to a no-op and the turn still unrolls (the history half never needed
git).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.snapshots import SnapshotStore
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Never called: these tests drive ``undo_last_turn`` directly."""

    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[override]
        return StepResult(text="ok")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "init", "--allow-empty",
        ],
        cwd=root,
        check=True,
    )
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "add", "a.txt"],
        cwd=root,
        check=True,
    )
    return root


def _loop(tmp_path: Path, workspace: Path) -> tuple[AgentLoop, SessionService]:
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(_Engine(), sessions, ToolRegistry(), snapshots=SnapshotStore(workspace))
    return loop, sessions


@pytest.mark.asyncio
async def test_undo_truncates_history_restores_files_and_reports_the_text(tmp_path):
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)
    session = sessions.create()

    await loop._capture_snapshot(session)  # the pre-turn snapshot the loop takes
    session.add_message(ChatMessage(role="user", content="改一下 a.txt", timestamp="t"))
    (root / "a.txt").write_text("broken\n", encoding="utf-8")
    session.add_message(ChatMessage(role="assistant", content="done", timestamp="t"))

    report = await loop.undo_last_turn(session)

    assert report["text"] == "改一下 a.txt"
    assert session.messages == [], "history is truncated to before the user message"
    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert report["files"] == ["a.txt"]
    # r18/F5: the staged report is per session, read through its accessor.
    assert loop.last_undo_for(session.session_id) == report

    await loop.redo_last_turn(session)

    assert len(session.messages) == 2, "redo puts the turn back"
    assert (root / "a.txt").read_text(encoding="utf-8") == "broken\n"


@pytest.mark.asyncio
async def test_undo_without_a_user_turn_says_so(tmp_path):
    root = _repo(tmp_path)
    loop, sessions = _loop(tmp_path, root)

    report = await loop.undo_last_turn(sessions.create())

    assert report == {"ok": False, "reason": "nothing to undo"}


@pytest.mark.asyncio
async def test_outside_a_repo_the_history_still_unrolls(tmp_path):
    """No git: undo has no files to restore, but the conversation half works and
    the report says exactly how many files were restored (zero)."""
    plain = tmp_path / "plain"
    plain.mkdir()
    loop, sessions = _loop(tmp_path, plain)
    session = sessions.create()

    await loop._capture_snapshot(session)
    session.add_message(ChatMessage(role="user", content="hello", timestamp="t"))

    report = await loop.undo_last_turn(session)

    assert report["ok"] is True and report["files"] == [] and report["text"] == "hello"
    assert session.messages == []
