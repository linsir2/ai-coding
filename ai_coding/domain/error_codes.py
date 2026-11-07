"""Canonical tool-error codes (gRPC status family) + legacy detail codes.

Why a family:
    ``ToolResult.error`` used to carry ~100 different snake_case leaves, so no
    consumer could write a policy against it (``error == "stale_read"`` and
    ``error == "timeout"`` had nothing in common).  The wire contract is now the
    standard gRPC status set -- the same vocabulary as ``INVALID_ARGUMENT`` /
    ``FAILED_PRECONDITION`` / ``UNAVAILABLE`` -- borrowed because every client
    library and every engineer already knows the retry semantics of those names.

Why a detail:
    Collapsing ``old_string_not_found`` and ``file_not_found`` into one family
    loses the one thing the model and the operator need: *which* precondition
    failed.  The specific leaf is therefore kept in ``ToolResult.detail`` and,
    on the wire, as ``code="..."`` next to ``error="FAMILY"``.  ``error`` is the
    contract; ``detail`` is the diagnostic.  New code should emit a family and
    may omit the detail.

Compatibility:
    Old sessions, traces and third-party tools still carry leaves.  Everything
    that enters through :class:`ai_coding.domain.message.ToolResult` is
    normalised by ``normalize()``, so a leaf becomes ``(family, leaf)`` and an
    unknown string becomes ``(UNKNOWN, original)``.  Readers never compare
    against a leaf again.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

__all__ = [
    "CANCELLED_BY_USER",
    "ErrorFamily",
    "ToolError",
    "family_of",
    "is_family",
    "is_neutral",
    "is_retryable",
    "normalize",
    "severity_of",
    "tone_of",
]

#: The one leaf that keeps a policy meaning of its own: the user pressed Esc.
#: Re-exported from ``domain.message``'s historical constant.
CANCELLED_BY_USER = "cancelled_by_user"


class ToolError(str, Enum):
    """The gRPC status codes, as the canonical ``ToolResult.error`` values."""

    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    FAILED_PRECONDITION = "FAILED_PRECONDITION"
    ABORTED = "ABORTED"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    UNIMPLEMENTED = "UNIMPLEMENTED"
    INTERNAL = "INTERNAL"
    UNAVAILABLE = "UNAVAILABLE"
    DATA_LOSS = "DATA_LOSS"
    UNAUTHENTICATED = "UNAUTHENTICATED"


#: Alias kept for readability at call sites that only talk about the taxonomy.
ErrorFamily = ToolError

_FAMILY_VALUES: frozenset[str] = frozenset(member.value for member in ToolError)

#: Legacy leaf -> family.  This table is the *only* place the old vocabulary
#: lives; producers may keep emitting leaves (a normalisation hop turns them
#: into ``(family, leaf)``), but consumers must read the family.
LEGACY_TO_FAMILY: dict[str, ToolError] = {
    # -- cancelled / stopped -------------------------------------------------
    "cancelled": ToolError.CANCELLED,
    "cancelled_by_user": ToolError.CANCELLED,
    "stopped": ToolError.CANCELLED,
    "skipped": ToolError.CANCELLED,
    "killed": ToolError.CANCELLED,
    # -- the model asked for something that is not a tool --------------------
    "unknown_tool": ToolError.UNIMPLEMENTED,
    # -- clocks --------------------------------------------------------------
    "timeout": ToolError.DEADLINE_EXCEEDED,
    "tool_timeout": ToolError.DEADLINE_EXCEEDED,
    "child_timeout": ToolError.DEADLINE_EXCEEDED,
    "approval_timeout": ToolError.DEADLINE_EXCEEDED,
    # -- missing objects -----------------------------------------------------
    "unknown_id": ToolError.NOT_FOUND,
    "unknown_agent": ToolError.NOT_FOUND,
    "unknown_job": ToolError.NOT_FOUND,
    "no_jobs": ToolError.NOT_FOUND,
    "not_found": ToolError.NOT_FOUND,
    "no_transcript": ToolError.NOT_FOUND,
    "no_goal": ToolError.NOT_FOUND,
    "ambiguous": ToolError.NOT_FOUND,
    "ambiguous_target": ToolError.NOT_FOUND,
    "file_not_found": ToolError.NOT_FOUND,
    "unknown_skill": ToolError.NOT_FOUND,
    # -- already in that state ----------------------------------------------
    "already_armed": ToolError.ALREADY_EXISTS,
    "already_concluded": ToolError.ALREADY_EXISTS,
    "plan_already_approved": ToolError.ALREADY_EXISTS,
    # -- policy / boundary refusals -----------------------------------------
    "denied": ToolError.PERMISSION_DENIED,
    "guard_denied": ToolError.PERMISSION_DENIED,
    "hook_blocked": ToolError.PERMISSION_DENIED,
    "not_approved": ToolError.PERMISSION_DENIED,
    "readonly_denied": ToolError.PERMISSION_DENIED,
    "readonly_session": ToolError.PERMISSION_DENIED,
    "readonly_completion": ToolError.PERMISSION_DENIED,
    "sandbox_violation": ToolError.PERMISSION_DENIED,
    "read_only_shell_required": ToolError.PERMISSION_DENIED,
    "credential_approval_required": ToolError.PERMISSION_DENIED,
    "plan_mode": ToolError.PERMISSION_DENIED,
    "plan_directory_required": ToolError.PERMISSION_DENIED,
    "workspace_escape": ToolError.PERMISSION_DENIED,
    "not_in_plan": ToolError.PERMISSION_DENIED,
    "implementation_request": ToolError.PERMISSION_DENIED,
    "url_not_allowed": ToolError.PERMISSION_DENIED,
    "too_few_questions": ToolError.PERMISSION_DENIED,
    "too_many_questions": ToolError.PERMISSION_DENIED,
    "duplicate_question_id": ToolError.PERMISSION_DENIED,
    "bad_questions": ToolError.PERMISSION_DENIED,
    # -- sizes / quotas ------------------------------------------------------
    "content_too_large": ToolError.RESOURCE_EXHAUSTED,
    "schema_too_large": ToolError.RESOURCE_EXHAUSTED,
    "job_limit": ToolError.RESOURCE_EXHAUSTED,
    "agents_exhausted": ToolError.RESOURCE_EXHAUSTED,
    "context_too_long": ToolError.RESOURCE_EXHAUSTED,
    "file_too_large": ToolError.RESOURCE_EXHAUSTED,
    "child_max_iterations": ToolError.RESOURCE_EXHAUSTED,
    # -- order / state preconditions ----------------------------------------
    "must_read_first": ToolError.FAILED_PRECONDITION,
    "stale_read": ToolError.FAILED_PRECONDITION,
    "blocked_by_dependencies": ToolError.FAILED_PRECONDITION,
    "completion_unverified": ToolError.FAILED_PRECONDITION,
    "completion_blocked": ToolError.FAILED_PRECONDITION,
    "legacy_session_unverified": ToolError.FAILED_PRECONDITION,
    "override_unavailable": ToolError.FAILED_PRECONDITION,
    "override_denied": ToolError.FAILED_PRECONDITION,
    "runner_only_status": ToolError.FAILED_PRECONDITION,
    "plan_approval_not_ask_user": ToolError.FAILED_PRECONDITION,
    "directory_unsupported": ToolError.FAILED_PRECONDITION,
    "verify_failed": ToolError.FAILED_PRECONDITION,
    "verify_unavailable": ToolError.FAILED_PRECONDITION,
    "task_busy": ToolError.FAILED_PRECONDITION,
    "subagent_busy": ToolError.FAILED_PRECONDITION,
    "block_threshold": ToolError.FAILED_PRECONDITION,
    "unverifiable_job": ToolError.FAILED_PRECONDITION,
    "adopted_job": ToolError.FAILED_PRECONDITION,
    "nonzero_exit": ToolError.FAILED_PRECONDITION,
    "old_string_not_found": ToolError.FAILED_PRECONDITION,
    "old_string_not_unique": ToolError.FAILED_PRECONDITION,
    "overwrite_would_truncate": ToolError.FAILED_PRECONDITION,
    "not_configured": ToolError.FAILED_PRECONDITION,
    "invalid_plan": ToolError.FAILED_PRECONDITION,
    # -- concurrency / races -------------------------------------------------
    "write_conflict": ToolError.ABORTED,
    "read_raced": ToolError.ABORTED,
    "branch_moved": ToolError.ABORTED,
    "head_moved": ToolError.ABORTED,
    "dirty": ToolError.ABORTED,
    "conflict": ToolError.ABORTED,
    "not_settled": ToolError.ABORTED,
    "busy": ToolError.ABORTED,
    "nothing_to_merge": ToolError.ABORTED,
    # -- bad input -----------------------------------------------------------
    "invalid_params": ToolError.INVALID_ARGUMENT,
    "invalid_arguments": ToolError.INVALID_ARGUMENT,
    "truncated_arguments": ToolError.INVALID_ARGUMENT,
    "invalid_mode": ToolError.INVALID_ARGUMENT,
    "invalid_status": ToolError.INVALID_ARGUMENT,
    "invalid_readonly": ToolError.INVALID_ARGUMENT,
    "invalid_fork_turns": ToolError.INVALID_ARGUMENT,
    "invalid_message": ToolError.INVALID_ARGUMENT,
    "invalid_ref": ToolError.INVALID_ARGUMENT,
    "invalid_brief": ToolError.INVALID_ARGUMENT,
    "unknown_action": ToolError.INVALID_ARGUMENT,
    "unknown_kind": ToolError.INVALID_ARGUMENT,
    "unknown_dependency": ToolError.INVALID_ARGUMENT,
    "self_dependency": ToolError.INVALID_ARGUMENT,
    "missing_content": ToolError.INVALID_ARGUMENT,
    "missing_id": ToolError.INVALID_ARGUMENT,
    "missing_note": ToolError.INVALID_ARGUMENT,
    "missing_objective": ToolError.INVALID_ARGUMENT,
    "missing_question": ToolError.INVALID_ARGUMENT,
    "missing_summary": ToolError.INVALID_ARGUMENT,
    "missing_task": ToolError.INVALID_ARGUMENT,
    "missing_target": ToolError.INVALID_ARGUMENT,
    "missing_text": ToolError.INVALID_ARGUMENT,
    "missing_agent_id": ToolError.INVALID_ARGUMENT,
    "missing_override_reason": ToolError.INVALID_ARGUMENT,
    "empty_query": ToolError.INVALID_ARGUMENT,
    "empty_job_id": ToolError.INVALID_ARGUMENT,
    "bad_evidence": ToolError.INVALID_ARGUMENT,
    "schema_invalid": ToolError.INVALID_ARGUMENT,
    "missing_pattern": ToolError.INVALID_ARGUMENT,
    "missing_url": ToolError.INVALID_ARGUMENT,
    "invalid_format": ToolError.INVALID_ARGUMENT,
    "invalid_pattern": ToolError.INVALID_ARGUMENT,
    "invalid_skill": ToolError.INVALID_ARGUMENT,
    "missing_name": ToolError.INVALID_ARGUMENT,
    "invalid_skill_name": ToolError.INVALID_ARGUMENT,
    "missing_old_string": ToolError.INVALID_ARGUMENT,
    "missing_command": ToolError.INVALID_ARGUMENT,
    "missing_query": ToolError.INVALID_ARGUMENT,
    # -- dependency / mechanism unavailable ---------------------------------
    "mcp_error": ToolError.UNAVAILABLE,
    # MCP-07: three leaves instead of one, so a timeout, a dead transport and
    # a remote tool's own refusal stop looking the same in a trace or a retry
    # decision.  ``mcp_error`` stays for old traces and third-party producers.
    "mcp_timeout": ToolError.DEADLINE_EXCEEDED,
    "mcp_transport": ToolError.UNAVAILABLE,
    "mcp_tool_error": ToolError.UNAVAILABLE,
    "memory_error": ToolError.UNAVAILABLE,
    "memory_unavailable": ToolError.UNAVAILABLE,
    "sandbox_unavailable": ToolError.UNAVAILABLE,
    "spawn_failed": ToolError.UNAVAILABLE,
    "subagent_error": ToolError.UNAVAILABLE,
    "agent_start_failed": ToolError.UNAVAILABLE,
    "user_prompt_error": ToolError.UNAVAILABLE,
    "unavailable": ToolError.UNAVAILABLE,
    "supervisor": ToolError.UNAVAILABLE,
    "cleanup_failed": ToolError.UNAVAILABLE,
    "commit_failed": ToolError.UNAVAILABLE,
    "journal_failed": ToolError.UNAVAILABLE,
    "child_error": ToolError.UNAVAILABLE,
    "all_children_failed": ToolError.UNAVAILABLE,
    "no_user": ToolError.UNAVAILABLE,
    "no_answer": ToolError.UNAVAILABLE,
    "checkpoint_unavailable": ToolError.UNAVAILABLE,
    "websearch_error": ToolError.UNAVAILABLE,
    "fetch_failed": ToolError.UNAVAILABLE,
    "fetch_refused": ToolError.PERMISSION_DENIED,
    "fetch_timeout": ToolError.DEADLINE_EXCEEDED,
    # -- our own breakage ----------------------------------------------------
    "tool_error": ToolError.INTERNAL,
    "error": ToolError.INTERNAL,
    "internal": ToolError.INTERNAL,
    "read_error": ToolError.INTERNAL,
    "write_error": ToolError.INTERNAL,
    "edit_error": ToolError.INTERNAL,
    "grep_error": ToolError.INTERNAL,
    "glob_error": ToolError.INTERNAL,
}

#: Family defaults.  Used only when a caller emits a family directly (no leaf
#: detail); a leaf-carrying result keeps the historical severity below, so the
#: fatigue ladder's tuned behaviour does not move.
_FAMILY_SEVERITY: dict[str, int] = {
    ToolError.CANCELLED.value: 1,
    ToolError.UNKNOWN.value: 1,
    ToolError.INVALID_ARGUMENT.value: 2,
    ToolError.DEADLINE_EXCEEDED.value: 5,
    ToolError.NOT_FOUND.value: 2,
    ToolError.ALREADY_EXISTS.value: 2,
    ToolError.PERMISSION_DENIED.value: 3,
    ToolError.RESOURCE_EXHAUSTED.value: 4,
    ToolError.FAILED_PRECONDITION.value: 2,
    ToolError.ABORTED.value: 2,
    ToolError.OUT_OF_RANGE.value: 2,
    ToolError.UNIMPLEMENTED.value: 2,
    ToolError.INTERNAL.value: 5,
    ToolError.UNAVAILABLE.value: 5,
    ToolError.DATA_LOSS.value: 5,
    ToolError.UNAUTHENTICATED.value: 3,
}

#: Historical keyword severity, applied to the *detail* so the ladder keeps the
#: behaviour every measurement was taken against.  Deprecated: new leaves should
#: be added with an explicit family and the keyword path will go away when the
#: old traces no longer matter.
_SEVERITY_KEYWORDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (5, ("timeout", "context overflow", "out of memory", "token limit")),
    (4, ("rate limit", "too many requests", "quota exceeded")),
    (3, ("permission denied", "access forbidden", "unauthorized")),
    (2, ("not found", "invalid", "malformed", "bad request", "error", "failed")),
)

#: Codes that do not count as failures in the ladder.  Exactly the historical
#: pair: a cancelled call is the user's decision, and a plan-validation refusal
#: is an instruction, not breakage.  Everything else -- including a guard
#: refusal -- keeps the burn (measured 2026-10).
_NEUTRAL_DETAILS: frozenset[str] = frozenset({CANCELLED_BY_USER, "invalid_plan"})

_RETRYABLE: frozenset[str] = frozenset(
    {
        ToolError.DEADLINE_EXCEEDED.value,
        ToolError.RESOURCE_EXHAUSTED.value,
        ToolError.UNAVAILABLE.value,
        ToolError.ABORTED.value,
        ToolError.INTERNAL.value,
        # MCP-07: a timed-out remote call is worth retrying after the session
        # is rebuilt; the other MCP leaves add nothing here (their families
        # already carry the answer), and an unknown server/tool must not.
        "mcp_timeout",
    }
)

#: "denied" = something stopped the call on purpose (operator, guard, rule);
#: "failed" = it broke.  The UI colours and strikes through by this value.
_DENIED: frozenset[str] = frozenset(
    {
        ToolError.PERMISSION_DENIED.value,
        ToolError.CANCELLED.value,
        ToolError.FAILED_PRECONDITION.value,
        ToolError.ALREADY_EXISTS.value,
        ToolError.ABORTED.value,
        ToolError.UNAUTHENTICATED.value,
    }
)


def _code_text(error: str | ToolError | None) -> str:
    """The string value of an input code.

    ``ToolError`` is a ``str`` mixin, but ``str(ToolError.INTERNAL)`` is
    ``"ToolError.INTERNAL"`` -- a producer that passes the enum member (which
    the module invites, since it exports it) would otherwise be classified as
    ``UNKNOWN``.  Normalise through ``.value``.
    """
    if isinstance(error, ToolError):
        return error.value
    return str(error or "")


def is_family(value: str | ToolError | None) -> bool:
    """Is ``value`` already one of the canonical gRPC codes?"""
    return _code_text(value) in _FAMILY_VALUES


def normalize(
    error: str | ToolError | None, *, success: bool
) -> tuple[str | None, str | None]:
    """``(family, detail)`` for a result's ``error`` input.

    A leaf becomes ``(its family, the leaf)``; an already-canonical code keeps
    its detail (if any); a missing code on a failed result becomes
    ``INTERNAL`` -- an unclassified failure is our bug, not the model's.
    A successful result keeps ``(None, None)`` and its envelope stays
    byte-identical.
    """
    if error is None:
        if success:
            return None, None
        return ToolError.INTERNAL.value, None
    text = _code_text(error)
    if text in _FAMILY_VALUES:
        return text, None
    return (LEGACY_TO_FAMILY.get(text) or ToolError.UNKNOWN).value, text


def family_of(error: str | ToolError | None) -> str:
    """The family for a code, tolerating legacy leaves and ``None``."""
    if error is None:
        return ToolError.UNKNOWN.value
    text = _code_text(error)
    if text in _FAMILY_VALUES:
        return text
    return (LEGACY_TO_FAMILY.get(text) or ToolError.UNKNOWN).value


def severity_of(error: str | None, detail: str | None = None) -> int:
    """Fatigue severity, 1..5.

    A legacy detail keeps the historical keyword answer (the ladder was tuned
    against it); a bare family falls back to its default.
    """
    if detail:
        lowered = detail.lower()
        for level, keywords in _SEVERITY_KEYWORDS:
            if any(keyword in lowered for keyword in keywords):
                return level
        return 1
    return _FAMILY_SEVERITY.get(family_of(error), 1)


def is_neutral(error: str | None, detail: str | None = None) -> bool:
    """Does this failure leave the ladder's failure streak alone?"""
    del error  # the pair is deliberately detail-only, matching the old set
    return bool(detail) and detail in _NEUTRAL_DETAILS


def is_retryable(error: str | None) -> bool:
    """May the model retry the same call unchanged (after a back-off)?"""
    # A producer may still hand over the legacy leaf; the set is written in
    # families, with the leaves that need to be named individually listed too.
    return _code_text(error) in _RETRYABLE or family_of(error) in _RETRYABLE


def tone_of(error: str | None) -> Literal["denied", "failed"]:
    """How the UI should draw the settled card."""
    return "denied" if family_of(error) in _DENIED else "failed"
