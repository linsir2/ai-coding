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
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ai_coding.domain.acceptance import AcceptanceItem, pending_items
from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.security.permission_gate import bash_denied
from ai_coding.shell_parse import command_views

_DEFAULT_EACH_SECONDS = 120.0
_DEFAULT_TOTAL_SECONDS = 300.0
_OUTPUT_TAIL = 500

#: A narrow view of the rule layer: the reason this proof command is refused,
#: or ``""``.  Acceptance does not import the gate, so its dependency face stays
#: the same as before M1a.
DenyReason = Callable[[str, dict[str, Any]], str]


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) > _OUTPUT_TAIL:
        return f"…{text[-_OUTPUT_TAIL:]}"
    return text


def _needs_a_human(
    command: str, workspace: str, *, extra_roots: Sequence[str] = ()
) -> str:
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
    # A proof command is a command, not a sentence: the shell grammar reads
    # the paths a text scan cannot see (the one behind a variable, the one
    # inside a command substitution, the one a directory command names).
    offenders = outside_workspace_paths(
        [command], workspace, extra_roots=tuple(extra_roots), from_shell=True
    )
    if not offenders:
        offenders = _relative_targets_outside(command, workspace, extra_roots)
    if offenders:
        return (
            "目标路径在工作区外："
            + ", ".join(offenders)
            + "。验收命令由 shell 执行，能访问工作区外的路径。"
        )
    return ""


def _relative_targets_outside(
    command: str, workspace: str, extra_roots: Sequence[str]
) -> list[str]:
    """Relative words that resolve outside every root, from the shell grammar.

    ``path_candidates`` only reports absolute-looking words, on purpose: a
    prose path is not a command argument.  A proof *is* a command, though, so
    ``cat ../secret.txt`` names a real target.  The same containment test as
    the sandbox is used here (resolve, then ``relative_to``), so this adds no
    second definition of "inside".
    """
    from ai_coding.security.sandbox import Sandbox, SandboxViolation
    from ai_coding.shell_parse import command_views

    sandbox = Sandbox(workspace, extra_roots=tuple(extra_roots))
    offenders: list[str] = []
    # ``cd`` changes the directory every later command runs in.  Tracking it
    # is the difference between "this word is a bare filename" and "this
    # filename is outside the workspace".
    cwd = Path(workspace)
    for view in command_views(command):
        if view.program == "cd":
            target = next((w for w in view.arguments if not w.startswith("-")), "")
            if target:
                cwd = (cwd / target).expanduser().resolve()
            continue
        for word in (*view.arguments, *view.redirects):
            text = str(word or "")
            if not text or text.startswith(("-", "$", "&")):
                continue
            # Absolute words are path_candidates' job; this is the relative
            # half.  A bare word is a place only when ``cd`` moved us: in
            # ``cd .. && cat secret.txt`` the second word is the target.
            if text.startswith(("/", "~")):
                continue
            if "/" not in text and cwd == Path(workspace).resolve():
                continue
            try:
                target_path = (cwd / text).expanduser()
                resolved = target_path.resolve()
                if sandbox._contained_in(resolved) is None:
                    raise SandboxViolation(text)
            except SandboxViolation:
                if text not in offenders:
                    offenders.append(text)
    return offenders


async def verify_items(
    items: list[AcceptanceItem],
    workspace: str,
    *,
    timeout_each: float = _DEFAULT_EACH_SECONDS,
    timeout_total: float = _DEFAULT_TOTAL_SECONDS,
    approval: ApprovalCallback | None = None,
    deny_reason: DenyReason | None = None,
    extra_roots: Sequence[str] = (),
) -> list[AcceptanceItem]:
    """Check every not-yet-passed item; return the items with verdicts filled.

    The input list is never mutated — a fresh list comes back, and the caller
    decides what to persist.  Items left unchecked by the total cap keep
    ``passed=None`` (fail-closed downstream).

    ``approval`` is the human in the loop for a proof command that reaches
    outside the workspace (W2).  Without it such a command is refused rather
    than run: the proof commands bypass the dispatch gate, so this is the
    only place a boundary can be put on them.

    ``deny_reason`` is the rule layer (M1a): called as
    ``deny_reason("bash", {"command": cmd})`` before anything runs, and a
    non-empty answer fails the item.  It is a callback rather than the gate so
    this module keeps its small dependency face.  ``ask`` deliberately does
    not count here: an ordinary ``pytest`` must not become a prompt.
    """
    checked: list[AcceptanceItem] = []
    spent = 0.0
    for item in pending_items(items):
        if spent >= timeout_total:
            checked.append(item)
            continue
        if deny_reason is not None:
            try:
                denied = deny_reason("bash", {"command": item.verify})
            except Exception:  # noqa: BLE001 — a boundary that cannot read fails closed
                denied = "the permission rules could not be read"
            if denied:
                checked.append(
                    replace(item, passed=False, output=_tail(f"refused: {denied}"))
                )
                continue
        reason = _needs_a_human(item.verify, workspace, extra_roots=extra_roots)
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


async def verify_command_problem(command: str) -> str:
    """Why this proof command cannot run, or ``""`` when it can.

    Two questions, in the order a reader asks them: can the shell read
    it at all, and is every program in it something the shell can
    actually run?  The second one is the one that catches a model
    inventing a proof -- ``glob '**/*'`` is valid shell and names no
    program, so the proof would fail at execution, after the operator
    had already approved the plan.  The plan is where the model can
    still fix it, so that is where the question belongs.

    The oracle for "can the shell run it" is the shell itself
    (``command -v``), because that is what will run the proof: it knows
    the builtins and the PATH, so a check that disagreed with it would
    refuse proofs that would have worked.
    """
    views = command_views(command)
    if not views:
        return "验收命令里没有可执行的命令"
    broken = [view.program or "?" for view in views if view.broken]
    if broken:
        return "验收命令的语法读不完整：" + ", ".join(broken)
    programs: list[str] = []
    for view in views:
        # A program that names a path is the model's own business: the
        # check below looks the name up on PATH, and a relative path would
        # be judged against the wrong directory.
        if "/" in view.program or not view.program:
            continue
        if view.program not in programs:
            programs.append(view.program)
    missing = await _unresolvable_programs(programs)
    if missing:
        return "验收命令里的程序不存在：" + ", ".join(missing)
    return ""


async def _unresolvable_programs(programs: list[str]) -> list[str]:
    """Which of these programs the shell would not be able to run."""
    if not programs:
        return []
    script = 'for p do command -v "$p" >/dev/null || printf "%s\\n" "$p"; done'
    outcome = await run_process_async(
        ["/bin/sh", "-c", script, "sh", *programs],
        shell=False,
        timeout=10.0,
    )
    if getattr(outcome, "returncode", 0) != 0:
        # The oracle itself failed: say nothing, so a working proof is not
        # refused because the check could not run.
        return []
    return [line for line in (getattr(outcome, "stdout", "") or "").splitlines() if line.strip()]
