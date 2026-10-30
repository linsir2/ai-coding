"""Wave handoff contract (C3).

A wave is one continuous session; the next wave starts fresh.  The handoff is
what survives that boundary:

* mechanical facts the executor owns -- what finished, what was skipped, which
  files changed, which proofs ran and passed;
* what the model chose to hand over -- interfaces it introduced, decisions the
  next wave must keep, pitfalls it hit, one hint for the next wave.

The mechanical half is authoritative: ``build`` reads only the four model
fields out of the report, so a model that claims a file changed cannot put a
wrong path into the next seed.  ``render`` is the only consumer (the seed); the
per-step ``task.wave_note`` stays a short mechanical digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: One model-written line (interface / decision / pitfall).
WAVE_HANDOFF_ITEM_MAX_CHARS = 200
#: Lines per section.
WAVE_HANDOFF_MAX_ITEMS = 12
#: The one hint for the next wave.
WAVE_HANDOFF_HINT_MAX_CHARS = 400
#: Total rendered size; the rest is trimmed (see ``render``).
WAVE_HANDOFF_MAX_CHARS = 2_400


@dataclass(frozen=True)
class WaveHandoff:
    """What one wave leaves to the next (frozen: built once, then rendered)."""

    done: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    #: ``{"itemId": str, "command": str, "passed": bool, "tail": str}`` rows.
    verify: tuple[dict[str, Any], ...] = ()
    interfaces: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    pitfalls: tuple[str, ...] = ()
    next_hint: str = ""

    @classmethod
    def build(
        cls,
        *,
        done: tuple[str, ...] = (),
        skipped: tuple[str, ...] = (),
        files: tuple[str, ...] = (),
        verify: tuple[dict[str, Any], ...] = (),
        report: dict[str, Any] | None = None,
    ) -> WaveHandoff:
        """Merge the executor's facts with the model's report.

        ``report`` is read defensively: only the four model fields are used,
        so the mechanical facts cannot be overridden by a crafted report.
        """
        data = report if isinstance(report, dict) else {}
        return cls(
            done=tuple(str(value) for value in done),
            skipped=tuple(str(value) for value in skipped),
            files=tuple(str(value) for value in files),
            verify=tuple(dict(row) for row in verify),
            interfaces=_model_lines(data.get("interfaces")),
            decisions=_model_lines(data.get("decisions")),
            pitfalls=_model_lines(data.get("pitfalls")),
            next_hint=_clean(str(data.get("next_hint", "") or ""),
                             WAVE_HANDOFF_HINT_MAX_CHARS),
        )

    @property
    def has_model_content(self) -> bool:
        return bool(self.interfaces or self.decisions or self.pitfalls or self.next_hint)

    def render(self, max_chars: int = WAVE_HANDOFF_MAX_CHARS) -> str:
        """Render for the next wave's seed, trimmed to ``max_chars``.

        Trim order is pitfalls -> decisions -> interfaces, oldest line first:
        the newest experience is the most valuable, and the mechanical facts
        (done/skipped/files/proofs) are never dropped.
        """
        pitfalls = list(self.pitfalls)
        decisions = list(self.decisions)
        interfaces = list(self.interfaces)
        dropped = False
        while True:
            text = self._render(
                interfaces=interfaces, decisions=decisions, pitfalls=pitfalls
            )
            if len(text) <= max_chars:
                break
            if pitfalls:
                pitfalls.pop(0)
                dropped = True
                continue
            if decisions:
                decisions.pop(0)
                dropped = True
                continue
            if interfaces:
                interfaces.pop(0)
                dropped = True
                continue
            break
        if dropped:
            text = f"{text}\n…"
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
        return text

    def _render(
        self,
        *,
        interfaces: list[str],
        decisions: list[str],
        pitfalls: list[str],
    ) -> str:
        lines: list[str] = []
        if self.done:
            lines.append("已完成：" + ",".join(self.done))
        if self.skipped:
            lines.append("跳过：" + ",".join(self.skipped))
        if self.files:
            lines.append("改动：" + ",".join(self.files))
        for row in self.verify:
            item_id = str(row.get("itemId", "") or "")
            command = str(row.get("command", "") or "")
            passed = bool(row.get("passed", False))
            tail = str(row.get("tail", "") or "")
            line = f"验收：{item_id} {'通过' if passed else '失败'}（{command}）"
            if tail:
                line += f" {tail}"
            lines.append(line)
        sections = (
            ("接口", interfaces),
            ("决定", decisions),
            ("坑", pitfalls),
        )
        for name, entries in sections:
            if not entries:
                continue
            lines.append(f"{name}：")
            lines.extend(f"- {entry}" for entry in entries)
        if self.next_hint:
            lines.append("下一波：" + self.next_hint)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "done": list(self.done),
            "skipped": list(self.skipped),
            "files": list(self.files),
            "verify": [dict(row) for row in self.verify],
            "interfaces": list(self.interfaces),
            "decisions": list(self.decisions),
            "pitfalls": list(self.pitfalls),
            "nextHint": self.next_hint,
        }

    @classmethod
    def from_dict(cls, data: Any) -> WaveHandoff | None:
        """Rebuild a stored handoff; ``None`` for absent/garbage payloads."""
        if not isinstance(data, dict):
            return None
        done = _stored_lines(data.get("done"))
        if not any(key in data for key in ("done", "interfaces", "decisions")):
            return None
        raw_verify = data.get("verify")
        verify = tuple(
            dict(row) for row in (raw_verify if isinstance(raw_verify, list) else [])
            if isinstance(row, dict)
        )
        return cls(
            done=done,
            skipped=_stored_lines(data.get("skipped")),
            files=_stored_lines(data.get("files")),
            verify=verify,
            interfaces=_stored_lines(data.get("interfaces")),
            decisions=_stored_lines(data.get("decisions")),
            pitfalls=_stored_lines(data.get("pitfalls")),
            next_hint=str(data.get("nextHint", data.get("next_hint", "")) or ""),
        )


def _clean(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _model_lines(value: Any) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple)) else []
    lines = [
        _clean(str(entry).strip(), WAVE_HANDOFF_ITEM_MAX_CHARS)
        for entry in raw
        if str(entry).strip()
    ]
    return tuple(lines[:WAVE_HANDOFF_MAX_ITEMS])


def _stored_lines(value: Any) -> tuple[str, ...]:
    raw = value if isinstance(value, (list, tuple)) else []
    return tuple(str(entry) for entry in raw if str(entry))


__all__ = [
    "WAVE_HANDOFF_ITEM_MAX_CHARS",
    "WAVE_HANDOFF_MAX_ITEMS",
    "WAVE_HANDOFF_HINT_MAX_CHARS",
    "WAVE_HANDOFF_MAX_CHARS",
    "WaveHandoff",
]
