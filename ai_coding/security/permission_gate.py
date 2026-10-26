"""Permission gate — decides ALLOW/WARN/DENY for each tool invocation.

Rules mirror the original Java ``PermissionGate``:
  - read / glob          → ALLOW
  - write / edit         → WARN
  - bash                 → WARN (hard-deny list → DENY)
  - todo_write / skill / subAgent / memory_search / ask_user → ALLOW
  - unknown tools        → WARN (fail-closed, conservative)
"""

from __future__ import annotations

import enum
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.security.bash_redlines import bash_red_line_with_fallback
from ai_coding.security.credentials import credential_match, credential_rules
from ai_coding.security.grants import DEFAULT_GRANT_TTL, GrantStore
from ai_coding.security.permissions import (
    NO_FILE_RULES,
    POSTURES,
    Effect,
    FileRules,
    Rule,
    Ruleset,
    evaluate,
    load_file_rules,
    partially_allowed,
    subjects_of,
    wholly_disabled,
)
from ai_coding.security.permissions import _lexical as _lexical_subject


class PermissionDecision(enum.Enum):
    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


@dataclass(frozen=True)
class _DecisionWithReason:
    """A decision plus a human-readable reason (shown to the user on WARN/DENY)."""

    decision: PermissionDecision
    reason: str


# Re-export the enum so ``PermissionGate.check()`` returns a ``PermissionDecision``
# but we internally carry reasons.  The public API uses ``PermissionDecision``
# for simple equality checks; ``reason`` is available as an attribute when needed.
#
# We attach ``reason`` to each enum member via a wrapper approach: return a custom
# subclass instance?  Simpler: return the enum but also set an attribute.
# Enums can't carry extra instance state directly, so we use a dataclass that
# compares equal to the enum by value.


class Decision:
    """A permission decision with an attached reason.

    Equality with ``PermissionDecision`` enum members works by value, so callers
    can write ``decision == PermissionDecision.ALLOW`` and also access
    ``decision.reason``.
    """

    def __init__(
        self,
        decision: PermissionDecision,
        reason: str = "",
        *,
        specific: bool = False,
    ) -> None:
        self.decision = decision
        self.reason = reason
        #: M3 T5: this ALLOW came from a **scoped** rule (resource != "*"),
        #: so it is a boundary the dispatch overlay must not veto.  False is
        #: the safe default: an overlay keeps refusing what it refused before.
        self.specific = specific

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PermissionDecision):
            return self.decision == other
        if isinstance(other, Decision):
            return (
                self.decision == other.decision
                and self.reason == other.reason
                and self.specific == other.specific
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.decision)

    def __repr__(self) -> str:
        return (
            f"Decision({self.decision.value!r}, reason={self.reason!r},"
            f" specific={self.specific!r})"
        )


def bash_denied(command: str) -> str:
    """Why this bash command may not run at all, or ``""`` when it may be asked.

    A delegate, and deliberately so.  The judgement -- the shell grammar, and
    the text reading that stands behind it for a line the grammar cannot
    finish -- lives in :mod:`ai_coding.security.bash_redlines`, in
    :func:`~ai_coding.security.bash_redlines.bash_red_line_with_fallback`.
    It used to be duplicated here: the same ``rm -rf /`` and ``chmod 777``
    decided twice, and the two copies were kept in step only by an oral
    agreement.  One judge now, so the refusal and the approval panel cannot
    disagree about what is forbidden.

    This name stays because two callers import it (the dispatcher and the
    acceptance runner), and it reads no clock and no file; the shell grammar
    is loaded once per process and kept.
    """
    return bash_red_line_with_fallback(command)


#: Tools an "always" may never remember, whatever resource can be extracted.
#: A policy exclusion rather than a technical one: an always on bash would be an
#: always on every command it may ever run.  It lives here, beside the gate,
#: because it is a permission rule -- it used to live in the approval modal,
#: where the button and the recorder each had to remember to consult it.
_NEVER_REMEMBERED = frozenset({"bash"})

#: Tools whose params name a single file, so a grant can say which one.
#: ``read`` joined them (opencode's external-directory ask,
#: ``docs/orchestration-research/06-opencode长任务机制.md:20``): a read is the
#: request the sandbox used to refuse outright, and refusing it is what sent the
#: model to ``bash``.
_PATH_TOOLS = frozenset({"write", "edit", "read"})

#: Read-only tools that name a *directory* rather than a file: a glob pattern and
#: a grep root.  Their grant key is the deepest directory the call can reach, so
#: "always" on one pattern does not re-ask for its siblings -- and it cannot open
#: anything above that directory.
_DIR_TOOLS = frozenset({"glob", "grep"})


def _directory_of(pattern: str) -> str:
    """The deepest static directory a glob/grep argument names.

    Everything up to the last wildcard segment is a plain path; if the whole
    argument is a wildcard the call could reach anywhere under the workspace, so
    the answer is the empty string and the caller refuses to key a grant.
    """
    parts = [part for part in re.split(r"[/\\]", str(pattern)) if part]
    concrete: list[str] = []
    for part in parts:
        if any(char in part for char in "*?[]{}"):
            break
        concrete.append(part)
    return "/".join(concrete)


def _split_grant(key: str) -> tuple[str, str | None]:
    """A stored grant as ``(key, posture)``; ``None`` means every posture.

    New grants are written as ``key@posture``.  Old ones have no suffix and
    keep meaning "everywhere" -- a file is not rewritten just because the
    reader learned a new word.  A path literally ending in ``@build`` is
    indistinguishable from a scoped grant; we read it as scoped, which is the
    narrower (fail-closed) reading, matching how an unknown suffix is treated.
    """
    base, separator, suffix = key.rpartition("@")
    if not separator or suffix not in POSTURES:
        return key, None
    return base, suffix


#: A credential file is a secret container: reading or searching it asks the
#: operator, and the example file is the one documentation exception.  The
#: vocabulary itself lives in ``security.credentials`` so Bash, read and grep
#: cannot drift.
CREDENTIAL_READ_RULES: Ruleset = credential_rules(("read", "grep"))


def _grant_base(key: str) -> str:
    """The path/tool part of a stored grant, without its posture suffix."""
    return _split_grant(key)[0]


def grant_key(
    tool_name: str,
    params: Mapping[str, Any],
    *,
    workspace: str | None = None,
) -> str | None:
    """What an "always" would actually remember, or ``None`` when it cannot be said.

    The key has to be *what the operator just read*.  Keyed on the tool name
    alone it is strictly broader than the modal it came from: the operator reads
    "edit src/foo.py" -- diff included -- and would otherwise be granting edits
    to every file in the workspace.

    ``None`` means the grant must not be offered at all: either the tool is
    excluded by policy, or its params name no single resource (a remote tool's
    arguments are not summarisable).  Remembering "something" in those cases
    would repeat the same defect one level down.

    Paths are normalised lexically (``./a`` -> ``a``) because the key is for
    *matching*, not for enforcing a boundary -- ``Sandbox`` owns that, and it
    resolves properly.  Two spellings this cannot unify cost an extra prompt,
    never a wrong decision.
    """
    name = tool_name.lower()
    if name in _NEVER_REMEMBERED:
        return None
    # M2: ``server__tool`` — the remote tool's own name is what was approved.
    # It used to return None here ("a remote tool's arguments are not
    # summarisable"), which made "Always" silently "this once" for every MCP
    # tool: the operator's answer had nothing to be remembered *as*.
    if "__" in name:
        return name
    path = str(params.get("path") or "").strip()
    if name in ("read", "grep") and path and credential_match(
        path, workspace=workspace
    ):
        # A credential is approved one call at a time.  No standing grant may
        # turn a later read into an unasked secret read, and a symlinked alias
        # must not hide the same target.
        return None
    if name in _PATH_TOOLS and path:
        if name == "read":
            # A read is granted by *directory* (opencode's external-directory
            # ask): "always" on one file would re-ask for every sibling, which is
            # the friction being removed.  ``.`` means "the workspace itself",
            # where nothing needs granting.
            return f"{name}:{os.path.dirname(os.path.normpath(path)) or '.'}"
        return f"{name}:{os.path.normpath(path)}"
    if name in _DIR_TOOLS:
        # glob's argument is a pattern, grep's is a directory; both name a tree.
        argument = path or _directory_of(str(params.get("pattern", "") or ""))
        if argument:
            return f"{name}:{os.path.normpath(argument)}"
    return None


class PermissionGate:
    """Determines whether a tool invocation is allowed, warned, or denied."""

    def __init__(
        self,
        *,
        trusted_servers: Iterable[str] = (),
        grants: Iterable[str] = (),
        store: Any | None = None,
        denied_tools: Iterable[str] = (),
        allowed_tools: Iterable[str] = (),
        skill_permissions: Mapping[str, str] | None = None,
        skill_resolver: Callable[[str], str | None] | None = None,
        workspace: str | None = None,
        rules: FileRules = NO_FILE_RULES,
        #: M-D3: per-grant expiry, epoch seconds; a grant absent here is
        #: permanent.  Seeded from ``GrantStore.load_with_expiry`` so a grant
        #: loaded from disk carries the lifetime it was written with.
        grant_expiry: Mapping[str, float] | None = None,
        #: M-D3: the TTL a newly granted "always" gets; ``None`` is permanent.
        grant_ttl: float | None = DEFAULT_GRANT_TTL,
        #: M-D3: the clock the expiry check reads.  Injected so a test (and the
        #: store) can move time; the default is the wall clock.
        clock: Callable[[], float] = time.time,
    ) -> None:
        #: Calls the operator answered "Always" for, as ``grant_key`` strings.
        #: Seeded from the store at startup and extended by ``grant``.
        self._grants = {str(key) for key in grants}
        #: M-D3: when each standing grant stops answering (epoch seconds).  A
        #: grant with no entry here is permanent -- the shape every v1 file
        #: already has.
        self._grant_expiry = {
            str(key): float(deadline)
            for key, deadline in (grant_expiry or {}).items()
        }
        self._grant_ttl = grant_ttl
        self._clock = clock
        #: One-off "yes" answers (B).  Session-scoped and never persisted: they
        #: exist so the sandbox can honour the answer for the call it was given
        #: to, and they are not standing permissions.
        self._once_grants: set[str] = set()
        #: Where a new grant is written down, or ``None`` for this process only.
        #: Injected rather than built here so the gate stays constructible
        #: without a filesystem -- every test in the suite relies on that.
        self._store = store
        # B: where this session runs, so the gate can tell an in-workspace path
        # (no question) from one outside it (ask).  ``None`` -- a bare REPL, a
        # sub-agent -- keeps the old behaviour of not judging paths at all.
        self._workspace = str(Path(workspace).expanduser().resolve()) if workspace else ""
        # R7d: MCP servers the user declared as trusted.  A remote tool is named
        # ``server__tool``, so the server is the only thing that can be declared
        # here — the tool names come from the server, not from our config.
        self._trusted_servers = {str(name) for name in trusted_servers if name}
        # M2: per-tool rules, declared per server (``mcp.servers[].permissions``).
        # Full ``server__tool`` names, so a rule reads the same way the catalog
        # and the gate do.
        self._denied_tools = {str(name) for name in denied_tools if name}
        self._allowed_tools = {str(name) for name in allowed_tools if name}
        # E-5: the model's skill tool is judged per skill id (alias-normalized
        # once, here, so check() is a dict lookup).  ``skill_resolver`` maps a
        # spelling to the canonical id; without one, spellings must match the
        # config literally.
        self._skill_resolver = skill_resolver
        self._skill_permissions = {
            (skill_resolver(key) or key) if skill_resolver is not None else key: str(value)
            for key, value in (skill_permissions or {}).items()
        }
        # M1a: the rules read from files -- the user's, the project's, and
        # the global policy.  Injected rather than read here, so the gate
        # stays constructible without a filesystem.
        self._rules = rules
        self._allow_tools = {
            "read",
            "glob",
            "grep",  # r17/T2: read-only content search
            # r17/T3: read-only network tools.  webfetch keeps its own SSRF
            # guard (public http(s) only); neither writes anything locally.
            "webfetch",
            "websearch",
            "todo_write",
            "skill",
            "subagent",
            "memory_search",  # read-only recall (R3b)
            # C-1: reading this conversation's own transcript. It touches nothing
            # outside the process, and it is most needed in plan mode, where a
            # WARN tier would put an approval panel in front of a lookup.
            "history_search",
            # D-1: reading and stopping the workspace's background jobs. The blast
            # radius is the processes this workspace's own model started -- a
            # WARN tier would put an approval panel in front of stopping your own
            # test run.
            "job_list",
            "job_kill",
            # D-1b: reading a job, waiting included. A wait spends the turn; it
            # changes nothing outside the process, which is what the WARN tier is
            # for.
            "job_output",
            # C-3b: reading the background sub-agent table. The table's writer
            # is this process; WARN would put an approval panel in front of the
            # model checking its own delegated work.
            "subagent_list",
            # R7e-1: durable, but not a side effect on the repo, and the harness
            # harvests notes without asking anyway (docs/r7e-analysis.md Q1).
            "memory_write",
            # R6c: asking a question changes nothing by itself — the answer only
            # becomes an action through further tool calls, which are gated on their
            # own.  Leaving it WARN meant a REPL asked "Allow ask_user?" *before*
            # the question: two prompts for one question, and any answer that was
            # not "y" turned the question into "the user declined" (measured).
            "ask_user",
            # R8a: writing one line into the turn's own book touches neither the
            # repo nor the durable memory (docs/r8-analysis.md 3.1, step 5).
            "checkpoint_write",
            # 19 §3.2: presenting the plan only occupies the question channel,
            # like ask_user above -- the plan becomes action through ``/plan
            # run``, whose tools are gated on their own.  WARN made the review
            # card arrive behind an "unknown tool 'present_plan' requires
            # confirmation" prompt: two approvals for one plan (tmux, measured).
            "present_plan",
            # N1: the research exit asks nobody and changes nothing -- the
            # conclusion is a message, and the plan it replaces is not an
            # action.  A WARN tier would put an approval panel in front of
            # the one moment a research answer is allowed to land.
            "finish_research",
            # D-4: starting/updating this session's own goal — an orchestration
            # switch with the blast radius of the session itself (like job_*
            # above): the armed task runs on this workspace's loop, the driver
            # re-checks the disarm at every round boundary, and Esc always
            # stops it.  WARN would put an approval panel in front of the
            # model's own "this needs a goal" judgement, which is the one
            # moment the user does not need to be asked (Q2 拍板: 非自主轮即可
            # — the tool-level guards are the authority rule).
            "start_goal",
            "update_goal",
            # M2: the MCP dispatcher is a vessel, not an action — what it carries
            # is authorized as ``server__tool`` (BaseTool.permission_name), which
            # is what the gate actually judges.  The only calls that reach it
            # under its own name are ones whose pair is not callable (not
            # connected, unknown, whitelisted out), and those must come back as a
            # *result* naming the real tools, not as an approval prompt for a
            # call that cannot run (seen in the M3 end-to-end).
            "mcp_call",
        }
        self._warn_tools = {"write", "edit", "bash"}

    def check(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        session_rules: Ruleset = (),
        agent_rules: Ruleset = (),
        posture: str = "build",
    ) -> Decision:
        """Return a ``Decision`` for the given tool invocation.

        The shape of the answer:

        1. **the hard tier** -- what no rule may reopen: a bash red line, a
           tool the config denies, the skill policy;
        2. **the agent's veto** -- the agent layer evaluated *alone*: a deny
           there is absolute, because an agent that narrows a capability
           must not be reopened by a session rule (M1b);
        3. **the rules** -- the base layer, the config's standing
           permissions, the agent layer, the operator's files, the session's
           own layer and the global policy, in that order, the last match
           winning;
        4. **a saved grant**, which answers a question but never reopens a
           refusal;
        5. **what the rules said**: an allow (with the workspace edge still
           asked), a question, or the unknown-tool fallback.
        """
        name = tool_name.lower()

        # 1. The hard tier.  Three refusals that stand above the rules: a
        #    red line, a config denial, and the skill policy.  The bash red
        #    line is checked first because it is the most specific, and
        #    because a red line a model can spell around is worse than none.
        if name == "bash":
            reason = bash_denied(str(params.get("command", "")))
            if reason:
                return Decision(
                    PermissionDecision.DENY,
                    f"bash command blocked: {reason}",
                )
        if tool_name in self._denied_tools:
            return Decision(
                PermissionDecision.DENY,
                f"tool '{tool_name}' is denied by configuration",
            )
        if name == "skill" and str(params.get("action", "")).lower() == "read":
            decision = self._skill_decision(params)
            if decision is not None:
                return decision

        subjects = list(subjects_of(name, params))
        # A rule is a boundary over a *place*, not a spelling.  The same
        # subject list serves the veto and the layered query, so the two
        # cannot disagree about what this call touches.
        resolved = self._resolved_subject(name, params)
        if resolved is not None and resolved not in subjects:
            subjects.append(resolved)
        if name == "grep":
            root = _lexical_subject(str(params.get("path") or ""))
            if root and root not in subjects:
                subjects.append(root)

        # 2. The agent's veto.  Evaluated on the agent layer alone, so a
        #    session ``allow`` can never reopen it.  This is the one place
        #    the chain is not simply "last match wins": the agent's *deny*
        #    is absolute, while its *allow* is only one layer of the chain
        #    below (it wins base and standing, and loses to the operator).
        if agent_rules:
            veto = next(
                (
                    rule
                    for subject in subjects
                    for rule in (evaluate(name, subject, agent_rules, posture=posture),)
                    if rule is not None and rule.effect is Effect.DENY
                ),
                None,
            )
            if veto is not None:
                return Decision(
                    PermissionDecision.DENY,
                    veto.reason or f"denied by agent rule: {veto.action} {veto.resource}",
                )

        # 3. The rules.  One query per resource the call touches, combined:
        #    a refusal on any of them refuses the call.
        layers = self._layers(tuple(session_rules), tuple(agent_rules))
        winners = [
            evaluate(name, subject, *layers, posture=posture)
            for subject in subjects
        ]
        denial = next((w for w in winners if w is not None and w.effect is Effect.DENY), None)
        if denial is not None:
            return Decision(
                PermissionDecision.DENY,
                denial.reason or f"denied by rule: {denial.action} {denial.resource}",
            )

        # 4. A resource the operator already approved.  After the rules'
        #    refusals, so a standing grant can never reopen something that is
        #    refused outright, and before the rules' allows, so a grant
        #    keeps meaning what it means today: the operator's own answer
        #    for that path, with no second question about the workspace.
        key = self.grant_key(name, params)
        if key is not None and self._grant_applies(key, posture):
            return Decision(PermissionDecision.ALLOW, "")

        # 5. What the rules said.
        question = next((w for w in winners if w is not None and w.effect is Effect.ASK), None)
        if question is not None:
            return Decision(
                PermissionDecision.WARN,
                question.reason or f"tool '{tool_name}' requires confirmation",
            )
        if winners and all(w is not None and w.effect is Effect.ALLOW for w in winners):
            # B: a read of a path outside every root this session knows is a
            # question, not an answer.  opencode treats an external directory
            # the same way, and the reason is measurable: we used to refuse
            # outright, the model reached for ``bash`` instead, and that path
            # was unguarded -- so the refusal bought a speed bump and the
            # operator still said yes.
            outside = self._outside_resource_reason(name, params)
            if outside is not None:
                return Decision(PermissionDecision.WARN, outside)
            # M3 T5: an allow that rests on a **scoped** rule is a boundary --
            # the tool may run for that resource and nowhere else.  The plan
            # directory exception is exactly this, and the dispatch overlay
            # must not veto it a second time.  Every touched resource has to
            # be scoped: one wildcard allow would make the call unrestricted.
            specific = all(
                w is not None and w.resource != "*" for w in winners
            )
            return Decision(PermissionDecision.ALLOW, "", specific=specific)
        return Decision(
            PermissionDecision.WARN,
            f"unknown tool '{tool_name}' requires confirmation",
        )

    def deny_reason(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        session_rules: Ruleset = (),
        agent_rules: Ruleset = (),
        posture: str = "build",
    ) -> str:
        """Why the rule layer refuses this call, or ``""`` when it does not.

        The narrow view acceptance uses (M1a ③): it must see a DENY, never a
        WARN -- an ordinary proof command is ``bash`` and base says ask, so
        treating WARN as a refusal would put a human in front of every proof.
        """
        decision = self.check(
            tool_name,
            params,
            session_rules=session_rules,
            agent_rules=agent_rules,
            posture=posture,
        )
        if decision.decision is PermissionDecision.DENY:
            return decision.reason
        return ""

    # ---- the layers ----------------------------------------------------------

    def _base_rules(self) -> Ruleset:
        """The base layer, derived from the two lists that are the truth today.

        Derived, never copied.  A hand-copied table is how ``finish_research``
        went missing once (N1): the tool was added to the allow list and
        nobody remembered the rule table, so a tool that had been allowed
        for months started asking.
        """
        return tuple(
            [
                Rule(action=name, resource="*", effect=Effect.ALLOW)
                for name in sorted(self._allow_tools)
            ]
            + [
                Rule(action=name, resource="*", effect=Effect.ASK)
                for name in sorted(self._warn_tools)
            ]
            # M3 T9: appended last on purpose.  ``*`` crosses slashes, so these
            # cover a nested ``config/.env`` too; putting them before
            # ``read * allow`` would let the blanket allow win.
            + list(CREDENTIAL_READ_RULES)
        )

    def _standing_rules(self) -> Ruleset:
        """The permissions the config grants for good.

        A tool the config allows, and every tool of a server the operator
        declared trusted.  Both are *standing* permissions rather than
        refusals, so they sit above the base layer -- where a rule can
        still narrow them, which is the point of having rules at all.
        """
        return tuple(
            [
                Rule(action=name.lower(), resource="*", effect=Effect.ALLOW)
                for name in sorted(self._allowed_tools)
            ]
            + [
                Rule(action=f"{server.lower()}__*", resource="*", effect=Effect.ALLOW)
                for server in sorted(self._trusted_servers)
            ]
        )

    def _layers(
        self, session_rules: Ruleset, agent_rules: Ruleset = ()
    ) -> tuple[Ruleset, ...]:
        """The layers, lowest priority first; the last one has the last word.

        The base, then what the config grants, then the agent's own narrowing,
        then the operator's files (the project over the user), then this
        session's own layer, and last the global policy -- deny only, and the
        one thing nothing may talk its way out of.

        The agent sits *above* base/standing so it can narrow what they
        allow, and *below* the operator's files so a person can always narrow
        further.  Its deny is additionally absolute (``check`` evaluates that
        layer alone); its allow is just this one layer.
        """
        return (
            self._base_rules(),
            self._standing_rules(),
            tuple(agent_rules),
            self._rules.user,
            self._rules.project,
            tuple(session_rules),
            self._rules.policy,
        )

    def layer_counts(
        self, session_rules: Ruleset = (), agent_rules: Ruleset = ()
    ) -> dict[str, int]:
        """How many rules each layer holds, lowest priority first.

        The read view wants the shape of the policy, not every rule: a user
        with three files would otherwise scroll past the answer.  The names
        are the layer names the docs use, in the same order as ``_layers``.
        """
        names = ("base", "standing", "agent", "user", "project", "session", "policy")
        return {
            name: len(layer)
            for name, layer in zip(
                names,
                self._layers(tuple(session_rules), tuple(agent_rules)),
                strict=True,
            )
        }

    def hides(
        self,
        action: str,
        *,
        session_rules: Ruleset = (),
        agent_rules: Ruleset = (),
        posture: str = "build",
    ) -> bool:
        """Whether the whole action is cut from the tool catalogue.

        A scoped denial (``edit *.env deny``) does **not** hide the tool:
        the call is refused at dispatch, where the model can read which
        resource it hit.  This is the same predicate the execution query
        uses, so the catalogue and the verdict cannot drift apart.
        """
        lowered = action.lower()
        return wholly_disabled(
            lowered,
            *self._layers(tuple(session_rules), tuple(agent_rules)),
            posture=posture,
        ) or wholly_disabled(lowered, tuple(agent_rules), posture=posture)

    def partially_allowed(
        self,
        action: str,
        *,
        session_rules: Ruleset = (),
        agent_rules: Ruleset = (),
        posture: str = "build",
    ) -> bool:
        """Whether a scoped allow keeps this whole action reachable.

        The mirror of :meth:`hides`: a wholly denied action leaves the
        catalogue, a partly allowed one stays and its other resources are
        refused at dispatch.  The plan directory is the case this exists for
        -- without it the plan agent's write rule could never be reached,
        because the read-only overlay had already pruned the tool.
        """
        return partially_allowed(
            action.lower(),
            *self._layers(tuple(session_rules), tuple(agent_rules)),
            posture=posture,
        )

    def _skill_decision(self, params: Mapping[str, Any]) -> Decision | None:
        """The per-skill policy for the model's skill tool, or ``None``.

        Only the model-initiated read is judged: a human's @mention is its
        own authorization, and ``list`` names skills but reads none.
        """
        target = str(params.get("name", "")).strip()
        canonical = self._skill_resolver(target) if self._skill_resolver is not None else None
        policy = (
            self._skill_permissions.get(canonical or "")
            or self._skill_permissions.get(target)
            or "allow"
        )
        if policy == "deny":
            return Decision(
                PermissionDecision.DENY,
                f"skill '{target}' is denied by configuration",
            )
        if policy == "ask":
            return Decision(
                PermissionDecision.WARN,
                f"skill '{target}' requires confirmation",
            )
        return None

    # ---- outside-workspace resources (B) -----------------------------------

    def _outside_resource_reason(self, tool_name: str, params: Mapping[str, Any]) -> str | None:
        """Why this call reaches outside the workspace, or ``None`` if it does not.

        Pure containment on already-normalised text, because the gate is asked
        before the tool runs and must not touch the filesystem.  ``Sandbox``
        repeats the check against resolved paths, so a symlink that points out
        is caught there even when this says the path is inside.
        """
        if not self._workspace:
            return None
        resource = str(params.get("path") or params.get("pattern") or "").strip()
        if not resource:
            return None
        # A glob pattern is not a path; only its static prefix names a place.
        target = _directory_of(resource) if tool_name in _DIR_TOOLS else resource
        if not target or any(char in target for char in "*?[]{}"):
            return None
        candidate = Path(target).expanduser()
        if not candidate.is_absolute():
            return None  # relative: it is inside the workspace by construction
        try:
            Path(os.path.normpath(str(candidate))).relative_to(self._workspace)
        except ValueError:
            return (
                f"'{tool_name}' targets {target}, outside workspace "
                f"'{self._workspace}' — it needs your approval"
            )
        return None

    def _resolved_subject(
        self, tool_name: str, params: Mapping[str, Any]
    ) -> str | None:
        """The real location of a path tool's target, relative to the workspace.

        The rule language compares text; a symlink is the one place where the
        text and the thing it names disagree.  ``Path.resolve`` follows the
        parents that exist and leaves the final missing component alone, so a
        write of a new file is judged by the directory it will land in.
        """
        if not self._workspace or tool_name not in ("read", "write", "edit", "grep"):
            return None
        raw = str(params.get("path") or "").strip()
        if not raw:
            return None
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self._workspace) / candidate
        try:
            resolved = candidate.resolve()
            return resolved.relative_to(Path(self._workspace)).as_posix()
        except (OSError, ValueError):
            # Outside every root: the sandbox owns that refusal, and adding a
            # made-up subject here would let a rule match text nobody wrote.
            return None

    def _grant_applies(self, key: str, posture: str) -> bool:
        """Whether a stored grant answers this key in this posture.

        A bare grant answers everywhere.  A scoped one answers in the posture
        it was given in and in every looser one (the ladder ``plan <
        readonly < build``); an unknown posture is treated as stricter than
        ``plan``, so a build grant cannot leak through a misspelling.

        M-D3: a grant whose deadline has passed is treated as absent, checked
        *before* the posture ladder so an expired grant never answers in any
        posture.  The clock is read per call, which is what lets a grant loaded
        from disk expire inside the session that loaded it.
        """
        try:
            current = POSTURES.index(posture)
        except ValueError:
            current = -1
        now = self._clock()
        for stored in self._grants:
            deadline = self._grant_expiry.get(stored)
            if deadline is not None and deadline < now:
                continue
            base, granted = _split_grant(stored)
            if base != key:
                continue
            if granted is None or current >= POSTURES.index(granted):
                return True
        return False

    def allow_once(self, tool_name: str, params: Mapping[str, Any]) -> None:
        """Remember one approval for this process only (B).

        A single "yes" has to reach the tool, and the sandbox asks the gate
        before refusing -- so the answer is parked here rather than dropped.

        Deliberately *not* in :attr:`grants`: that set is what "always" means,
        and ``/permissions`` prints it as standing permissions.  A one-off answer
        that showed up there would make every ``y`` look like a remembered
        decision. This is a separate, session-scoped set -- the same distinction
        the approval vocabulary already draws between yes and always.
        """
        key = self.grant_key(tool_name, params)
        if key is not None:
            self._once_grants.add(key)

    def path_allowed(self, path: str | Path) -> bool:
        """Whether the operator has already authorized this path (B).

        ``Sandbox`` asks this before refusing, so an "always" recorded against a
        directory reaches the tool instead of only the gate.  Containment is by
        prefix on the *granted* path's directory: a grant for ``/outside`` lets
        the tool read ``/outside/deep/file.py``, and nothing above it.

        The posture suffix is ignored on purpose: the gate has already made
        the posture decision, and a build grant for an outside path must still
        reach the tool it was given for.  In a stricter posture the gate asks
        first; the one-off ``yes`` arrives here as a bare key.

        M-D3: a grant whose TTL has passed is treated as absent, on this path too
        -- otherwise an expired grant would keep opening the *file* tools while
        the gate had already stopped answering it, the two halves of the same
        boundary disagreeing about what is authorized.
        """
        if not self._workspace:
            return False
        try:
            resolved = Path(path).expanduser().resolve()
        except OSError:
            return False
        for key in (*self.grants, *self._once_grants):
            tool_name, _, granted = key.partition(":")
            if tool_name not in _PATH_TOOLS and tool_name not in _DIR_TOOLS:
                continue
            root = Path(_grant_base(granted)).expanduser()
            if not root.is_absolute():
                root = Path(self._workspace) / root
            try:
                resolved.relative_to(Path(os.path.normpath(str(root.resolve()))))
            except (ValueError, OSError):
                continue
            return True
        return False

    def trust_server(self, name: str) -> None:
        """Trust a server's tools from now on (M5, the wizard's last step).

        The gate is built once from the config at startup, so a server added at
        runtime has to be trusted on the *live* object — otherwise the answer
        the operator just gave would be ignored until the next start, which is
        the same set of rules from_config installs.
        """
        if name:
            self._trusted_servers.add(str(name))

    def grant(
        self,
        tool_name: str,
        params: Mapping[str, Any],
        *,
        posture: str | None = None,
    ) -> str | None:
        """Remember exactly this call; return the key, or ``None`` if unnameable.

        Called by the dispatcher when the human answered ``ALWAYS``.  Writing
        through to the store is best effort -- the operator did say always, so
        the grant holds for this process even if it cannot be written down.

        ``posture`` scopes the answer to that posture and every looser one.
        ``None`` writes a bare key meaning everywhere, which is both the old
        format and what a direct caller (a test, a replay) gets.
        """
        key = self.grant_key(tool_name, params)
        if key is None:
            return None
        stored = key if posture is None else f"{key}@{posture}"
        self._grants.add(stored)
        store = self._store
        if store is not None:
            # Unconditional: a re-approval refreshes the store's grantedAt (and
            # so the TTL), which is the whole point of answering "always" twice.
            store.add(stored)
        # M-D3: a grant written with a TTL carries its deadline.  Re-approving
        # refreshes the store's timestamp; the in-memory deadline moves with it
        # so the answer holds for the new lifetime, not the old one.
        self._grant_expiry.pop(stored, None)
        if self._grant_ttl is not None:
            self._grant_expiry[stored] = self._clock() + float(self._grant_ttl)
        return key

    def grant_key(
        self, tool_name: str, params: Mapping[str, Any]
    ) -> str | None:
        """Whether this call may be remembered, resolved in this workspace."""
        return grant_key(tool_name, params, workspace=self._workspace or None)

    @property
    def grants(self) -> tuple[str, ...]:
        """What is currently remembered -- the *live* ones, sorted for display.

        M-D3: an expired grant is not "remembered" any more, so the panel shows
        the same set the gate answers with.
        """
        now = self._clock()
        live = (
            key
            for key in self._grants
            if self._grant_expiry.get(key, now) >= now
        )
        return tuple(sorted(live))

    def grant_expiry_map(self) -> dict[str, float]:
        """The deadline of every live grant that has one (epoch seconds)."""
        now = self._clock()
        return {
            key: deadline
            for key, deadline in self._grant_expiry.items()
            if key in self._grants and deadline >= now
        }

    def grant_records(self) -> list[tuple[str, float | None]]:
        """Every live grant as ``(key, expiry)``; ``None`` expiry is permanent.

        The panel's row shape: one call yields both halves (the key and whether
        it expires) so the two cannot disagree about what is remembered.
        """
        expiry = self.grant_expiry_map()
        return [(key, expiry.get(key)) for key in self.grants]

    def revoke(self, key: str) -> bool:
        """Forget one grant, by full key or by base name.

        A stored key is ``tool:path[@posture]``.  Passing the **full** key
        (``edit:src/foo.py@build``) removes exactly that variant; passing the
        **base name** (the part before ``@``, e.g. ``edit:src/foo.py``) removes
        every posture variant of it.  Both halves -- memory and disk -- are
        cleared, and the deadline goes with them.  Returns whether anything was
        removed, so a command can say "no such grant" instead of "done".
        """
        text = str(key).strip()
        if not text:
            return False
        _, posture = _split_grant(text)
        if posture is not None:
            targets = [stored for stored in self._grants if stored == text]
        else:
            targets = [stored for stored in self._grants if _grant_base(stored) == text]
        if not targets:
            return False
        for stored in targets:
            self._grants.discard(stored)
            self._grant_expiry.pop(stored, None)
            if self._store is not None:
                self._store.revoke(stored)
        return True

    def revoke_all(self) -> None:
        """Forget everything, on disk as well as in memory."""
        self._grants.clear()
        self._grant_expiry.clear()
        self._once_grants.clear()
        if self._store is not None:
            self._store.clear()

    @classmethod
    def from_config(
        cls,
        app_cfg: Any,
        *,
        workspace: str | None = None,
        skill_resolver: Callable[[str], str | None] | None = None,
        #: M-D3: the clock the gate's expiry check reads; the store is built with
        #: the same one so a grant written and a grant read agree on "now".
        clock: Callable[[], float] = time.time,
    ) -> PermissionGate:
        """The one place the composition root's policy is read (R7d).

        Nothing about the *tool list* is needed: a server is trusted by name, so
        this works before (and independently of) the MCP connection.

        ``workspace`` does two jobs now.  It turns on persistence (G4b-2): the
        grants the operator has given in this directory are loaded, and new ones
        are written beside them.  Without it the gate is session-only -- which is
        what a sub-agent builds, and it never asks anyway.  It also tells the
        gate *where the workspace is* (B), which is how a read of ``src/x.py`` is
        told apart from a read of ``/elsewhere/x.py``; without it every path tool
        call is ALLOW, and the sandbox's own refusal is the only thing standing
        between the model and the rest of the filesystem.

        ``skill_resolver`` (E-5) turns a skill spelling into its canonical id so
        ``ai.skill_permissions`` keys and call arguments meet in one namespace.

        M-D3: the store is read with :meth:`GrantStore.load_with_expiry`, so a
        grant loaded from disk carries the deadline it was written with -- the
        expiry check reads the clock per call, which is what lets it expire
        inside the session that loaded it.
        """
        mcp = getattr(app_cfg, "mcp", None)
        servers = getattr(mcp, "servers", None) or []
        ttl = getattr(getattr(app_cfg, "security", None), "grant_ttl_seconds", DEFAULT_GRANT_TTL)
        store = GrantStore(workspace, ttl=ttl, clock=clock) if workspace else None
        denied: list[str] = []
        allowed: list[str] = []
        for server in servers:
            name = str(getattr(server, "name", ""))
            rules = getattr(server, "permissions", None)
            for remote in getattr(rules, "deny", None) or []:
                denied.append(f"{name}__{remote}")
            for remote in getattr(rules, "allow", None) or []:
                allowed.append(f"{name}__{remote}")
        grants, expiry = store.load_with_expiry() if store is not None else ((), {})
        return cls(
            trusted_servers=[
                str(getattr(server, "name", ""))
                for server in servers
                if getattr(server, "auto_approve", False)
            ],
            grants=grants,
            grant_expiry=expiry,
            grant_ttl=ttl,
            clock=clock,
            store=store,
            denied_tools=denied,
            allowed_tools=allowed,
            skill_permissions=getattr(getattr(app_cfg, "ai", None), "skill_permissions", None)
            or {},
            skill_resolver=skill_resolver,
            workspace=workspace,
            # Without a workspace there is no project to read, and reading
            # the real user's files here would make every test that builds
            # a gate depend on the machine it runs on.  Production always
            # passes a workspace; a gate without one is session-only.
            rules=load_file_rules(workspace) if workspace is not None else NO_FILE_RULES,
        )


__all__ = ["PermissionGate", "PermissionDecision", "Decision", "grant_key"]
