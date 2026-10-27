"""M-G3: the live layer must not skip itself into a green build.

Every live test is ``skipif`` on ``AI_CODING_MCP_STDIO``.  A CI job that forgets
to set it collects the whole layer, skips it, and exits 0 -- green while covering
nothing, which is the exact failure this milestone exists to remove (``-rs`` only
prints the reason; it does not fail).

This file is marked ``live`` but deliberately **not** skipif-gated.  It is inert
in an ordinary ``pytest`` run (nothing asked for the live layer) and it fails
when ``-m live`` was asked for without the gate variable -- which is precisely
the CI mistake it exists to catch.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

GATE = "AI_CODING_MCP_STDIO"


def test_the_live_gate_variable_is_set(pytestconfig) -> None:
    selection = str(pytestconfig.option.markexpr or "")
    if "live" not in selection:
        pytest.skip("only meaningful when the live layer is explicitly selected")

    assert os.environ.get(GATE) == "1", (
        f"{GATE} is not set, so every other live test just skipped: "
        "this job would be green while covering nothing"
    )
