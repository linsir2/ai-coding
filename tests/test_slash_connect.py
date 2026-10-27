"""``/connect`` 必须是一条**已发布**的命令（H3 红相）。

TUI 判断"这行是不是命令"只信后端的 ``slash.list``（``util/submission.ts``）。
后端不认识 ``/connect`` 时，用户敲它回车，这句话会连同图片一起被当成
prompt 发给模型——既没打开向导，又花了一次模型调用。
"""

from __future__ import annotations

import pytest

from ai_coding.app.commands import REPLAction, handle_input
from ai_coding.app.slash import command_names, parse_slash


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message: str) -> None:
        self.lines.append(message)

    def error(self, message: str) -> None:
        self.lines.append(message)

    def delta(self, token: str) -> None:  # pragma: no cover
        pass

    def rule(self, title: str = "") -> None:  # pragma: no cover
        pass

    def step(self, title: str):  # pragma: no cover
        return None


class _Loop:
    async def process_input(self, *args, **kwargs):
        raise AssertionError("/connect must not reach the model")


class _Sessions:
    def create(self):  # pragma: no cover - not used by this command
        raise AssertionError("unexpected session create")


def test_connect_is_a_published_command():
    assert "connect" in command_names()


def test_connect_parses_as_itself():
    command = parse_slash("/connect")

    assert command is not None
    assert command.name == "connect"


@pytest.mark.asyncio
async def test_connect_does_not_reach_the_model():
    console = _Console()

    action, _session = await handle_input(
        _Loop(), "/connect", object(), _Sessions(), console
    )

    assert action is REPLAction.CONTINUE
    assert console.lines, "the command has to say something"


@pytest.mark.asyncio
async def test_connect_explains_where_to_do_it():
    console = _Console()

    await handle_input(_Loop(), "/connect", object(), _Sessions(), console)

    out = "\n".join(console.lines)
    assert "ctrl+a" in out
    assert "config init" in out
