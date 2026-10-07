"""TDD (M2-2): HITL approval callback protocol."""

import pytest

from ai_coding.security.approval import (
    ApprovalResult,
    CLIApprovalCallback,
)


class FakeInput:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.replies.pop(0) if self.replies else ""


@pytest.mark.asyncio
async def test_approve_yes():
    cb = CLIApprovalCallback(input_fn=FakeInput(["y"]))
    r = await cb.approve("bash", {"command": "ls"}, "needs confirmation")
    assert r == ApprovalResult.YES


@pytest.mark.asyncio
async def test_approve_no():
    cb = CLIApprovalCallback(input_fn=FakeInput(["n"]))
    r = await cb.approve("bash", {"command": "rm foo"}, "dangerous")
    assert r == ApprovalResult.NO


@pytest.mark.asyncio
async def test_approve_stop():
    cb = CLIApprovalCallback(input_fn=FakeInput(["s"]))
    r = await cb.approve("write", {"path": "f.txt"}, "warn")
    assert r == ApprovalResult.STOP


@pytest.mark.asyncio
async def test_approve_default_no_on_empty():
    cb = CLIApprovalCallback(input_fn=FakeInput([""]))
    r = await cb.approve("write", {"path": "f.txt"}, "warn")
    assert r == ApprovalResult.NO  # fail-closed


