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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.security.grants import GrantStore


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

    def __init__(self, decision: PermissionDecision, reason: str = "") -> None:
        self.decision = decision
        self.reason = reason

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PermissionDecision):
            return self.decision == other
        if isinstance(other, Decision):
            return self.decision == other.decision and self.reason == other.reason
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.decision)

    def __repr__(self) -> str:
        return f"Decision({self.decision.value!r}, reason={self.reason!r})"


#: Hard deny patterns for bash commands
#:
#: Matched against the *normalized* command (``_normalize_command``), not the raw
#: text.  The table used to be applied with ``pattern in command``, and a space
#: was enough to walk past it: ``rm  -rf /``, ``rm -r -f /``, ``RM -RF /`` and
#: ``chmod  777`` all returned WARN.  A red line a model can defeat by respelling
#: it is worse than no red line, because the first refusal teaches the spelling
#: that works.
_BASH_HARD_DENY_PATTERNS: list[tuple[str, str]] = [
    ("rm -rf /", "recursive root deletion"),
    ("sudo ", "sudo escalation"),
    ("shutdown", "system shutdown"),
    ("reboot", "system reboot"),
    ("mkfs", "filesystem format"),
    ("dd if=", "disk write"),
    ("> /dev/sd", "raw disk write"),
    ("git push --force", "force git push"),
    ("git push -f ", "force git push (short)"),
    ("chmod 777", "world-writable permissions"),
]

#: Credential paths bash must never read.  ``bash`` is never grantable
#: (``_NEVER_REMEMBERED``), so a WARN here means the model can ask until you say
#: yes; only this list says no regardless.  The file tools answer the same
#: request by asking the operator, which is why leaving this empty made the
#: sandbox boundary a speed bump: ``read`` was refused, the model reached for
#: ``bash cat``, and the answer was yes.
#:
#: A *path shape*, not a word: ``.env.example`` is documentation and
#: ``grep password src/`` is how one audits for a leak, so neither is matched.
_CREDENTIAL_PATTERNS: tuple[str, ...] = (
    "/.ssh/",
    "/.aws/",
    "/.config/gh/",
    "/.gnupg/",
    "/.kube/",
    "/.docker/config.json",
    "/.npmrc",
    "/.pypirc",
    "/.netrc",
    "/etc/shadow",
    "/etc/sudoers",
)

#: Exact basenames that are credentials wherever they appear.
_CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
)

#: Suffixes that are credentials wherever they appear.
_CREDENTIAL_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".keystore")

#: Shell metacharacters that end a word; a path token is read between them.
_SHELL_SPLIT = re.compile(r"[\s;|&<>()'\"`]+")
#: A leading ``~`` is the home directory wherever it is written.
_HOME_PREFIX = re.compile(r"^~(?=/|$)")


def _normalize_command(command: str) -> str:
    """Collapse the spellings that let a red line be walked past.

    Runs of whitespace become one space, so ``rm  -rf  /`` normalizes to
    ``rm -rf /``.  Nothing else is rewritten: the table is matched against this
    text as a whole, and collapsing whitespace is the only normalization that
    cannot change which command runs.
    """
    return re.sub(r"\s+", " ", command).strip()


def _command_words(command: str) -> list[str]:
    """The command's words, ``~`` expanded, for shape-based matching.

    ``rm -r -f /`` and ``rm -rf /`` are different word lists but the same
    command, so :func:`_dangerous_recursive_root_delete` compares the *set* of
    flags rather than the string.
    """
    words = _SHELL_SPLIT.split(command)
    return [_HOME_PREFIX.sub(str(Path.home()), word) for word in words if word]

def _dangerous_recursive_root_delete(words: list[str]) -> bool:
    """``rm`` with recursive+force aimed at ``/``, whatever the flag spelling.

    Every ordering and every amount of whitespace collapses to the same two
    facts: the program is ``rm``, it carries both ``-r`` and ``-f`` (in any
    grouping), and the target is the filesystem root.
    """
    if not words:
        return False
    program = Path(words[0]).name.lower()
    if program != "rm":
        return False
    flags: set[str] = set()
    targets: list[str] = []
    for word in words[1:]:
        if word.startswith("-") and word != "--":
            flags.update(word[1:].lower())
        elif word in ("--recursive", "--force"):
            flags.update("r" if word == "--recursive" else "f")
        else:
            targets.append(word)
    return "r" in flags and "f" in flags and any(t.rstrip("/") in ("", "/") for t in targets)


def _world_writable_chmod(words: list[str]) -> bool:
    """``chmod 777`` in any spelling, including octal written out long."""
    if not words or Path(words[0]).name.lower() != "chmod":
        return False
    return any(word in ("777", "0777", "a+rwx", "a+w", "+rwx") for word in words[1:])


def _reads_credential(words: list[str]) -> str:
    """The credential this command touches, or ``""``.

    A word is a path if it looks like one (starts at ``/`` or ``~``, or carries
    a suffix); the home directory is expanded first so ``~/.ssh`` and
    ``/home/you/.ssh`` compare equal.  Matching is on the *word*, so
    ``cat .env.example`` is not ``cat .env`` -- the token has to be the file.
    """
    for word in words[1:]:
        candidate = Path(word)
        name = candidate.name
        if name in _CREDENTIAL_NAMES:
            return name
        if any(candidate.name.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
            return candidate.name
        posix = candidate.as_posix()
        for pattern in _CREDENTIAL_PATTERNS:
            if pattern in posix:
                return pattern.strip("/")
    return ""


def bash_denied(command: str) -> str:
    """Why this bash command may not run at all, or ``""`` when it may be asked.

    Pure: no IO, no clock.  The dispatcher and the gate share this so the
    approval panel and the refusal can never disagree about what is forbidden.
    """
    normalized = _normalize_command(command)
    words = _command_words(normalized)
    if _dangerous_recursive_root_delete(words):
        return "recursive root deletion (rm -rf /)"
    if _world_writable_chmod(words):
        return "world-writable permissions (chmod 777)"
    secret = _reads_credential(words)
    if secret:
        return f"credential file ({secret})"
    for pattern, reason in _BASH_HARD_DENY_PATTERNS:
        if pattern in normalized:
            return f"{reason} ('{pattern}')"
    return ""


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


def grant_key(tool_name: str, params: Mapping[str, Any]) -> str | None:
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
    ) -> None:
        #: Calls the operator answered "Always" for, as ``grant_key`` strings.
        #: Seeded from the store at startup and extended by ``grant``.
        self._grants = {str(key) for key in grants}
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

    def check(self, tool_name: str, params: dict[str, Any]) -> Decision:
        """Return a ``Decision`` for the given tool invocation."""
        name = tool_name.lower()

        # 1. Bash hard-deny patterns (checked first, most specific).  The matcher
        # normalizes and reads words, so a respelled command cannot walk past a
        # red line, and a credential path is refused outright (bash is never
        # grantable, so a WARN there is only a question).
        if name == "bash":
            command = str(params.get("command", ""))
            reason = bash_denied(command)
            if reason:
                return Decision(
                    PermissionDecision.DENY,
                    f"bash command blocked: {reason}",
                )
            return Decision(
                PermissionDecision.WARN,
                "bash command requires confirmation",
            )

        # 1.5. M2: a tool the config denies outright.  Before the grants, for the
        # same reason the bash list is first: a standing "always" may never
        # reopen what the operator's own config refuses.
        if tool_name in self._denied_tools:
            return Decision(
                PermissionDecision.DENY,
                f"tool '{tool_name}' is denied by configuration",
            )

        # 2. A resource the operator already approved.  After the deny rules, so
        # a standing grant can never reopen something that is refused outright:
        # doing this first would make every future path-based deny bypassable by
        # an "always" the user gave before it existed.
        key = grant_key(name, params)
        if key is not None and key in self._grants:
            return Decision(PermissionDecision.ALLOW, "")

        # 2.5. E-5: a per-skill policy for the model's skill tool.  Only the
        # model-initiated read is judged: a human's @mention is its own
        # authorization, and ``list`` names skills but reads none.
        if name == "skill" and str(params.get("action", "")).lower() == "read":
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

        # 3. Known allow-list
        if name in self._allow_tools:
            # B: a read of a path outside every root this session knows is a
            # question, not an answer.  opencode treats an external directory
            # the same way (``06-opencode长任务机制.md:20``), and the reason is
            # measurable: we used to refuse outright, the model reached for
            # ``bash`` instead, and that path was unguarded -- so the refusal
            # bought a speed bump and the operator still said yes.
            outside = self._outside_resource_reason(name, params)
            if outside is not None:
                return Decision(PermissionDecision.WARN, outside)
            return Decision(PermissionDecision.ALLOW, "")

        # 3.5. M2: a tool the config explicitly allows — no prompt, like the
        # trusted-server rule but for one tool instead of the whole server.
        if tool_name in self._allowed_tools:
            return Decision(PermissionDecision.ALLOW, "")

        # 4. Known warn-list
        if name in self._warn_tools:
            return Decision(
                PermissionDecision.WARN,
                f"tool '{tool_name}' requires confirmation",
            )

        # 5. A tool from a server the user declared trusted (R7d).  Checked before
        # the fail-closed rule, and matched on the *server* segment only, so a
        # local tool cannot be trusted by naming itself "bash__something".
        server, sep, _remote = tool_name.partition("__")
        if sep and server in self._trusted_servers:
            return Decision(PermissionDecision.ALLOW, "")

        # 6. Unknown tools → WARN (fail-closed / conservative)
        return Decision(
            PermissionDecision.WARN,
            f"unknown tool '{tool_name}' requires confirmation",
        )

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

    def allow_once(self, tool_name: str, params: Mapping[str, Any]) -> None:
        """Remember one approval for this process only (B).

        A single "yes" has to reach the tool, and the sandbox asks the gate
        before refusing -- so the answer is parked here rather than dropped.

        Deliberately *not* in :attr:`grants`: that set is what "always" means,
        and ``/permissions`` prints it as standing permissions.  A one-off answer
        that showed up there would make every ``y`` look like a remembered
        decision.  This is a separate, session-scoped set -- the same distinction
        the approval vocabulary already draws between yes and always.
        """
        key = grant_key(tool_name, params)
        if key is not None:
            self._once_grants.add(key)

    def path_allowed(self, path: str | Path) -> bool:
        """Whether the operator has already authorized this path (B).

        ``Sandbox`` asks this before refusing, so an "always" recorded against a
        directory reaches the tool instead of only the gate.  Containment is by
        prefix on the *granted* path's directory: a grant for ``/outside`` lets
        the tool read ``/outside/deep/file.py``, and nothing above it.
        """
        if not self._workspace:
            return False
        try:
            resolved = Path(path).expanduser().resolve()
        except OSError:
            return False
        for key in (*self._grants, *self._once_grants):
            tool_name, _, granted = key.partition(":")
            if tool_name not in _PATH_TOOLS and tool_name not in _DIR_TOOLS:
                continue
            root = Path(granted).expanduser()
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

    def grant(self, tool_name: str, params: Mapping[str, Any]) -> str | None:
        """Remember exactly this call; return the key, or ``None`` if unnameable.

        Called by the dispatcher when the human answered ``ALWAYS``.  Writing
        through to the store is best effort -- the operator did say always, so
        the grant holds for this process even if it cannot be written down.
        """
        key = grant_key(tool_name, params)
        if key is None:
            return None
        if key not in self._grants:
            self._grants.add(key)
            store = self._store
            if store is not None:
                store.add(key)
        return key

    @property
    def grants(self) -> tuple[str, ...]:
        """What is currently remembered, sorted for display."""
        return tuple(sorted(self._grants))

    def revoke_all(self) -> None:
        """Forget everything, on disk as well as in memory."""
        self._grants.clear()
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
        """
        mcp = getattr(app_cfg, "mcp", None)
        servers = getattr(mcp, "servers", None) or []
        store = GrantStore(workspace) if workspace else None
        denied: list[str] = []
        allowed: list[str] = []
        for server in servers:
            name = str(getattr(server, "name", ""))
            rules = getattr(server, "permissions", None)
            for remote in getattr(rules, "deny", None) or []:
                denied.append(f"{name}__{remote}")
            for remote in getattr(rules, "allow", None) or []:
                allowed.append(f"{name}__{remote}")
        return cls(
            trusted_servers=[
                str(getattr(server, "name", ""))
                for server in servers
                if getattr(server, "auto_approve", False)
            ],
            grants=store.load() if store is not None else (),
            store=store,
            denied_tools=denied,
            allowed_tools=allowed,
            skill_permissions=getattr(getattr(app_cfg, "ai", None), "skill_permissions", None)
            or {},
            skill_resolver=skill_resolver,
            workspace=workspace,
        )


__all__ = ["PermissionGate", "PermissionDecision", "Decision", "grant_key"]
