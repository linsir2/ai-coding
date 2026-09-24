"""Hooks — user policy at fixed points (R4c).

A guard is *our* policy; a hook is the operator's.  Hooks therefore need two
properties that a guard does not:

* they run untrusted-shaped code (a script, or an imported module), so every
  failure has a defined outcome rather than a traceback;
* they must not be handed the model's credentials — the environment is built
  from an allow-list, never by deleting names we happen to remember
  (docs/r4-analysis.md 6.3).

Failure semantics follow the capability matrix (P3): an *observer* that breaks is
skipped (fail-open), while a hook that can *block* or *rewrite* fails closed.
"""

from __future__ import annotations

import enum
import importlib.util
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.infra.subprocess_runner import run_process_async

__all__ = [
    "CAPABILITIES",
    "CommandHookRunner",
    "HookRunnerGroup",
    "HookDecision",
    "HookPoint",
    "HookResult",
    "HookSpec",
    "PythonHookRunner",
    "specs_from_config",
    "validate_spec",
]

_log = logging.getLogger(__name__)

# Variables a hook may see.  Everything else (keys, tokens, credentialed
# proxies) simply is not there.
_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "PWD", "USER", "SHELL")


class HookPoint(enum.Enum):
    """Where a hook may attach (docs/r4-analysis.md 6.1)."""

    SESSION_START = "session.start"
    TURN_START = "turn.start"
    STEP_BEFORE = "step.before"
    TOOL_BEFORE = "tool.before"
    TOOL_AFTER = "tool.after"
    COMPACT_BEFORE = "compact.before"
    COMPACT_AFTER = "compact.after"
    MEMORY_HARVEST = "memory.harvest"
    SUBAGENT_START = "subagent.start"
    SUBAGENT_END = "subagent.end"
    TURN_END = "turn.end"
    STOP_BLOCKED = "stop.blocked"


# Which capabilities each point actually supports.  Declaring a capability a point
# cannot honour is a configuration error, not a silent no-op.
CAPABILITIES: dict[HookPoint, frozenset[str]] = {
    HookPoint.SESSION_START: frozenset({"observe"}),
    HookPoint.TURN_START: frozenset({"observe", "inject"}),
    HookPoint.STEP_BEFORE: frozenset({"observe", "inject"}),
    HookPoint.TOOL_BEFORE: frozenset({"observe", "block", "rewrite"}),
    HookPoint.TOOL_AFTER: frozenset({"observe", "inject"}),
    HookPoint.COMPACT_BEFORE: frozenset({"observe"}),
    HookPoint.COMPACT_AFTER: frozenset({"observe"}),
    HookPoint.MEMORY_HARVEST: frozenset({"observe"}),
    HookPoint.SUBAGENT_START: frozenset({"observe", "inject"}),
    HookPoint.SUBAGENT_END: frozenset({"observe"}),
    HookPoint.TURN_END: frozenset({"observe"}),
    HookPoint.STOP_BLOCKED: frozenset({"observe", "block"}),
}


class HookDecision(enum.Enum):
    """What a hook asked for."""

    ALLOW = "allow"
    BLOCK = "block"
    REWRITE = "rewrite"
    INJECT = "inject"


@dataclass(frozen=True)
class HookSpec:
    """One configured hook."""

    point: HookPoint
    kind: str = "command"  # "command" | "python"
    command: tuple[str, ...] = ()
    module: str = ""
    entrypoint: str = "run"
    capability: str = "observe"
    persist: bool = False
    raw: bool = False
    match_tool: str = ""

    @property
    def fails_closed(self) -> bool:
        """A broken hook that could have stopped something must stop it."""
        return self.capability in {"block", "rewrite"}

    def matches(self, ctx: Mapping[str, Any]) -> bool:
        if not self.match_tool:
            return True
        tool = ctx.get("tool")
        name = tool.get("name") if isinstance(tool, Mapping) else None
        return name == self.match_tool


@dataclass(frozen=True)
class HookResult:
    """One hook's verdict."""

    hook: str
    decision: HookDecision = HookDecision.ALLOW
    reason: str = ""
    params: dict[str, Any] | None = None
    context: str = ""
    failed: bool = False
    persist: bool = False


def validate_spec(spec: HookSpec) -> None:
    """Reject a configuration the point cannot honour."""
    allowed = CAPABILITIES.get(spec.point, frozenset())
    if spec.capability not in allowed:
        raise ValueError(
            f"hook at {spec.point.value} cannot {spec.capability} "
            f"(allowed: {', '.join(sorted(allowed)) or 'none'})"
        )


def specs_from_config(config: Any) -> list[HookSpec]:
    """Turn ``HooksConfig`` into validated specs."""
    specs: list[HookSpec] = []
    for event in getattr(config, "events", []) or []:
        point_value = str(getattr(event, "point", ""))
        try:
            point = HookPoint(point_value)
        except ValueError as exc:
            raise ValueError(f"unknown hook point '{point_value}'") from exc
        match = getattr(event, "match", {}) or {}
        spec = HookSpec(
            point=point,
            kind=str(getattr(event, "kind", "command")),
            command=tuple(getattr(event, "command", []) or ()),
            module=str(getattr(event, "module", "")),
            entrypoint=str(getattr(event, "entrypoint", "run") or "run"),
            capability=str(getattr(event, "capability", "observe")),
            persist=bool(getattr(event, "persist", False)),
            raw=bool(getattr(event, "raw", False)),
            match_tool=str(match.get("tool", "")) if isinstance(match, Mapping) else "",
        )
        validate_spec(spec)
        specs.append(spec)
    return specs


def hook_environment() -> dict[str, str]:
    """An allow-listed environment: credentials simply are not in it."""
    env = {name: value for name, value in os.environ.items() if name in _ENV_ALLOWLIST}
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


def parse_payload(payload: str) -> dict[str, Any] | None:
    """Read a hook's verdict; anything unparseable means "no verdict"."""
    text = (payload or "").strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1]):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def to_result(spec: HookSpec, hook_name: str, payload: Mapping[str, Any]) -> HookResult:
    """Convert a hook's JSON verdict, capped at the capability it declared."""
    try:
        decision = HookDecision(str(payload.get("decision", "allow")).lower())
    except ValueError:
        decision = HookDecision.ALLOW
    if decision.value not in CAPABILITIES.get(spec.point, frozenset()):
        decision = HookDecision.ALLOW
    params = payload.get("params")
    return HookResult(
        hook=hook_name,
        decision=decision,
        reason=str(payload.get("reason", "")),
        params=dict(params) if isinstance(params, Mapping) else None,
        context=str(payload.get("context", "")),
        persist=spec.persist,
    )


def failed_result(spec: HookSpec, hook_name: str, reason: str) -> HookResult:
    """A hook that could not run: state, then follow the capability matrix."""
    decision = HookDecision.BLOCK if spec.fails_closed else HookDecision.ALLOW
    return HookResult(
        hook=hook_name,
        decision=decision,
        reason=f"{reason} ({spec.capability} hook)",
        failed=True,
    )


class _SpecsMixin:
    """Shared filtering: only this point's hooks, and only matching ones."""

    _specs: list[HookSpec]
    _redactor: Any
    _kind: str = ""

    def specs_for(self, point: HookPoint) -> list[HookSpec]:
        return [
            spec
            for spec in self._specs
            if spec.point is point and spec.kind == self._kind
        ]

    def _payload(self, spec: HookSpec, point: HookPoint, ctx: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(ctx)
        payload.setdefault("point", point.value)
        if not spec.raw:
            payload = self._redactor.redact_obj(payload)
        return payload


class CommandHookRunner(_SpecsMixin):
    """Runs configured scripts: one JSON line on stdin, JSON on stdout."""

    _kind = "command"

    def __init__(
        self,
        specs: Sequence[HookSpec],
        *,
        workspace: str = ".",
        timeout_seconds: float = 10.0,
        redactor: Any | None = None,
    ) -> None:
        self._specs = list(specs)
        self._workspace = workspace
        self._timeout = float(timeout_seconds)
        if redactor is None:
            from ai_coding.core.trace import Redactor

            redactor = Redactor()
        self._redactor = redactor

    async def run(
        self, point: HookPoint, ctx: Mapping[str, Any]
    ) -> list[HookResult]:
        results: list[HookResult] = []
        for spec in self.specs_for(point):
            if not spec.matches(ctx):
                continue
            results.append(await self._run_one(spec, point, ctx))
        return results

    async def _run_one(
        self, spec: HookSpec, point: HookPoint, ctx: Mapping[str, Any]
    ) -> HookResult:
        name = " ".join(spec.command) or "<empty command>"
        payload = self._payload(spec, point, ctx)
        try:
            outcome = await run_process_async(
                list(spec.command),
                cwd=self._workspace,
                env=hook_environment(),
                stdin_data=json.dumps(payload, ensure_ascii=False, default=str),
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 — a hook must not break the turn
            return failed_result(spec, name, f"hook could not run: {exc}")

        if outcome.error or outcome.timed_out:
            return failed_result(spec, name, outcome.error or "hook timed out")
        if outcome.returncode not in (0, None):
            return failed_result(spec, name, f"hook exited with status {outcome.returncode}")
        parsed = parse_payload(outcome.stdout)
        if parsed is None:
            return HookResult(hook=name)
        return to_result(spec, name, parsed)


class HookRunnerGroup:
    """Run several runners (command + python) as if they were one.

    Config order is preserved within a runner; the group keeps the runners'
    order, so a hook declared first still runs first
    (docs/r4-analysis.md 6.3, "顺序").
    """

    def __init__(self, runners: Sequence[Any]) -> None:
        self._runners = [runner for runner in runners if runner is not None]

    def __len__(self) -> int:
        return len(self._runners)

    async def run(
        self, point: HookPoint, ctx: Mapping[str, Any]
    ) -> list[HookResult]:
        results: list[HookResult] = []
        for runner in self._runners:
            results.extend(await runner.run(point, ctx))
        return results


class PythonHookRunner(_SpecsMixin):
    """Runs hooks as in-process callables discovered from a plugin directory."""

    _kind = "python"

    def __init__(
        self,
        specs: Sequence[HookSpec],
        *,
        plugin_dir: str = "",
        workspace: str = ".",
        redactor: Any | None = None,
    ) -> None:
        self._specs = list(specs)
        self._plugin_dir = plugin_dir
        self._workspace = workspace
        if redactor is None:
            from ai_coding.core.trace import Redactor

            redactor = Redactor()
        self._redactor = redactor
        self._modules: dict[str, Any] = {}

    async def run(
        self, point: HookPoint, ctx: Mapping[str, Any]
    ) -> list[HookResult]:
        results: list[HookResult] = []
        for spec in self.specs_for(point):
            if not spec.matches(ctx):
                continue
            results.append(self._run_one(spec, point, ctx))
        return results

    def _load(self, spec: HookSpec) -> Any:
        if spec.module in self._modules:
            return self._modules[spec.module]
        path = Path(self._plugin_dir) / f"{spec.module}.py"
        module_spec = importlib.util.spec_from_file_location(
            f"ai_coding_hooks.{spec.module}", path
        )
        if module_spec is None or module_spec.loader is None:
            raise ImportError(f"hook module '{spec.module}' not found at {path}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        self._modules[spec.module] = module
        return module

    def _run_one(
        self, spec: HookSpec, point: HookPoint, ctx: Mapping[str, Any]
    ) -> HookResult:
        name = f"{spec.module}.{spec.entrypoint}"
        payload = self._payload(spec, point, ctx)
        try:
            module = self._load(spec)
            entry = getattr(module, spec.entrypoint)
            verdict = entry(payload)
        except Exception as exc:  # noqa: BLE001 — a hook must not break the turn
            return failed_result(spec, name, f"hook failed: {exc}")
        if not isinstance(verdict, Mapping):
            return HookResult(hook=name)
        return to_result(spec, name, verdict)
