"""Task driver (11-spec §5): the kernel-side round loop for armed tasks.

The driver owns the generic lifecycle of an armed task: ask the profile for
the next item, run it through the ordinary turn path, let the profile settle
it mechanically, stop on completion / a failing round / the round cap / a
disarm — and leave accounting to the profile's hooks.  Everything
plan-specific lives behind ``RoundProfile``, so one driver serves ``/plan``
today and any future armed task, with the plan behaviour pinned by the
existing plan suites (its Prove).

（audit addendum 2026-10-16）Two corrections against the original sketch:
``before_round`` fires only once an item was picked (the expected log is
the contract), and the loop re-checks ``task.armed`` every round — the
seam T7's Esc handling depends on.
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["RoundProfile", "TaskDriver"]


class RoundProfile(Protocol):
    """What one kind of armed task must provide; one hook per existing part.

    Every method maps 1:1 to a piece the plan runner already had (11-spec
    §6.1), so the plan profile is an adapter, not a rewrite.
    """

    def new_report(self) -> Any: ...

    def max_rounds(self) -> int: ...

    def prepare(self, session: Any, task: Any) -> set[str]: ...

    def before_round(
        self, task: Any, session: Any, report: Any, receipted: set[str]
    ) -> None: ...

    def next_item(self, task: Any, attempted: set[str]) -> Any | None: ...

    def no_item(self, task: Any, report: Any) -> None: ...

    def rounds_used(self, report: Any) -> int: ...

    def budget_stop(self, report: Any, cap: int) -> None: ...

    async def run_round(
        self,
        session: Any,
        task: Any,
        item: Any,
        report: Any,
        console: Any,
        *,
        on_delta: Any,
        observer: Any,
    ) -> None: ...

    def after_round(self, session: Any, task: Any, report: Any) -> None: ...

    def should_stop(self, report: Any, item: Any) -> bool: ...

    def taskless(self, report: Any, console: Any) -> None: ...

    def persist(self, session: Any) -> None: ...

    def finish(self, console: Any, report: Any) -> None: ...


class TaskDriver:
    """Drive one armed task round by round (11-spec §5.1)."""

    def __init__(self, loop: Any) -> None:
        self._loop = loop

    async def run(
        self,
        session: Any,
        profile: RoundProfile,
        console: Any = None,
        *,
        on_delta: Any = None,
        observer: Any = None,
    ) -> Any:
        report = profile.new_report()
        task = getattr(session, "task", None)
        if task is None:
            # No task at all: same verdict as an empty plan — "nothing to
            # do" — and no persist, matching the plan runner's early exit.
            profile.taskless(report, console)
            return report
        attempted: set[str] = set()
        cap = profile.max_rounds()
        receipted = profile.prepare(session, task)
        while True:
            # 11-spec §5.1 / audit addendum: a disarm between rounds (Esc,
            # breaker, an external `/plan off`) ends the loop before the
            # next round is opened.  Absent attribute reads as armed so the
            # fake-profile tests can drive a bare object.
            if not getattr(task, "armed", True):
                break
            if 0 < cap <= profile.rounds_used(report):
                profile.budget_stop(report, cap)
                break
            item = profile.next_item(task, attempted)
            if item is None:
                profile.no_item(task, report)
                break
            profile.before_round(task, session, report, receipted)
            attempted.add(item.id)
            await profile.run_round(
                session,
                task,
                item,
                report,
                console,
                on_delta=on_delta,
                observer=observer,
            )
            receipted.add(item.id)
            profile.after_round(session, task, report)
            if profile.should_stop(report, item):
                break
        profile.persist(session)
        profile.finish(console, report)
        return report
