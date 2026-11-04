"""TDD (P2-B1): WriteCoordinator — path claims, workspace holds, run overlap.

The coordinator is the one thing two concurrent writers in the same process
share: same path serialises, different paths run together, and a workspace-wide
claim (``*``, what an opaque bash command takes) excludes every path in that
root.  Roots are compared too, so an isolated worktree never blocks the main
tree.

All of this has to be safe across threads as well as tasks: the async claim is
polled from the event loop, while ``acquire_hold``/``release_hold`` are called
synchronously from job watcher threads (a background bash command owns its
workspace-wide claim until it settles).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ai_coding.core.write_coordinator import WriteCoordinator


def _coord(tmp_path) -> WriteCoordinator:
    return WriteCoordinator(tmp_path)


def _root(tmp_path) -> str:
    return str(tmp_path)


@pytest.mark.asyncio
async def test_the_same_path_is_serialised(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)
    target = str(tmp_path / "x.py")

    first = await coord.claim("a", root, (target,), timeout=1.0)
    assert first is not None
    waiter = asyncio.ensure_future(coord.claim("b", root, (target,), timeout=0.5))
    await asyncio.sleep(0.05)

    assert not waiter.done(), "a second owner must not hold the same path"
    first.release()
    second = await waiter
    assert second is not None
    second.release()


@pytest.mark.asyncio
async def test_different_paths_run_together(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)

    first = await coord.claim("a", root, (str(tmp_path / "a.py"),), timeout=1.0)
    second = await coord.claim("b", root, (str(tmp_path / "b.py"),), timeout=1.0)

    assert first is not None and second is not None
    first.release()
    second.release()


@pytest.mark.asyncio
async def test_a_workspace_claim_excludes_every_path_in_that_root(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)

    workspace = await coord.claim("a", root, (), timeout=1.0)
    assert workspace is not None
    waiter = asyncio.ensure_future(
        coord.claim("b", root, (str(tmp_path / "x.py"),), timeout=0.5)
    )
    await asyncio.sleep(0.05)

    assert not waiter.done(), "'*' means the whole workspace"
    workspace.release()
    assert await waiter is not None


@pytest.mark.asyncio
async def test_a_path_claim_blocks_a_later_workspace_claim(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)

    first = await coord.claim("a", root, (str(tmp_path / "x.py"),), timeout=1.0)
    assert first is not None
    waiter = asyncio.ensure_future(coord.claim("b", root, (), timeout=0.4))
    await asyncio.sleep(0.05)

    assert not waiter.done(), "the '*' waiter must see the held path"
    first.release()
    assert await waiter is not None


@pytest.mark.asyncio
async def test_different_roots_do_not_conflict(tmp_path):
    coord = _coord(tmp_path)
    main = str(tmp_path / "main")
    other = str(tmp_path / "worktree")

    first = await coord.claim("a", main, (), timeout=1.0)
    second = await coord.claim("b", other, (), timeout=0.2)

    assert first is not None and second is not None
    first.release()
    second.release()


@pytest.mark.asyncio
async def test_a_claim_times_out_after_its_bounded_wait(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)
    target = str(tmp_path / "x.py")

    first = await coord.claim("a", root, (target,), timeout=1.0)
    assert first is not None
    started = time.monotonic()
    denied = await coord.claim("b", root, (target,), timeout=0.1)
    elapsed = time.monotonic() - started

    assert denied is None
    assert elapsed >= 0.09
    first.release()


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaks_nothing(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)
    target = str(tmp_path / "x.py")

    first = await coord.claim("a", root, (target,), timeout=1.0)
    assert first is not None
    waiter = asyncio.ensure_future(coord.claim("b", root, (target,), timeout=5.0))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    first.release()
    later = await coord.claim("c", root, (target,), timeout=0.5)
    assert later is not None, "a cancelled waiter must not have taken the path"
    later.release()


@pytest.mark.asyncio
async def test_the_same_owner_can_reenter_and_counts_releases(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)
    target = str(tmp_path / "x.py")

    first = await coord.claim("a", root, (target,), timeout=1.0)
    second = await coord.claim("a", root, (target,), timeout=1.0)
    assert first is not None and second is not None

    first.release()
    assert await coord.claim("b", root, (target,), timeout=0.1) is None, (
        "one release must not drop the re-entrant hold"
    )
    second.release()
    later = await coord.claim("b", root, (target,), timeout=0.5)
    assert later is not None
    later.release()


@pytest.mark.asyncio
async def test_a_synchronous_hold_blocks_an_async_claim_until_released(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)

    hold = coord.acquire_hold("a", root)
    assert hold is not None

    waiter = asyncio.ensure_future(coord.claim("b", root, (), timeout=0.2))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    assert await waiter is None

    coord.release_hold(hold)
    claimed = await coord.claim("b", root, (), timeout=0.5)
    assert claimed is not None
    claimed.release()


def test_a_hold_is_reentrant_for_its_owner(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)

    first = coord.acquire_hold("a", root)
    second = coord.acquire_hold("a", root)
    assert first is not None and second is not None
    assert first != second

    coord.release_hold(first)
    assert coord.acquire_hold("b", root) is None, "the second hold still stands"
    coord.release_hold(second)
    assert coord.acquire_hold("b", root) is not None


def test_release_hold_is_idempotent(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)

    hold = coord.acquire_hold("a", root)
    assert hold is not None
    coord.release_hold(hold)
    coord.release_hold(hold)  # the job callback and the tool may both say it

    assert coord.acquire_hold("b", root) is not None
    assert coord.acquire_hold("c", root) is None


def test_overlap_names_the_other_run_and_excludes_its_own_key(tmp_path):
    coord = _coord(tmp_path)
    coord.register_run("run-a", (str(tmp_path / "a.py"),), "s1")
    coord.register_run("run-b", (str(tmp_path / "b.py"),), "s2")

    hits = coord.overlap("run-b", (str(tmp_path / "a.py"),))
    assert hits and hits[0]["key"] == "run-a"
    assert hits[0]["origin_session"] == "s1"
    assert hits[0]["paths"] == ("a.py",)

    assert coord.overlap("run-a", (str(tmp_path / "a.py"),)) == (), (
        "a run does not conflict with itself"
    )
    assert coord.overlap("run-b", (str(tmp_path / "b.py"),)) == ()
    assert coord.overlap("run-c", (str(tmp_path / "nope.py"),)) == ()


def test_register_run_unions_a_second_turn_in_the_same_window(tmp_path):
    """A multi-turn child must not lose turn 1's paths when turn 2 registers."""
    coord = _coord(tmp_path)
    coord.register_run("run-a", (str(tmp_path / "a.py"),), "s1")
    coord.register_run("run-a", (str(tmp_path / "b.py"),), "s2")

    hits = coord.overlap("run-b", (str(tmp_path / "a.py"), str(tmp_path / "b.py")))
    assert hits and hits[0]["key"] == "run-a"
    assert hits[0]["paths"] == ("a.py", "b.py")
    assert hits[0]["origin_session"] == "s2"


def test_register_run_ignores_an_empty_path_set(tmp_path):
    coord = _coord(tmp_path)
    coord.register_run("run-a", (), "s1")

    assert coord.overlap("run-b", (str(tmp_path / "a.py"),)) == ()


def test_overlap_compares_relative_paths(tmp_path):
    coord = _coord(tmp_path)
    coord.register_run("run-a", ("sub/a.py",), "s1")

    hits = coord.overlap("run-b", (str(tmp_path / "sub" / "a.py"),))
    assert hits and hits[0]["paths"] == ("sub/a.py",)


def test_overlap_ignores_runs_older_than_the_window(tmp_path, monkeypatch):
    from ai_coding.core import write_coordinator as module

    monkeypatch.setattr(module, "RUN_TTL_SECONDS", 0.01)
    coord = _coord(tmp_path)
    coord.register_run("run-a", (str(tmp_path / "a.py"),), "s1")
    time.sleep(0.02)

    assert coord.overlap("run-b", (str(tmp_path / "a.py"),)) == ()


def test_only_the_twenty_most_recent_runs_are_compared(tmp_path, monkeypatch):
    from ai_coding.core import write_coordinator as module

    monkeypatch.setattr(module, "MAX_RUNS", 20)
    coord = _coord(tmp_path)
    for index in range(21):
        coord.register_run(f"run-{index:02d}", (str(tmp_path / "a.py"),), "s")

    hits = coord.overlap("run-new", (str(tmp_path / "a.py"),))
    assert len(hits) == 20
    assert "run-00" not in {hit["key"] for hit in hits}


def test_the_subagent_runner_uses_the_injected_coordinator(tmp_path):
    """P2-B2: the overlap table is the same process-wide table the dispatcher
    claims against — a second instance would never see the other run."""
    from ai_coding.core.subagent import SubAgentRunner

    coord = _coord(tmp_path)
    runner = SubAgentRunner(
        object(),
        engine_factory=lambda: None,
        build_tools=lambda _workdir: None,
        workspace=str(tmp_path),
        write_coordinator=coord,
    )
    assert runner._write_coordinator is coord  # noqa: SLF001
    assert runner._active == set()  # noqa: SLF001


def test_blockers_names_the_other_owner_for_a_conflict(tmp_path):
    coord = _coord(tmp_path)
    root = _root(tmp_path)
    hold = coord.acquire_hold("job-session", root)
    assert hold is not None
    assert coord.blockers("other", root, ()) == ("job-session",)
    assert coord.blockers("job-session", root, ()) == ()
    coord.release_hold(hold)
    assert coord.blockers("other", root, ()) == ()
