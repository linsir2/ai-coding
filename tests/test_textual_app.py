"""The Textual shell: status bar, transcript area, input bar."""

import asyncio
from types import SimpleNamespace

import pytest
from textual.command import CommandPalette
from textual.widgets import Input, Static

from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.ui.status_bar import StatusBar
from ai_coding.ui.textual_app import AICodingApp
from ai_coding.ui.transcript import Prose


def _app(**overrides):
    kwargs = {
        "loop": None,
        "session": object(),
        "sessions": object(),
        "workspace": "~/projects/ai-app",
        "model_name": "DeepSeek-V3",
    }
    kwargs.update(overrides)
    return AICodingApp(**kwargs)


@pytest.mark.asyncio
async def test_shell_mounts_with_status_transcript_and_input():
    app = _app()
    async with app.run_test():
        assert app.query_one(StatusBar)
        assert app.query_one("#transcript")
        prompt = app.query_one("#prompt", Input)
        assert prompt.placeholder == "Type a message or enter a command..."
        assert app.query_one("#input-hint").content == "/ for commands"
        assert "● Ready" in app.query_one("#status-right").content.plain
        assert "~/projects/ai-app" in app.query_one("#status-left").content.plain


@pytest.mark.asyncio
async def test_status_bar_switches_to_working():
    app = _app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.set_busy(True)
        await pilot.pause()
        assert "● Working" in app.query_one("#status-right").content.plain


class _FakeLoop:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.workspace = "/tmp/ws"
        self.model_name = "fake-model"
        # A real AgentLoop always has a policy; /plan swaps a copy of it to
        # disarm C3 for the planning turn, so the stub carries one too.
        self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        self.calls.append(text)
        if on_delta is not None:
            on_delta("## 回答\n\n")
            on_delta("**好**")
        return "## 回答\n\n**好**"

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_submit_runs_the_loop_and_streams_markdown():
    loop = _FakeLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "你好"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert loop.calls == ["你好"]
        assert "你好" in _all_static_text(app)
        assert "好" in app.query_one(Prose).source


@pytest.mark.asyncio
async def test_input_stays_live_and_queues_while_a_turn_runs():
    loop = _BlockingLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "第一次"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if app._turn_active:
                break
        assert app._turn_active, "the turn never started"
        assert app.query_one("#prompt").disabled is False, "输入框被禁用了"
        app.query_one("#prompt").value = "第二次"
        await pilot.press("enter")
        await pilot.pause()
        assert list(app._queued) == ["第二次"], "回合运行中的提交没有被排队"
        loop.release()
        for _ in range(20):
            await pilot.pause()
            if loop.calls == ["第一次", "第二次"]:
                break
        assert loop.calls == ["第一次", "第二次"], "第一条结束后排队的那条没有按序执行"


class _BlockingLoop(_FakeLoop):
    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def process_input(self, session, text, *, on_delta=None, observer=None):
        await self._gate.wait()
        return await super().process_input(
            session, text, on_delta=on_delta, observer=observer
        )


def _all_static_text(app) -> str:
    # Prose subclasses Static but holds a Rich renderable whose str() is a
    # repr; it is covered by .source assertions, not by static-text scans.
    return "\n".join(
        w.content.plain if hasattr(w.content, "plain") else str(w.content)
        for w in app.query(Static)
        if not isinstance(w, Prose)
    )


@pytest.mark.asyncio
async def test_slash_command_runs_through_handle_input(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        console.info("[status] ok")
        from ai_coding.ui.repl import REPLAction
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        assert seen == ["/status"]
        assert "[status] ok" in _all_static_text(app)
        assert loop.calls == []  # slash does not go to the model


@pytest.mark.asyncio
async def test_resume_with_an_id_still_works():
    """The typed form keeps working; only the bare form becomes a picker."""
    from ai_coding.domain.session import SessionData
    from ai_coding.ui.repl import REPLAction

    target = SessionData(session_id="a" * 32, title="", created_time="", last_access_time="")
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        return REPLAction.CONTINUE, session

    app = _app(loop=_FakeLoop(), sessions=SimpleNamespace(resolve=lambda p: [target]))
    import ai_coding.ui.textual_app as ta

    original = ta.handle_input
    ta.handle_input = fake_handle_input
    try:
        async with app.run_test() as pilot:
            app.query_one("#prompt").value = f"/resume {target.session_id[:12]}"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            assert seen == [f"/resume {target.session_id[:12]}"]
            assert not CommandPalette.is_open(app)
    finally:
        ta.handle_input = original


@pytest.mark.asyncio
async def test_status_bar_set_model_updates_display():
    app = _app()
    async with app.run_test() as pilot:
        bar = app.query_one(StatusBar)
        bar.set_model("NewModel-X")
        await pilot.pause()
        assert "NewModel-X" in app.query_one("#status-left").content.plain


@pytest.mark.asyncio
async def test_permissions_switch_re_attaches_the_provider():
    from ai_coding.security.approval import CLIApprovalCallback

    app = _app(loop=_FakeLoop())
    async with app.run_test():
        fresh = CLIApprovalCallback()
        dispatcher = SimpleNamespace(approval=fresh)
        app._ensure_approval_provider(dispatcher)
        assert fresh._provider == app.approval_provider


@pytest.mark.asyncio
async def test_run_command_re_attaches_the_approval_provider(monkeypatch):
    from ai_coding.security.approval import CLIApprovalCallback
    from ai_coding.ui.repl import REPLAction

    loop = _FakeLoop()
    stale = CLIApprovalCallback()  # what /permissions confirm installs: no provider
    loop.dispatcher = SimpleNamespace(approval=stale)
    app = _app(loop=loop)

    async def fake_handle_input(loop_, text, session, sessions, console):
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert stale._provider == app.approval_provider


@pytest.mark.asyncio
async def test_textual_console_delta_streams_into_transcript(monkeypatch):
    from ai_coding.ui.repl import REPLAction

    loop = _FakeLoop()
    app = _app(loop=loop)
    seen: list[str] = []

    async def fake_handle_input(loop_, text, session, sessions, console):
        seen.append(text)
        console.delta("## 计划\n\n")
        console.delta("第一步：写代码")
        console._end_stream()
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/review"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert seen == ["/review"]
        # Console deltas ride Turn.stream like model deltas; they land in
        # Prose.source (previously MarkdownParagraph Static children, which
        # is why a Static scan used to see them).
        prose = app.query_one(Prose).source
        assert "计划" in prose
        assert "第一步：写代码" in prose


@pytest.mark.asyncio
async def test_plan_asks_before_running(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)

    async def fake_wait(screen):
        return "add tests"

    monkeypatch.setattr(app, "push_screen_wait", fake_wait)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert len(loop.calls) == 1
        assert "add tests" in loop.calls[0]
        assert "implementation plan" in loop.calls[0]


@pytest.mark.asyncio
async def test_plan_escape_does_nothing(monkeypatch):
    loop = _FakeLoop()
    app = _app(loop=loop)

    async def fake_wait(screen):
        return ""

    monkeypatch.setattr(app, "push_screen_wait", fake_wait)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert loop.calls == []


@pytest.mark.asyncio
async def test_input_stays_live_and_queues_while_a_command_runs(monkeypatch):
    """The old guard was ``input.disabled`` for the whole command; it is now a
    queue.  Same invariant (no concurrent turns), usable input."""
    from ai_coding.ui.repl import REPLAction

    loop = _FakeLoop()
    app = _app(loop=loop)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_handle_input(loop_, text, session, sessions, console):
        started.set()
        await gate.wait()
        return REPLAction.CONTINUE, session

    monkeypatch.setattr("ai_coding.ui.textual_app.handle_input", fake_handle_input)
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/status"
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if started.is_set():
                break
        assert started.is_set(), "the command never started"
        assert app.query_one("#prompt").disabled is False, "输入框被禁用了"
        app.query_one("#prompt").value = "later"
        await pilot.press("enter")
        await pilot.pause()
        assert list(app._queued) == ["later"], "命令运行中的提交没有被排队"
        assert loop.calls == [], "排队的那条在命令没结束时就开始跑了"
        gate.set()
        for _ in range(20):
            await pilot.pause()
            if loop.calls == ["later"]:
                break
        assert loop.calls == ["later"], "命令结束后排队的那条没有执行"


@pytest.mark.asyncio
async def test_escape_sets_the_cancel_event_the_dispatcher_waits_on():
    """Esc must reach the switch ``ToolDispatcher`` already watches.

    The whole chain was already there — ``dispatch.py:171`` holds the event,
    ``deadline.py:132`` abandons the tool when it fires, and
    ``dispatch.py:446-450`` kills the subprocess group on top of that. Nothing
    ever constructed the event or set it, so a long tool call could only be
    escaped by quitting the app.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        assert not app._cancel_event.is_set()
        await pilot.press("escape")
        assert app._cancel_event.is_set()


@pytest.mark.asyncio
async def test_escape_sets_the_event_the_dispatcher_was_given():
    """The app must set the loop's event, not a private replacement.

    ``run_app`` builds the dispatcher first and hands the same instance to the
    app, so Esc has to land on that object.  A fresh ``asyncio.Event()`` here
    would look identical to every other test while silently disabling the
    switch in the real app.
    """
    cancel = asyncio.Event()
    app = _app(loop=_FakeLoop(), cancel_event=cancel)
    assert app._cancel_event is cancel
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert cancel.is_set()


@pytest.mark.asyncio
async def test_ask_screen_receives_the_options_the_model_offered():
    """The modal's ``options`` parameter must stop being dead code.

    ``AskScreen`` has rendered ``#ask-options`` since it was written, but every
    caller left the tuple empty, so the line never appeared.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        worker = pilot.app.run_worker(
            app.ask_provider("改 A 还是 B？", ["A", "B"]), exclusive=False
        )
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "AskScreen":
                break
        assert app.screen.__class__.__name__ == "AskScreen", "modal never opened"
        assert app.screen._options == ("A", "B")  # type: ignore[attr-defined]
        # The options line only renders when the tuple is non-empty.
        assert app.screen.query("#ask-options")  # type: ignore[attr-defined]
        app.screen.query_one("#ask-input", Input).value = "A"
        await pilot.press("enter")
        await worker.wait()

    assert worker.result == "A"


@pytest.mark.asyncio
async def test_ctrl_y_copies_last_tool_output_to_clipboard():
    """`y` (ctrl) copies the most recent finished tool call, params + output."""
    from types import SimpleNamespace

    from textual.widgets import Input

    from ai_coding.core.events import ToolFinished, ToolStarted
    from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolOutput
    from ai_coding.domain.run import tool_message

    full_output = "commit f9fd276\n 3 files changed"
    session = SimpleNamespace(
        messages=[
            ChatMessage(
                role="assistant", content=None, timestamp="",
                tool_calls=[ToolCallRef(id="c1", name="bash",
                                        arguments='{"command": "git log -1 --stat"}')],
            ),
            tool_message(
                ToolOutput(call_id="c1", tool_name="bash", output=full_output),
                timestamp="",
            ),
        ]
    )

    class _Loop:
        async def process_input(self, session, text, *, on_delta=None, observer=None):
            assert observer is not None
            observer.on_event(ToolStarted(call_id="c1", tool_name="bash",
                                         params={"command": "git log -1 --stat"}))
            observer.on_event(ToolFinished(call_id="c1", tool_name="bash",
                                           success=True, output_preview="done"))
            if on_delta is not None:
                on_delta("answer")
            return "answer"

        async def aclose(self):
            return None

    app = _app(loop=_Loop(), session=session)
    async with app.run_test(size=(80, 24)) as pilot:
        app.query_one("#prompt", Input).value = "commit?"
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        await pilot.press("ctrl+y")
        await pilot.pause()
        copied = app.clipboard
        assert "$ git log -1 --stat" in copied
        assert "3 files changed" in copied


@pytest.mark.asyncio
async def test_ctrl_y_with_no_tool_says_so():
    app = _app(loop=_FakeLoop())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("ctrl+y")
        await pilot.pause()
        assert "没有可复制的工具输出" in _all_static_text(app)


@pytest.mark.asyncio
async def test_plan_with_topic_skips_the_prompt_and_resets_the_list():
    """The typed path: "/plan <topic>" must plan that topic, not ask (R1-5b).

    Before, the router matched only the exact string "/plan"; the typed
    topic fell through to the generic handler and never planned anything.
    """
    from ai_coding.domain.session import SessionData
    from ai_coding.domain.task import TaskState

    loop = _FakeLoop()
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="这个项目最近一次提交干嘛的")
    session.task.add("旧的待办")
    app = _app(loop=loop, session=session)

    def refuse(*_args, **_kwargs):
        raise AssertionError("must not ask for a topic when one was typed")

    app.push_screen_wait = refuse
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan 给 /tasks 加一个 show 子命令"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        assert len(loop.calls) == 1
        assert "给 /tasks 加一个 show 子命令" in loop.calls[0]
        assert session.task.goal == "给 /tasks 加一个 show 子命令"
        assert session.task.items == []


@pytest.mark.asyncio
async def test_plan_turn_runs_with_auto_continuation_disabled():
    """Review fix: the planning turn must not inherit C3.

    Its open items are the plan being authored, not work in progress, so a
    budget-exhausted planning turn was told to "continue the unfinished
    work" — on the one turn whose prompt says *do not execute the work*
    (measured: 10 model calls and 3 [continue] injections where 2 calls did
    the job).  The step turns the runner runs afterwards keep their own
    policy; this only disarms the planning turn, and restores the loop's.
    """
    from ai_coding.domain.session import SessionData
    from ai_coding.domain.task import TaskState

    loop = _FakeLoop()
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="old goal")
    armed = loop.policy
    seen: list[int] = []
    inner = loop.process_input

    async def spy(target, text, **kwargs):
        seen.append(loop.policy.config.max_auto_continuations)
        return await inner(target, text, **kwargs)

    loop.process_input = spy
    app = _app(loop=loop, session=session)

    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan 加一个子命令"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

    assert seen == [0]          # the planning turn ran with C3 disarmed
    assert loop.policy is armed  # and the loop got its policy back


@pytest.mark.asyncio
async def test_plan_run_is_gone_as_a_command():
    """M1-A: /plan-run was absorbed into /plan — and now says so.

    This test used to assert the opposite tail: that /plan-run "reaches the
    model as prose".  That was the M1-A behaviour and it was wrong — a command
    the docs and muscle memory still advertise was forwarded as a model turn,
    and at four edits from "plan" the near-miss rule could not catch it.  The
    tombstone answers it instead.  What must remain true is the part M1-A was
    actually about: it is not a command, so it never reaches the plan runner.
    """
    from ai_coding.domain.session import SessionData

    loop = _FakeLoop()
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    app = _app(loop=loop, session=session)

    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan-run"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        # Not a command, and not a model turn either: it gets a pointer.
        assert loop.calls == []
        out = _all_static_text(app)
        assert "did you mean" in out and "/plan" in out


@pytest.mark.asyncio
async def test_planning_shows_each_step_as_a_live_fold():
    """M3: executing a plan must show the work, not only "[plan] 步骤 k/N".

    The runner already threaded ``on_delta`` into every step turn, but the
    app passed none for the run, so a plan executed in front of the user as a
    sequence of step headers while the model wrote prose and ran commands
    that nobody could see — and the step transcripts are discarded by design
    (M1), so this view was the only place that work could ever appear.

    Driven through the real runner with the app's own console, so the wiring
    is what is under test, not a stub's.
    """
    from ai_coding.core.plan_runner import STEP_HEAD
    from ai_coding.domain.session import SessionData
    from ai_coding.domain.task import TaskState

    class _PlanningLoop(_FakeLoop):
        """Plans like the model does, then works one step per turn."""

        def __init__(self):
            super().__init__()
            self.policy = TurnPolicy(TurnPolicyConfig(max_auto_continuations=3))

        async def process_input(self, session, text, *, on_delta=None, observer=None):
            if text.startswith("Create a detailed implementation plan"):
                session.task.add("改 loader")
                session.task.add("补测试")
                return "计划写好了"
            if text.startswith(STEP_HEAD):
                if on_delta is not None:
                    on_delta("读了 loader，准备改\n")
                for item in session.task.open_items()[:1]:
                    item.status = "completed"
                return "改完了"
            return await super().process_input(
                session, text, on_delta=on_delta, observer=observer
            )

    loop = _PlanningLoop()
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.task = TaskState(goal="")
    app = _app(loop=loop, session=session)

    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/plan 改 loader"
        await pilot.press("enter")
        for _ in range(4):
            await pilot.pause()
        # queried inside run_test: the DOM is gone once the app stops
        folds = app.query(".plan-step-fold")
        assert len(folds) == 2, [f.title for f in folds]
        assert "读了 loader，准备改" in "\n".join(
            md.source for md in app.query(Prose)
        )
        # the first step's title carries its receipt summary
        assert "改 loader" in folds[0].title


@pytest.mark.asyncio
async def test_always_allow_skips_the_modal_for_that_call():
    """G4: the key is the resource the operator was shown, not the tool.

    The setup changed with the contract: what is remembered is
    ``edit:a.py``, because that is what the modal displayed.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test():
        app._grants.add("edit:a.py")
        answer = await app.approval_provider("edit", {"path": "a.py"}, "reason")
        assert answer == "y"
        assert len(app.screen_stack) == 1, "always 命中不应弹窗"


@pytest.mark.asyncio
async def test_always_allow_is_scoped_to_the_resource():
    """Drive the provider, not a hand-pushed modal: only the provider decides
    whether the modal appears at all."""
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        app._grants.add("edit:a.py")
        worker = app.run_worker(
            app.approval_provider("bash", {"command": "ls"}, "r"), exclusive=False
        )
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ApprovalScreen":
                break
        assert app.screen.__class__.__name__ == "ApprovalScreen", "bash 应弹窗"
        await pilot.click("#no")
        await worker.wait()
    assert worker.result == "n"


@pytest.mark.asyncio
async def test_always_allow_records_on_answer_a():
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        worker = app.run_worker(
            app.approval_provider(
                "edit", {"path": "a.py", "old_string": "x", "new_string": "y"}, ""
            ),
            exclusive=False,
        )
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ApprovalScreen":
                break
        assert app.screen.__class__.__name__ == "ApprovalScreen", "edit 应弹窗"
        await pilot.click("#always")
        await worker.wait()
    assert worker.result == "y"
    # The contract: what gets remembered is the file that was read, not "edit".
    assert "edit:a.py" in app._grants


@pytest.mark.asyncio
async def test_always_allow_excludes_bash():
    """bash 的 always 等于放弃所有命令审查——永远不允许。"""
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        app._grants.add("bash")
        worker = app.run_worker(
            app.approval_provider("bash", {"command": "ls"}, "r"), exclusive=False
        )
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ApprovalScreen":
                break
        assert app.screen.__class__.__name__ == "ApprovalScreen", "bash 即使在集合里也应弹窗"
        await pilot.click("#no")
        await worker.wait()
    assert worker.result == "n"


@pytest.mark.asyncio
async def test_a_grant_does_not_cover_a_different_file():
    """G4: the grant has to mean what the modal showed.

    The operator reads `edit a.py` — diff and all — and presses Always.  If that
    were remembered as "edit", every later edit to every file would run without
    being read, which is strictly more than the one call they approved.
    """
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        app._grants.add("edit:a.py")

        # The file that was approved: no modal.
        assert await app.approval_provider("edit", {"path": "a.py"}, "r") == "y"
        assert len(app.screen_stack) == 1, "the approved file must not ask again"

        # A different file: still has to be read.
        worker = app.run_worker(
            app.approval_provider("edit", {"path": "b.py"}, "r"), exclusive=False
        )
        for _ in range(10):
            await pilot.pause()
            if app.screen.__class__.__name__ == "ApprovalScreen":
                break
        assert app.screen.__class__.__name__ == "ApprovalScreen", (
            "a grant on one file must not cover another"
        )
        await pilot.click("#no")
        await worker.wait()
    assert worker.result == "n"
