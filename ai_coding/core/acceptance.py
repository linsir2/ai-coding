"""Proof command runner (R0-1): run the verification commands, bounded.

``update_plan``'s ``plan_verify_items`` runs one proof command at the item's
completion boundary, and this is the only place a shell runs for it.  Each
command executes in the workspace with a per-command timeout and a total cap,
so one hung suite cannot eat the turn it was meant to close.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ai_coding.infra.subprocess_runner import run_process_async
from ai_coding.security.approval import ApprovalCallback, ApprovalResult
from ai_coding.security.permission_gate import bash_denied

_DEFAULT_EACH_SECONDS = 120.0
_DEFAULT_TOTAL_SECONDS = 300.0
_OUTPUT_TAIL = 500
#: The verdict of a command the total budget left no room for.  Fail-closed:
#: "not checked" is never a pass.
_BUDGET_EXHAUSTED = "not checked: time budget exhausted"

#: A path-shaped literal inside an otherwise opaque argument.  This is for
#: ``python -c "open('../x')"``: the shell correctly sees one argument, and
#: the command is free to interpret the path inside it.  URL prefixes are
#: removed before this runs -- ``https://x/a/b`` names a resource, not a
#: local path.
_STRING_PATH_RE = re.compile(
    r"(?:~|/|\.\.?/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@+-]+)+"
)
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")

#: A narrow view of the rule layer: the reason this proof command is refused,
#: or ``""``.  This module does not import the gate, so its dependency face
#: stays the same as before M1a.
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
    from ai_coding.core.workspace_guard import outside_workspace_paths

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
            + "。验证命令由 shell 执行，能访问工作区外的路径。"
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

    def outside(text: str, base: Path) -> bool:
        try:
            resolved = (base / text).expanduser().resolve()
            return sandbox._contained_in(resolved) is None
        except (OSError, RuntimeError, SandboxViolation):
            return True

    def remember(text: str, base: Path) -> None:
        if text and outside(text, base) and text not in offenders:
            offenders.append(text)

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
            remember(text, cwd)
        # A quoted argument can hide the path from the shell's word split
        # (``python -c "open('../x')"``).  Read the obvious literal pieces
        # inside it; this deliberately does not attempt to interpret Python,
        # and dynamic construction remains outside this layer's promise.
        for word in (*view.arguments, *view.redirects):
            for fragment in _path_fragments(str(word or "")):
                remember(fragment, cwd)
    return offenders


def _path_fragments(text: str) -> list[str]:
    """Obvious local-path literals inside one shell argument.

    Quotes and punctuation around a literal are not part of it.  A token
    needs at least two concrete segments, so a bare ``../`` used for runtime
    concatenation is not mistaken for a discoverable target.  The caller
    still makes the containment decision with the sandbox.

    Single-segment credential literals (``open('.env')``) are deliberately
    out of scope here: this layer only closes workspace-outside paths, and
    a bare credential name is closed by the read/grep tool layer instead.
    """
    cleaned = _URL_RE.sub(" ", text)
    found: list[str] = []
    for match in _STRING_PATH_RE.finditer(cleaned):
        token = match.group(0).rstrip(".,:;()[]{}<>\"'")
        if token and token not in found:
            found.append(token)
    return found


async def verify_items(
    commands: Sequence[str],
    workspace: str,
    *,
    timeout_each: float = _DEFAULT_EACH_SECONDS,
    timeout_total: float = _DEFAULT_TOTAL_SECONDS,
    approval: ApprovalCallback | None = None,
    deny_reason: DenyReason | None = None,
    extra_roots: Sequence[str] = (),
) -> list[tuple[bool, str]]:
    """Run every command once; return ``(passed, output tail)`` per command.

    The commands run in order, each bounded by ``timeout_each`` and all of
    them by ``timeout_total``.  A command the total cap leaves no room for
    was not checked, which is never a pass.

    ``approval`` is the human in the loop for a proof command that reaches
    outside the workspace (W2).  Without it such a command is refused rather
    than run: the proof commands bypass the dispatch gate, so this is the
    only place a boundary can be put on them.

    ``deny_reason`` is the rule layer (M1a): called as
    ``deny_reason("bash", {"command": cmd})`` before anything runs, and a
    non-empty answer fails the command.  It is a callback rather than the gate
    so this module keeps its small dependency face.  ``ask`` deliberately does
    not count here: an ordinary ``pytest`` must not become a prompt.
    """
    results: list[tuple[bool, str]] = []
    spent = 0.0
    for command in commands:
        if spent >= timeout_total:
            results.append((False, _BUDGET_EXHAUSTED))
            continue
        if deny_reason is not None:
            try:
                denied = deny_reason("bash", {"command": command})
            except Exception:  # noqa: BLE001 — a boundary that cannot read fails closed
                denied = "the permission rules could not be read"
            if denied:
                results.append((False, _tail(f"refused: {denied}")))
                continue
        reason = _needs_a_human(command, workspace, extra_roots=extra_roots)
        if reason and not await _approved(command, reason, approval):
            results.append((False, _tail(f"refused: {reason}")))
            continue
        started = time.monotonic()
        try:
            outcome = await run_process_async(
                command,
                shell=True,
                cwd=workspace,
                timeout=min(timeout_each, max(timeout_total - spent, 1.0)),
            )
        except Exception as exc:  # noqa: BLE001 — a check that cannot run fails
            results.append((False, _tail(f"error: {exc}")))
            continue
        finally:
            spent += time.monotonic() - started
        if outcome.timed_out:
            tail = (outcome.stdout or outcome.stderr or "").strip()
            results.append((
                False,
                _tail(f"timeout after {timeout_each:g}s: {tail}"),
            ))
        elif outcome.error is not None:
            results.append((False, _tail(f"error: {outcome.error}")))
        elif outcome.returncode != 0:
            body = (outcome.stdout or "") + (("\n" + outcome.stderr) if outcome.stderr else "")
            results.append((False, _tail(body)))
        else:
            results.append((True, _tail(outcome.stdout)))
    return results


async def _approved(
    command: str, reason: str, approval: ApprovalCallback | None
) -> bool:
    """Did the human let this proof command run?

    No one to ask means no: a proof command that reaches outside the
    workspace is not something to run on its own.
    """
    if approval is None:
        return False
    try:
        answer = await approval.approve("bash", {"command": command}, reason)
    except Exception:  # noqa: BLE001 — an approval is a boundary: fail closed
        return False
    return answer in (ApprovalResult.YES, ApprovalResult.ALWAYS)


__all__ = ["verify_items"]

