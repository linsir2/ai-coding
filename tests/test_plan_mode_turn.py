"""``run_human_turn`` runs the human's utterance as the turn it deserves.

Plan mode is now the codex parity path: a plan-mode utterance is the human's
own words through the ordinary front door, so this file keeps the ordinary-turn
half of that contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.app.commands import run_human_turn
from ai_coding.domain.session import SessionData
from ai_coding.domain.stance import PostureResult


class _Console:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def info(self, message: str) -> None:  # pragma: no cover - not used here
        pass

    def rule(self, title: str = "") -> None:  # pragma: no cover - not used here
        pass

    def delta(self, token: str) -> None:  # pragma: no cover - not used here
        pass


class _Loop:
    """A loop double that records the turns it was asked to run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.workspace = "."
        self.extra_workspace_roots: tuple[str, ...] = ()

    async def process_input(
        self,
        session: SessionData,
        text: str,
        on_delta: Any = None,
        observer: Any = None,
        **kwargs: Any,
    ) -> str:
        self.calls.append((text, session.mode))
        return "done"

    def request_posture(self, session: SessionData, **kwargs: Any) -> Any:
        self.commit_posture(session, **kwargs)
        return PostureResult.COMMITTED

    def commit_posture(
        self,
        session: SessionData,
        *,
        agent: str | None = None,
        readonly: bool | None = None,
        reason: str = "",
        observer: Any = None,
    ) -> None:
        session.agent = agent if agent is not None else session.agent
        session.readonly = bool(readonly) if readonly is not None else session.readonly
        session.pending_posture = None


def _session(mode: str = "normal") -> SessionData:
    session = SessionData(session_id="s1", title="t", created_time="", last_access_time="")
    # M1b: the agent is the authority; ``mode`` is derived from it.
    session.agent = "plan" if mode == "plan" else "build"
    return session


@pytest.mark.asyncio
async def test_an_ordinary_utterance_is_an_ordinary_turn():
    """Outside plan mode nothing changes: the loop runs the text as it always did."""
    loop = _Loop()
    session = _session("normal")
    answer = await run_human_turn(loop, session, "hello", console=_Console())

    assert answer == "done"
    assert loop.calls == [("hello", "normal")]
