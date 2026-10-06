"""Smoke: the offline observability case runner, in CI.

``docs/r4-plan.md`` T29 planned this file next to ``scripts/trace_cases.py`` and
it was never written, so the runner had no CI coverage: a case with a dead
import or a broken signature surfaced only when somebody ran the script by
hand.  That is how ``ScopedEvent`` came to be measured as absent — a probe read
``evt == "ScopedEvent"``, which ``TraceRecorder._describe`` never writes, and
reported 0 while 343 scoped rows sat in one real turn file.

The assertion logic stays in ``scripts/trace_cases.py``; this only runs the
runner and checks the exit code, so there is one copy of it rather than two
that can drift.
"""

import pytest


@pytest.mark.asyncio
async def test_the_scoped_trace_case_passes():
    from scripts import trace_cases

    # `main(only=...)` returns 0 for an *empty* selection — `0 == len([])` — so
    # without this the test would sit green on the day c10 does not exist.
    assert "c10" in dict(trace_cases.CASES)
    assert await trace_cases.main(only="c10") == 0


@pytest.mark.asyncio
async def test_the_whole_offline_gate_is_green():
    """The harness itself used to be the broken thing.

    `_loop` handed the recorder to `AgentLoop` twice — once as
    `observer=Braid(recorder)`, once as `trace=recorder` — and
    `_bind_observer` braids the trace in regardless, so every event was written
    twice.  c1, c6 and c7 each assert an exact count and were all red for that
    one reason.  Production was never affected: `ui/textual_app.py` passes only
    `trace=recorder`.

    Nothing pinned the runner, which is why it stayed broken. This does.
    """
    from scripts import trace_cases

    assert await trace_cases.main() == 0
