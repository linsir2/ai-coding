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
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.security.permission_gate import bash_denied

_DEFAULT_EACH_SECONDS = 120.0
_DEFAULT_TOTAL_SECONDS = 300.0
_OUTPUT_TAIL = 500


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) > _OUTPUT_TAIL:
        return f"…{text[-_OUTPUT_TAIL:]}"
    return text


def _needs_a_human(command: str, workspace: str) -> str:
    """Why this proof command has to be asked about, or ``""`` when it does not.

    A proof command is a shell command, and a shell has no sandbox: it can
    write outside the workspace, read files nobody meant it to read, and run
    the commands the bash tool refuses outright.  It used to run all of that
    silently -- the proof commands bypass the dispatch gate by design (the
    loop's own comment says so), and nothing put a boundary back.

    Two questions, and the first one is the one that decides:

    * does the command name a path the file tools cannot reach?  Then the
      plan is asking for something the workspace does not allow, and the
      human should know that before it happens;
    * does the command hit one of the bash tool's hard red lines?  Then it
      is not a proof command at all, and the bash tool's own verdict
      applies -- no asking.

    Everything else -- the ordinary ``test -f`` and ``pytest`` -- runs
    without a click, because that is what a proof command is for.
    """
    from ai_coding.core.plan_runner import outside_workspace_paths

    reason = bash_denied(command)
    if reason:
        return f"bash command blocked: {reason}"
    offenders = outside_workspace_paths([command], workspace)
    if offenders:
        return (
            "目标路径在工作区外："
            + ", ".join(offenders)
            + "。验收命令由 shell 执行，能访问工作区外的路径。"
        )
    return ""


async def verify_items(
    items: list[AcceptanceItem],
    workspace: str,
    *,
    timeout_each: float = _DEFAULT_EACH_SECONDS,
    timeout_total: float = _DEFAULT_TOTAL_SECONDS,
    approval: ApprovalCallback | None = None,
) -> list[AcceptanceItem]:
    """Check every not-yet-passed item; return the items with verdicts filled.

    The input list is never mutated — a fresh list comes back, and the caller
    decides what to persist.  Items left unchecked by the total cap keep
    ``passed=None`` (fail-closed downstream).

    ``approval`` is the human in the loop for a proof command that reaches
    outside the workspace (W2).  Without it such a command is refused rather
    than run: the proof commands bypass the dispatch gate, so this is the
    only place a boundary can be put on them.
    """
    checked: list[AcceptanceItem] = []
    spent = 0.0
    for item in pending_items(items):
        if spent >= timeout_total:
            checked.append(item)
            continue
        reason = _needs_a_human(item.verify, workspace)
        if reason and not await _approved(item, reason, approval):
            checked.append(replace(item, passed=False, output=_tail(f"refused: {reason}")))
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


async def _approved(
    item: AcceptanceItem, reason: str, approval: ApprovalCallback | None
) -> bool:
    """Did the human let this proof command run?

    No one to ask means no: a proof command that reaches outside the
    workspace is not something to run on its own.
    """
    if approval is None:
        return False
    try:
        answer = await approval.approve("bash", {"command": item.verify}, reason)
    except Exception:  # noqa: BLE001 — an approval is a boundary: fail closed
        return False
    return answer in (ApprovalResult.YES, ApprovalResult.ALWAYS)


__all__ = ["verify_items"]
