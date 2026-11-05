"""One completion gate for every TaskState entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.domain.task import TodoItem
from ai_coding.infra.file_utils import file_version
from ai_coding.security.approval import ApprovalResult

__all__ = ["CompletionGate", "CompletionVerdict"]


@dataclass(frozen=True)
class CompletionVerdict:
    """The result of checking one item's proof."""

    ok: bool
    reason: str = ""
    evidence: str = ""
    #: Stable diagnostic code for the tool layer/UI.  Reason is prose.
    code: str = ""


class CompletionGate:
    """A completed item needs a receipt or a verify pass, never prose alone."""

    @staticmethod
    def _key(raw: str, workspace: str) -> tuple[str, Path] | None:
        base = Path(workspace or ".").expanduser().resolve()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        try:
            path = path.resolve()
        except OSError:
            return None
        try:
            relative = path.relative_to(base)
        except ValueError:
            # Authorized extra roots use absolute effect keys; keep them
            # unique instead of folding to a basename.
            return str(path), path
        return relative.as_posix(), path

    @classmethod
    def receipt_floor(cls, item: TodoItem, ctx: Any) -> int | None:
        """Earliest receipt for the item's declared files, if one exists.

        A step created *after* the work was done still has a real receipt for
        it; lowering the boundary to that receipt is what keeps the completion
        gate from demanding a redundant second write.  ``update_plan`` and
        ``todo_write`` share this rule so the two readers cannot disagree.
        """
        ledger = ctx.effects() if getattr(ctx, "effects", None) is not None else None
        if ledger is None or not item.files:
            return None
        workspace = str(getattr(ctx, "workspace", "") or ".")
        seqs: list[int] = []
        for raw in item.files:
            resolved = cls._key(str(raw), workspace)
            if resolved is None:
                continue
            key, _path = resolved
            receipt = ledger.receipt_for(key)
            if receipt is not None and receipt.seq > 0:
                seqs.append(int(receipt.seq))
        return min(seqs) if seqs else None

    @classmethod
    async def check(cls, item: TodoItem, ctx: Any) -> CompletionVerdict:
        ledger = ctx.effects() if getattr(ctx, "effects", None) is not None else None
        workspace = str(getattr(ctx, "workspace", "") or ".")

        file_evidence: list[str] = []
        file_error = ""
        if item.files:
            missing: list[str] = []
            stale: list[str] = []
            for raw in item.files:
                resolved = cls._key(raw, workspace)
                if resolved is None:
                    missing.append(f"{raw}（工作区外）")
                    continue
                key, path = resolved
                if path.is_dir():
                    return CompletionVerdict(
                        False,
                        f"directory_unsupported: '{raw}' 是目录；目录请用 verify",
                        code="directory_unsupported",
                    )
                receipt = (
                    ledger.receipt_for(key, min_seq=item.started_seq)
                    if ledger is not None
                    else None
                )
                if receipt is None:
                    missing.append(raw)
                    continue
                if receipt.confidence not in ("exact", "observed"):
                    missing.append(raw)
                    continue
                if receipt.kind == "delete":
                    if path.exists():
                        stale.append(raw)
                    else:
                        file_evidence.append(f"{raw}@deleted")
                    continue
                if not receipt.version:
                    missing.append(raw)
                    continue
                current = file_version(path)
                if current != receipt.version:
                    stale.append(raw)
                    continue
                file_evidence.append(f"{raw}@{receipt.version}")
            if missing or stale:
                parts: list[str] = []
                if missing:
                    parts.append("缺少回执: " + ", ".join(missing))
                if stale:
                    parts.append("文件版本已变化: " + ", ".join(stale))
                file_error = "；".join(parts)

        verify_error = ""
        verify_evidence = ""
        verify_unavailable = False
        if item.verify:
            verifier = getattr(ctx, "verify_item", None)
            if verifier is None:
                verify_unavailable = True
            else:
                passed, tail = await verifier(item.id, item.content, item.verify)
                if not passed:
                    verify_error = f"verify failed: {tail}"
                else:
                    verify_evidence = f"verify: {item.verify}"

        if not item.files and not item.verify:
            return CompletionVerdict(
                False,
                "completion_unverified: 该条目没有 files 或 verify，"
                "不能只凭 evidence 标 completed",
                code="completion_unverified",
            )
        # An explicit verify that ran and failed must never be hidden by a
        # file receipt: receipts say "the file is current", not "the proof ran".
        if verify_error:
            return CompletionVerdict(False, verify_error, code="verify_failed")
        if file_evidence and not file_error:
            return CompletionVerdict(
                True, evidence="receipt: " + ", ".join(file_evidence)
            )
        if verify_evidence:
            return CompletionVerdict(True, evidence=verify_evidence)
        if file_error:
            code = (
                "legacy_session_unverified"
                if getattr(ctx, "effects_legacy", False)
                else "completion_unverified"
            )
            return CompletionVerdict(False, file_error, code=code)
        if verify_unavailable:
            return CompletionVerdict(
                False, "verify 未接线，无法证明完成", code="verify_unavailable"
            )
        return CompletionVerdict(False, "completion_unverified", code="completion_unverified")

    @classmethod
    async def override(
        cls, item: TodoItem, ctx: Any, reason: str
    ) -> CompletionVerdict:
        """Authorise one explicit override; the first one asks the operator."""
        reason = str(reason or "").strip()
        if not reason:
            return CompletionVerdict(
                False, "override_reason is required", code="missing_override_reason"
            )
        provider = getattr(ctx, "completion_override_provider", None)
        granted = bool(provider()) if callable(provider) else bool(
            getattr(ctx, "completion_override_approved", False)
        )
        if not granted:
            approval = getattr(ctx, "approval", None)
            if approval is None or not getattr(approval, "is_human", False):
                return CompletionVerdict(
                    False, "override needs operator approval", code="override_unavailable"
                )
            note = getattr(ctx, "note_approval", None)
            if note is not None:
                note(
                    {
                        "tool_name": "todo_write",
                        "params": {"id": item.id, "reason": reason},
                        "reason": "completion override",
                    }
                )
            answer = await approval.approve(
                "todo_write",
                {"id": item.id, "status": "completed", "reason": reason},
                "completion override: mark an item complete without a receipt",
            )
            if note is not None:
                note(None)
            if answer not in (ApprovalResult.YES, ApprovalResult.ALWAYS):
                return CompletionVerdict(
                    False,
                    "operator denied the completion override",
                    code="override_denied",
                )
            grant = getattr(ctx, "note_completion_override", None)
            if grant is not None:
                grant()
            ctx.completion_override_approved = True
        return CompletionVerdict(
            True, evidence=f"override: {reason}", code="override"
        )
