"""Acceptance verification runner (R0-1): run the proof commands, bounded.

This is the IO half of the contract — ``domain/acceptance.py`` is the data,
``TurnPolicy`` is the decision, and this is the only place a shell runs.
Each item's ``verify`` executes in the workspace with a per-item timeout and
a total cap, so one hung suite cannot eat the turn it was meant to close.
Already-passed items are skipped: re-running a green suite every iteration
is exactly the wall-clock burn the experiments measured.
"""

from __future__ import annotations

import time
from dataclasses import replace

from ai_coding.domain.acceptance import AcceptanceItem, pending_items
from ai_coding.infra.subprocess_runner import run_process_async

_DEFAULT_EACH_SECONDS = 120.0
_DEFAULT_TOTAL_SECONDS = 300.0
_OUTPUT_TAIL = 500


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= _OUTPUT_TAIL:
        return text
    return f"…{text[-_OUTPUT_TAIL:]}"


async def verify_items(
    items: list[AcceptanceItem],
    workspace: str,
    *,
    timeout_each: float = _DEFAULT_EACH_SECONDS,
    timeout_total: float = _DEFAULT_TOTAL_SECONDS,
) -> list[AcceptanceItem]:
    """Check every not-yet-passed item; return the items with verdicts filled.

    The input list is never mutated — a fresh list comes back, and the caller
    decides what to persist.  Items left unchecked by the total cap keep
    ``passed=None`` (fail-closed downstream).
    """
    checked: list[AcceptanceItem] = []
    spent = 0.0
    for item in pending_items(items):
        if spent >= timeout_total:
            checked.append(item)
            continue
        started = time.monotonic()
        try:
            outcome = await run_process_async(
                item.verify,
                shell=True,
                cwd=workspace,
                timeout=min(timeout_each, max(timeout_total - spent, 1.0)),
            )
        except Exception as exc:  # noqa: BLE001 — a check that cannot run fails
            checked.append(replace(item, passed=False, output=_tail(f"error: {exc}")))
            continue
        finally:
            spent += time.monotonic() - started
        if outcome.timed_out:
            tail = (outcome.stdout or outcome.stderr or "").strip()
            checked.append(replace(
                item, passed=False,
                output=_tail(f"timeout after {timeout_each:g}s: {tail}"),
            ))
        elif outcome.error is not None:
            checked.append(replace(item, passed=False, output=_tail(f"error: {outcome.error}")))
        elif outcome.returncode != 0:
            body = (outcome.stdout or "") + (("\n" + outcome.stderr) if outcome.stderr else "")
            checked.append(replace(item, passed=False, output=_tail(body)))
        else:
            checked.append(replace(item, passed=True, output=_tail(outcome.stdout)))
    by_id = {item.id: item for item in checked}
    return [by_id.get(item.id, item) for item in items]


__all__ = ["verify_items"]
