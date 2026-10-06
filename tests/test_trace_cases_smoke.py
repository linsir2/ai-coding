"""Smoke: the offline observability case runner, in CI.

``docs/r4-plan.md`` T29 planned this file next to ``scripts/trace_cases.py`` and
it was never written, so the runner had no CI coverage: a case with a dead
import or a broken signature surfaced only when somebody ran the script by
hand.  That is how ``ScopedEvent`` came to be measured as absent — a probe read
``evt == "ScopedEvent"``, which ``TraceRecorder._describe`` never writes, and
reported 0 while 343 scoped rows sat in one real turn file.

The assertion logic stays in ``scripts/trace_cases.py``; this only runs the
runner and checks the exit code, so there is one copy of it rather than two
that can drift.  It deliberately does not assert the whole offline set: c1, c6
and c7 are red against current behaviour, so demanding ``main() == 0`` here
would fail for reasons that have nothing to do with what this protects.
"""

import pytest


@pytest.mark.asyncio
async def test_the_scoped_trace_case_passes():
    from scripts import trace_cases

    # `main(only=...)` returns 0 for an *empty* selection — `0 == len([])` — so
    # without this the test would sit green on the day c10 does not exist.
    assert "c10" in dict(trace_cases.CASES)
    assert await trace_cases.main(only="c10") == 0
