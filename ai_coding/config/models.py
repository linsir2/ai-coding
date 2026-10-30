"""Pydantic configuration models with defaults aligned to the original Java values."""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_coding.domain.model_config import ModelConfig

_log = logging.getLogger(__name__)


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_file_size: int = 10_485_760  # 10 MB
    timeout_seconds: int = 30


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bash: ToolConfig = Field(default_factory=ToolConfig)
    read: ToolConfig = Field(default_factory=ToolConfig)
    write: ToolConfig = Field(default_factory=ToolConfig)
    edit: ToolConfig = Field(default_factory=ToolConfig)
    glob: ToolConfig = Field(default_factory=ToolConfig)
    # r17/T2: content search.  Same tier as glob (read-only, ALLOW).
    grep: ToolConfig = Field(default_factory=ToolConfig)
    # R6c: the model asking a human.  `timeout_seconds` is how long it may wait for
    # an answer (the tool adds 15s of slack for the dispatcher, like bash/subAgent).
    # Only registered where somebody can actually answer — see tools/builder.py.
    ask_user: ToolConfig = Field(
        default_factory=lambda: ToolConfig(timeout_seconds=300)
    )
    # B: directories the file tools may touch without asking, beyond the
    # workspace itself.  A standing answer, so it belongs here rather than in
    # the grant store: the operator declares a tree once and every session in
    # this workspace reads it.  Patterns are expanded at startup
    # (``tools/builder.py::expand_root_patterns``); empty = the workspace only,
    # which is the pre-B behaviour.  An unlisted path is not refused, it asks.
    extra_workspace_roots: list[str] = Field(default_factory=list)


def default_read_deny_paths() -> list[str]:
    """The read boundary's default denylist (M-D2 §B).

    A **denylist**, deliberately: the write face is an allow-list (``writable_paths``)
    and the read face is not derived from it -- the two are independent sides of
    one wall.  ``--ro-bind / /`` makes the whole root readable, so without this
    list ``~/.ssh/id_rsa``, ``~/.aws/credentials`` and the repository's own API
    key are readable inside the sandbox.

    Expanded through :func:`ai_coding.paths.config_home` /
    :func:`~ai_coding.paths.data_home` rather than a hardcoded ``~/.config``:
    when the XDG config root is overridden, a hardcoded mask lands on an empty
    directory and the key stays readable (review P2-1).  ``~`` is expanded by
    the composition root, so the user-tier entries stay relative.

    **Incomplete on purpose.**  A denylist is always one rule short -- that is
    the research's Pillar finding -- so this is a capability boundary, not a
    guarantee.  It also covers only the *user* tier: a workspace's own
    ``config.yaml`` may hold the same API key, and the workspace is necessarily
    readable (otherwise the tools cannot work).  Both are recorded in
    ``docs/plan-m-d2.md`` §7.1.
    """
    from ai_coding.paths import config_home, data_home

    xdg_config = config_home().parent
    xdg_data = data_home().parent
    return [
        "~/.ssh",
        "~/.aws",
        str(xdg_config / "gcloud"),
        "~/.azure",
        "~/.gnupg",
        "~/.netrc",
        "~/.docker/config.json",
        "~/.kube/config",
        # The repository's own credentials: the user config (API key) and the
        # MCP OAuth tokens.  Readable, they hand over the model credential and
        # the MCP authorisation.
        str(config_home()),
        str(xdg_data / "ai-coding" / "mcp-auth.json"),
        "/etc/shadow",
        "/etc/ssh/ssh_host_*_key",
        "~/.git-credentials",
        "~/.npmrc",
        "~/.pypirc",
        str(xdg_config / "gh" / "hosts.yml"),
        "~/.cargo/credentials",
        "~/.terraform.d/credentials",
        "~/.bash_history",
        "~/.python_history",
        "~/.viminfo",
        # Other users' homes: the mask is per-user, but a sandbox that shares
        # the host root still sees them.
        "/home/*/.ssh",
        "/root/.ssh",
    ]


#: The AF_UNIX pathname sockets masked by default (M-D2 §D).  bwrap has no
#: connect/bind filtering, so pathname sockets are hidden the same way files
#: are; abstract sockets have no path and are only reachable through the
#: seccomp tier (``block_af_unix``).
#:
#: The list is a denylist -- it is always one rule short, and the plan's Pillar
#: finding applies to it exactly as to the read boundary.  It was expanded after
#: the sandbox was measured reaching ``/run/supervisor.sock`` and
#: ``/run/shared/minimal-ssh-agent.sock`` (review P2-2): a supervisor socket is
#: command execution, and an ssh-agent socket is a signing oracle, so both are
#: at least as dangerous as a D-Bus socket.
DEFAULT_AF_UNIX_DENY_PATHS: tuple[str, ...] = (
    # per-user session buses and agents (the CVE-2026-47128 class)
    "/run/user/*/bus",
    "/run/user/*/systemd/private",
    "/run/user/*/pulse/native",
    "/run/user/*/keyring/control",
    "/run/user/*/dconf",
    "/run/user/*/at-spi/bus_*",
    "/run/user/*/gvfsd",
    "/run/user/*/snapd.socket",
    "/run/user/*/gnome-*",
    # system buses
    "/run/dbus/system_bus_socket",
    "/var/run/dbus/system_bus_socket",
    "/run/systemd/private",
    "/run/systemd/notify",
    # container runtimes
    "/run/docker.sock",
    "/var/run/docker.sock",
    "/run/containerd/containerd.sock",
    # supervisors (a supervisor socket is command execution)
    "/run/supervisor.sock",
    "/var/run/supervisor.sock",
    # shared agents (an ssh-agent socket is a signing oracle)
    "/run/shared/minimal-ssh-agent.sock",
    "/run/shared/*ssh-agent*.sock",
    # databases / caches
    "/run/postgresql/.s.PGSQL.*",
    "/var/run/postgresql/.s.PGSQL.*",
    "/run/redis/redis.sock",
    "/var/run/redis/redis.sock",
    "/run/uuidd/request",
    "/run/acpid.socket",
    "/run/openvswitch/db.sock",
    "/run/nscd/socket",
    "/var/run/nscd/socket",
    # snapd
    "/run/snapd.socket",
    "/run/snapd-snap.socket",
)


class NetworkConfig(BaseModel):
    """M-D2 §C: the network wall.

    Three modes, deliberately not a boolean:

    * ``off``   -- the host network, unchanged (the pre-M-D2 behaviour).
    * ``deny``  -- ``--unshare-net``: a netns with only loopback.  **The default**:
      a write/read wall without a network wall leaks, because what the sandbox
      reads can be sent straight out.
    * ``proxy`` -- ``--unshare-net`` plus one bound Unix socket and an in-sandbox
      bridge; the only **TCP/UDP** way out is the whitelisted HTTP/CONNECT
      proxy in ``server/sandbox_proxy.py``.  AF_UNIX is a parallel channel the
      proxy does not see, covered by the AF_UNIX masks instead (§D).

    ``allow`` is a domain whitelist decided by the proxy.  Empty means **deny
    everything**.  ``"*"`` is rejected **at load time** -- a wildcard is not a
    wall, and a configuration error must fail loudly at startup rather than at
    the first bash command (review P2-10).

    Each entry is a domain, optionally ``domain:port`` (or
    ``domain:port1,port2``).  A bare domain allows only ports 80 and 443: a
    domain-only entry used to admit every port, so ``allow: ["example.com"]``
    also opened ``CONNECT example.com:22`` and ``:3389`` -- lateral-movement
    ports behind a web allow-list (review P2-1).

    ``proxy_socket`` has no default on purpose: a guessed path would silently
    point at a socket nobody is listening on.  ``mode="proxy"`` without it is a
    load error (review P2-6).

    ``bridge_port`` is the loopback port the in-sandbox bridge listens on;
    ``0`` lets the bridge pick a free one and hand it to the command through
    the proxy environment variables.
    """

    model_config = ConfigDict(extra="ignore")

    mode: Literal["off", "deny", "proxy"] = "deny"
    proxy_socket: Path | None = None
    allow: list[str] = Field(default_factory=list)
    bridge_port: int = 0

    @field_validator("allow")
    @classmethod
    def _reject_wildcard(cls, value: list[str]) -> list[str]:
        for entry in value:
            text = str(entry).strip()
            if text == "*":
                raise ValueError(
                    'network.allow must not contain "*": a wildcard allow-list '
                    "is no network wall"
                )
            # A CIDR entry the proxy cannot parse would silently deny
            # everything (it is not a hostname); a configuration error must
            # fail loudly at load time, like the wildcard above (§D.2).
            if "/" in text:
                try:
                    ipaddress.ip_network(text, strict=False)
                except ValueError as exc:
                    raise ValueError(
                        f"malformed CIDR in network.allow: {text!r}"
                    ) from exc
        return value

    @model_validator(mode="after")
    def _proxy_requires_a_socket(self) -> NetworkConfig:
        if self.mode == "proxy" and self.proxy_socket is None:
            raise ValueError("network.mode='proxy' requires network.proxy_socket")
        return self


class ProcessSandboxConfig(BaseModel):
    """M-D1/D2: the OS-level process sandbox (the write and read boundaries).

    ``mode`` is the policy tier and ``allow_bypass`` is roadmap's explicit
    "do not isolate this run".  ``writable_paths`` are the extra writable
    directories beyond the roots the process layer shares with the path
    sandbox (``Sandbox.roots``); the default carries ``~/.cache`` and never a
    credential directory.  Widening this narrows the sandbox -- the trade is
    written down in ``docs/plan-m-d.md`` §A.2.

    ``read_deny_paths`` is the read boundary (M-D2 §B): paths hidden from the
    sandbox (directories become empty, files become unreadable).  It is a
    denylist, symmetric to ``writable_paths`` and never derived from it.

    ``af_unix_deny_paths`` / ``block_af_unix`` are the AF_UNIX layer (M-D2 §D):
    pathname sockets are masked, and the seccomp tier can refuse
    ``socket(AF_UNIX)`` outright -- including the abstract namespace, which has
    no path to mask.

    ``network`` is the network wall (M-D2 §C): ``off`` / ``deny`` / ``proxy``,
    default ``deny``.  It lives here, under ``process_sandbox``, so the existing
    ``EXECUTION_KEYS`` coverage also stops a project layer from opening it.

    The default is ``enforce``: roadmap's acceptance criterion is "a process
    under ``enforce`` cannot write outside the workspace", and a default of
    ``off`` would make it empty out of the box.  On a host with no unprivileged
    user namespaces (a common Debian/RHEL configuration) that means every bash
    call is refused until the operator sets ``warn`` -- which is the honest
    outcome, and why ``config.example.yaml`` shows the ``warn`` fallback.
    """

    model_config = ConfigDict(extra="ignore")

    mode: Literal["off", "warn", "enforce"] = "enforce"
    allow_bypass: bool = False
    writable_paths: list[str] = Field(default_factory=lambda: ["~/.cache"])
    read_deny_paths: list[str] = Field(default_factory=default_read_deny_paths)
    af_unix_deny_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_AF_UNIX_DENY_PATHS)
    )
    block_af_unix: bool = False
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    @model_validator(mode="after")
    def _proxy_and_block_af_unix_are_exclusive(self) -> ProcessSandboxConfig:
        if self.network.mode == "proxy" and self.block_af_unix:
            raise ValueError(
                "block_af_unix and network.mode='proxy' are mutually exclusive: "
                "the seccomp filter refuses socket(AF_UNIX), so the sandbox "
                "proxy bridge could not reach the proxy socket"
            )
        return self

    @model_validator(mode="after")
    def _warn_when_the_read_boundary_is_off(self) -> ProcessSandboxConfig:
        """An empty ``read_deny_paths`` silently closes the read boundary.

        The default is non-empty, so an empty list is an explicit "turn the read
        boundary off".  Doing that silently -- with no warning at load time --
        means a workspace config (or a pasted example) can hand the sandbox's
        read protection away without anyone noticing (review P3-6).  The warning
        is emitted once, here, at load time.
        """
        if not self.read_deny_paths:
            _log.warning(
                "process_sandbox.read_deny_paths is empty: the read boundary is "
                "OFF, so ~/.ssh, ~/.aws and this repository's own credentials "
                "are readable inside the sandbox"
            )
        return self


class SecurityConfig(BaseModel):
    """M-D3: the standing-grant lifetime.

    ``grant_ttl_seconds`` is how long an "always" stays in force: a grant
    written ``grant_ttl_seconds`` seconds ago stops answering (the store drops
    it on the next write, and the gate stops matching it the moment the clock
    passes the deadline).  ``None`` means a grant is permanent -- the explicit
    "always" of the pre-TTL shape, which every v1 file already is.
    """

    model_config = ConfigDict(extra="ignore")

    grant_ttl_seconds: float | None = 86_400.0


class StaticCheckConfig(BaseModel):
    """The commands the harness runs itself when the search ladder reaches L2 (B1).

    Commands are *declared*, never inferred: measured on this repository,
    ``mypy .`` reports 271 errors (it does not exclude ``scripts/``) while
    ``mypy ai_coding`` -- what CI runs -- reports none.  No heuristic tells those
    apart, so a project that wants mypy says so here.
    """

    model_config = ConfigDict(extra="ignore")

    #: Shell commands, run in the workspace (``.venv/bin`` is first on ``PATH``).
    #: Empty disables the feature; the nudge's own text still tells the model what
    #: to run by hand.
    commands: list[str] = Field(default_factory=lambda: ["ruff check ."])
    timeout_seconds: float = 20.0
    #: Cap on what the whole block may add to the prompt.
    max_chars: int = 2000


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto_process_tool_results: bool = True
    #: Per-turn tool-call ceiling.  Raised from 10 to 100 in r22: 10 ended real
    #: work mid-step ("budget exhausted, type continue"), which is not how the
    #: assistants this project is measured against behave.  Claude Code, Codex
    #: and OpenCode have no default iteration cap at all -- their loop ends when
    #: the model stops calling tools, and auto-compaction carries long turns.
    #: 100 keeps a hard bound for a runaway loop while clearing any real task;
    #: the clock, token budget and ladder guards are still the safety net.
    max_tool_iterations: int = 100
    #: B1/B2: consecutive look-only calls before the `[search-gate]` ladder speaks.
    #: 0 disables it.  Clamped at 0 because a negative value would make every
    #: iteration fire a nudge and leave the model no step to act in.
    search_streak_threshold: int = Field(default=4, ge=0)
    #: I1/R-7: consecutive look-only calls that force the turn to close.  The
    #: ladder's L1/L2 are advice; this is the rung that stops a survey from
    #: spending the whole budget.  **Default 0 (off) after measurement**: in
    #: eval-08 the streak fired on 18 novel-only reads of a design question
    #: (0 stale, 18 new findings) and truncated a turn that used to answer in
    #: full, while the pathology it targets (61 calls / 900s / no conclusion)
    #: interleaves ``bash`` and never builds a streak at all.  The stop that
    #: survives by default is the evidence one below -- same finding twice.
    search_streak_l3: int = Field(default=0, ge=0)
    #: B1: what the harness runs itself when the turn stalls in look-only mode.
    static_check: StaticCheckConfig = Field(default_factory=StaticCheckConfig)
    max_concurrent_subagents: int = 6
    subagent_worktree_isolation: bool = True
    max_context_tokens: int = 0
    # R3b: demoted from "trim every turn at 50 messages" to "fallback cap used
    # when the token budget is exceeded" (docs/r3-analysis.md 1.15).
    max_messages: int = 400
    snip_keep_head: int = 3
    # I2/D10: the first-compaction path keeps a longer verbatim tail.  Measured
    # in eval-08: a 48k window was handing the model 2-3k, so every iteration
    # re-read what the previous one had just learned.
    snip_keep_tail: int = 30
    keep_recent_tool_results: int = 8
    per_result_persist_bytes: int = 30_000
    # R7b: what an elided tool result keeps on each side, and what all of them may
    # keep together.  The body itself is written to transcripts/elided/ first, so
    # a window of 0 is still safe (it only costs the model a read).
    # I2/D10: 200 chars was "the content is gone"; the model's only recourse was
    # to read the file again.  1200 keeps the shape of a diff/hunk.
    l2_window_chars: int = 1_200
    l2_window_budget: int = 12_000
    #: I2/D1 branch C: a delta smaller than this is carried verbatim into the
    #: request instead of paying for a summary call.  Keeps "compress" from
    #: firing on every iteration once a checkpoint exists.  Must be > 0: zero
    #: would silently disable branch C and restore one summary per iteration.
    summary_delta_tokens: int = Field(default=4_000, gt=0)
    # P3-2 refactor: the L4 tail is token-budgeted (OpenCode parity), not
    # counted.  Effective budget is min(l4_tail_tokens, window // 4): the
    # tail alone can never approach the post-compaction paging target.
    # The default 15k matches OpenCode's keep; backtracked to a user
    # boundary, never empty.
    l4_tail_tokens: int = 15_000
    # ---- R1a orchestration (docs/r1-analysis.md) --------------------------
    # How a turn is allowed to end.  "todo_aware" gates the stop on open items.
    stop_policy: Literal["natural", "todo_aware", "strict"] = "todo_aware"
    #: How many "your list still has open items" nudges one turn may receive.
    #: Raised from 2 to 10 in r22: a model that closes one item and stops was
    #: handing the operator a dead turn every few minutes.  The cap still keeps
    #: a list nobody is working on from nudging forever.
    max_todo_nudges: int = 10
    # R9a: refuse to start/finish an item whose depends_on is unmet, and refuse
    # to create a dependency on a missing or self id.  Off = the R1 behaviour,
    # which exists for legacy lists only (docs/r9-analysis.md D-R9-3).
    enforce_dependencies: bool = True
    # Where the task list is injected: a volatile trailing message (cache
    # friendly) or appended to the system prompt (survives compaction).
    task_injection: Literal["message", "system"] = "message"
    # M1b: the agent a *new* session starts in.  Only a primary agent may be
    # the default; anything else falls back to build when the catalog is
    # loaded.  An existing session keeps the agent it was switched to.
    default_agent: str = "build"
    # M2 T6: the strict tier of the plan posture.  Off (the default) the
    # plan agent refuses the tools that change the world -- exactly what the
    # read-only overlay refused.  On, it also refuses the tools that change
    # the conversation: asking the human, writing memory, starting a goal.
    # It never refuses the two plan exits; a plan mode with no way to submit
    # a plan is a plan mode that never ends.
    plan_strict_readonly: bool = False
    max_concurrent_tools: int = 4
    # R7c: how long a human gets to answer an approval prompt.  0 means "only the
    # turn clock bounds it" — a slow human is not a refusal, but a turn with a
    # clock must still be able to end.
    approval_timeout_seconds: float = 0.0
    subagent_timeout_seconds: int = 1200
    # R7e-4: how much of the parent a sub-agent starts with.  "full" is the R1a-5
    # behaviour (the parent's request view + system prompt); "task_only" gives it a
    # clean window and the composition root's base instructions instead, which is
    # what Anthropic's sub-agent guidance describes - and it means the parent MUST
    # put every fact the child needs into the task text (docs/r7e-analysis.md 3.4).
    subagent_context_mode: Literal["full", "task_only"] = "full"
    # R7e-3, recalibrated by r22: warn once per loop when the MCP part of the
    # tool surface costs more than this.  0 disables.
    #
    # It used to gate the *whole* surface at 12,000, a number picked when the
    # built-in tools serialised to 4,280 chars.  They are 12,760 now, so the
    # ceiling sat below the floor and every session warned about a cost the user
    # cannot change.  The built-in tools are not configurable; MCP servers are.
    mcp_chars_warn: int = 2_000
    # R7e-2: what one turn may absorb from a user who keeps typing (docs §12 Q6).
    steering_max_messages: int = 5
    steering_max_chars: int = 2_000
    # R2a: wall-clock ceiling for one user turn.  Iteration counts cannot bound a
    # call that never returns, so any stuck step or tool must still end the turn.
    # Includes time spent waiting for a human approval prompt; 0 disables it.
    # Raised from 600s to 1800s in r22: a plan step that writes a package can
    # legitimately take longer than ten minutes, and the measured hotel session
    # was cut by this clock as well as by the iteration cap.
    turn_timeout_seconds: float = 1800.0
    # R1-1: iteration ceiling for one plan-runner step.  Raised from 8 to 60 in
    # r22: 8 was measured on "simple steps close in 5-7 iterations", but a step
    # that writes several files (a models package, a router set) needs far more
    # than 7, and the measured hotel session died at 8/8 with four files still
    # unwritten ("本轮预算有限").  60 clears a real step; the clock, acceptance
    # gate and runner's one repair pass are still the guards.
    plan_step_max_iterations: int = 60
    # C3: when a normal turn's iteration budget dies with todo items still
    # open, the loop injects a continuation prompt and starts a fresh round
    # instead of stopping.  0 disables.  The cap keeps a model that never
    # closes its items from running forever.  Fires regardless of stop_policy.
    max_auto_continuations: int = 3
    # R1-4: turn ceiling for one /plan-run.  0 (the r22 default) means "no run
    # cap": the measured hotel plan had 23 steps and the old 20-turn ceiling
    # stopped it with steps 21-23 untouched ("budget: plan turn budget
    # exhausted").  Each step is already bounded, and the run ends on
    # done/deadlock; an operator who wants a ceiling can still set one.
    plan_max_step_turns: int = 0
    # ---- r24/C: wave execution ---------------------------------------
    #: Which plan executor `/plan run` uses.  ``wave`` (default) runs items in
    #: continuous waves; ``legacy`` keeps the one-short-session-per-step runner
    #: exactly as it was (the escape hatch).
    plan_orchestrator: Literal["wave", "legacy"] = "wave"
    #: Open items one wave may hold.  4 is a context budget, not a correctness
    #: rule: a heavy proof command or a research/verify item always closes the
    #: wave earlier (see core/wave_planner.py).
    plan_wave_size: int = 4
    #: Provider-reported tokens one wave may spend; 0 disables.  The executor
    #: also clamps each turn's ``max_turn_tokens`` to what remains, so a single
    #: turn cannot overshoot the whole wave budget.
    plan_wave_token_budget: int = 300_000
    #: Wall-clock ceiling for one wave, checked between turns; 0 disables.
    plan_wave_wall_seconds: float = 1_200.0
    #: Turns one item may spend before it is marked blocked (includes the
    #: acceptance repair turn).
    plan_max_item_attempts: int = 2
    #: Waves one run may open; 0 disables (the plan itself bounds the run).
    plan_max_waves: int = 0
    #: Episode-wide token ceiling for one plan run, counted across waves;
    #: 0 disables.  Kept separate from ``goal_token_budget`` on purpose.
    plan_token_budget: int = 0
    #: Wave rewrites one run may perform (deadlock / needs_replan); 0 disables
    #: replanning.  Reserved for C3; the wave runner reads it once replan lands.
    plan_max_replans: int = 2
    # D-4: episode-wide round ceiling for one armed goal; 0 disables.  Was a
    # phantom until 2026-10-17: the runner read it duck-typed while this class
    # had no such field, and `extra="ignore"` swallowed any yaml key for it.
    goal_max_rounds: int = 20
    # D-4 v2: episode-wide token ceiling for one armed goal, counted from the
    # provider's usage (GoalRunner.after_round).  0 disables.  The round cap
    # already bounds the worst case; this bounds the one the user actually
    # pays for.  Unknown usage is not charged (fail-open).
    goal_token_budget: int = 0
    max_result_data_bytes: int = 8_000
    max_subagent_conclusion_chars: int = 4_000
    # Ask the endpoint to emit a final usage frame (stream_options.include_usage).
    # Off is available for OpenAI-compatible servers that reject the parameter.
    stream_usage: bool = True
    # Below this many messages the loop checkpoints the session after every
    # iteration; above it, only every LONG_SESSION_STRIDE iterations (plus the
    # mandatory final save) to avoid rewriting a large session many times.
    persist_checkpoint_messages: int = 200
    # R3b: compaction is budget-triggered, not count-triggered.  Measured on the
    # old defaults, the message-count rule trimmed at turn 26 while 5.2% of the
    # token budget was used, rewriting the middle of the history every turn and
    # destroying prompt-cache hits.  The gate is the OpenCode ceiling —
    # window minus max(10%, 16k once the window is >= 32k) — and the result
    # pages down to compress_target_ratio of the budget.
    compress_target_ratio: float = 0.5
    # OpenCode's settings.auto, our shape: False stops the proactive (budget
    # driven) compaction only.  /compact and the overflow rescue are outside
    # this gate — a turn must still finish even with proactive compaction off.
    auto_compact: bool = True
    # E-5: per-skill policy for the model's skill tool (allow | ask | deny).
    # deny also hides the skill from the model's catalog and withholds
    # @mention injection; ask routes the model's own reads through the
    # approval panel. A typo is a load error, not a silent allow.
    skill_permissions: dict[str, Literal["allow", "ask", "deny"]] = Field(default_factory=dict)
    # r17/T3: web search (Tavily).  Empty key = the tool stays registered but
    # answers with how to configure it (a vanishing tool cannot be diagnosed).
    websearch_api_key: str = ""
    websearch_max_results: int = 5
    # R4b: cumulative token ceiling for one turn (it counts every re-sent context,
    # so it is a spend cap, not a window size).  0 disables the gate.
    turn_token_budget: int = 0
    # R6a: the volatile <working-memory> ring — at most this many one-line turn
    # digests, rendered into this many characters (measured ≈230 estimated tokens,
    # 0.5% of the budget: docs/r6-analysis.md 1.6).  Injected only when the
    # request's history was actually rewritten, so an ordinary turn pays nothing.
    working_memory_keep: int = 12
    working_memory_chars: int = 1200
    # R8a: how many characters one model-written checkpoint line may keep.  It
    # replaces the digest line's "what happened" sentence; the ring pays for it
    # only after a compaction (measured: 104 chars fixed overhead, 1096 per ring,
    # docs/r8-analysis.md 1.1 / 3.1).
    working_memory_note_chars: int = 120
    # R6b: the escalation ladder (docs/r6-analysis.md 2.2).  A streak of failed
    # iterations buys one strategy nudge; a turn in which nothing succeeds stops
    # early.  0 disables either half.
    failed_streak_threshold: int = 2
    max_strategy_nudges: int = 1
    stall_iterations: int = 3
    #: M1: turns between background skill reviews (0=disabled).  After every N
    #: turns, a background task reviews the conversation delta and optionally
    #: creates a new skill in the skills directory.
    skill_review_interval: int = 10


class TracingConfig(BaseModel):
    """R4a observability settings; every field defaults to "on but cheap"."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    dir: str = ""  # empty = <workspace>/.ai-coding/traces
    # previews keeps <=500-char payloads; full keeps whatever the event carries.
    capture: Literal["previews", "full"] = "previews"
    max_turn_bytes: int = 2_000_000
    retention_days: int = 14
    redact: bool = True


class GuardConfig(BaseModel):
    """R4b gatekeeper settings."""

    model_config = ConfigDict(extra="ignore")

    duplicate_threshold: int = 2  # 0 disables the duplicate-call guard
    bash_deny: list[str] = Field(default_factory=list)
    bash_confirm: list[str] = Field(default_factory=list)
    strict_paths: bool = False
    redact_history: bool = False


class HookEventConfig(BaseModel):
    """One configured hook (R4c)."""

    model_config = ConfigDict(extra="ignore")

    point: str = ""
    match: dict[str, str] = Field(default_factory=dict)
    kind: Literal["python", "command"] = "command"
    command: list[str] = Field(default_factory=list)
    module: str = ""
    entrypoint: str = "run"
    capability: Literal["observe", "inject", "block", "rewrite"] = "observe"
    persist: bool = False
    raw: bool = False


class HooksConfig(BaseModel):
    """R4c hook settings; disabled by default (P8)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    timeout_seconds: float = 10.0
    plugin_dir: str = ""
    events: list[HookEventConfig] = Field(default_factory=list)


class EmbeddingsConfig(BaseModel):
    """Optional semantic second opinion for memory recall (R10d).

    Everything defaults to off or empty: a config without this block, or with
    it present but disabled, behaves exactly as if the tier did not exist.
    ``provider`` names a key in ``models`` so the tier reuses that model's
    ``base_url`` and ``api_key`` instead of introducing a second credential.
    ``dimensions=0`` means "use whatever the endpoint returns".
    ``min_cosine`` is only the OR branch of the absolute relevance gate, never
    a ranking weight — measured 0.60 on text-embedding-v4 (positive min 0.44 /
    negative max 0.66 overlap, so no threshold separates; 0.60 buys 6/46
    paraphrase for +0.02 FP@5).  A different embedding model needs recalibration.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    provider: str = ""
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    min_cosine: float = 0.60
    cache_path: str = ".memory/embeddings.json"
    timeout_seconds: float = 10.0


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    auto_extract: bool = True
    consolidate_threshold: int = 10
    consolidate_every_turns: int = 5
    max_index_entries: int = 200
    # R3: how many detail hits an explicit memory_search may return.
    max_per_turn_injections: int = 3
    recall_threshold: float = 0.25
    # R10b: 0.05 measured as no gate at all — recency + importance alone reach
    # 0.35 and clear the 0.25 threshold, so an irrelevant but fresh and
    # important note was injected (docs/r10-analysis.md 1.1). A config that
    # spells out 0.05 keeps its old behaviour.
    recall_min_relevance: float = 0.25
    # R10b: also keep only what is within this share of the best score in the
    # same query. Measured on our own corpus this is what strips the junk tail:
    # FP@5 0.52 -> 0.35, and removing the band on its own puts it back at 0.40.
    # The band is doing that work, not the BM25 ranker — swapping BM25 for plain
    # coverage measured 0.32 (a wash, see task-2-report.md), so do not credit
    # recall_relative_band to the ranker.
    recall_relative_band: float = 0.55
    # R11d: how the lexical and semantic opinions become one rank term.
    # `blend` is today's behaviour and the measured better one (RRF measured
    # paraphrase R@3 0.41 -> 0.26 on our corpus), so this is a switch for
    # re-measuring, not a default change. Literal: an unknown rule fails at
    # load rather than mid-recall, where nothing would report it.
    recall_fusion: Literal["blend", "rrf"] = "blend"
    # R11d: RRF's damping constant (paper value). Not in config.example.yaml on
    # purpose — it is a re-measuring knob, not a user-facing one.
    rrf_k: int = 60
    recency_half_life_days: float = 14.0
    # R11b: wall-clock budget for one recall (rank + semantic tier + probe).
    # Over budget the hits still return but the optional `touch` write is
    # skipped and the call is flagged; 0 or negative disables the budget.
    recall_timeout_seconds: float = 2.0
    # Budgets for the two injection surfaces (the tail constraint block and the
    # on-demand tool answer), in characters.
    index_max_chars: int = 600
    detail_max_chars: int = 1200
    harvest_max_messages: int = 12
    dedupe_similarity: float = 0.80
    pinned_kinds: list[str] = Field(default_factory=lambda: ["decision"])
    # R11a: the write-side gate. `extraction_enabled=False` stops every write
    # while recall keeps working; `extraction_extractors` (None = all) selects
    # which write channel runs: "harvest" and/or "memory_write".
    extraction_enabled: bool = True
    extraction_extractors: list[str] | None = None
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)


class MCPTimeout(BaseModel):
    """Three budgets instead of one, as in OpenCode v2 (schema/src/mcp.ts).

    A single ``connection_timeout`` conflated two very different jobs: waiting
    for ``npx`` to cold start (slow, measured ~27s, docs/r7-analysis.md 3.4) and
    waiting for a hung ``tools/list`` (a dead server, which must not hide behind
    a budget sized for cold starts).
    """

    # Milliseconds, like every v2 timeout.
    startup: int = 60_000
    catalog: int = 30_000
    #: One remote call.  ``None`` leaves the turn deadline as the only bound.
    execution: int | None = None


class MCPToolRules(BaseModel):
    """Per-tool authorization for one server, by *remote* tool name (M2).

    Deny wins, and a standing "always" the operator gave can never reopen a
    denied tool — the same ordering the bash hard-deny list uses.  Visibility is
    a separate question: a denied tool still appears in the catalog, so the
    refusal is legible instead of an unexplained absence.
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class MCPOAuthConfig(BaseModel):
    """How the operator's browser comes back (M4, remote servers only).

    Deliberately two knobs.  ``client_id``/``client_secret`` are absent because
    dynamic client registration (RFC 7591) is what the SDK does when the
    authorization server advertises it, and most hosted MCP servers do; a
    pre-registered client is a separate request with a real server behind it,
    not a speculative field.
    """

    #: The local port the authorization redirect returns to.  19876 is
    #: OpenCode's default for the same purpose.
    callback_port: int = 19876
    #: Empty means "ask for whatever the server advertises" — a scope guessed
    #: here can only make a server refuse the request.
    scope: str = ""


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    #: ``local`` spawns a command; ``remote`` speaks Streamable HTTP (M4).
    #: The two shapes are disjoint on purpose: a local server has no URL and a
    #: remote one has no command, so one missing field is the whole check.
    type: Literal["local", "remote"] = "local"
    #: argv as one list, OpenCode-v2 shape, so a pasted OpenCode config works.
    #: (Was ``command: str`` + ``args: list[str]``.)
    command: list[str] = Field(default_factory=list)
    #: The endpoint for a remote server (M4).
    url: str = ""
    #: Sent with every request, e.g. an API key a proxy wants (M4).
    headers: dict[str, str] = Field(default_factory=dict)
    #: Present on a remote server that needs the operator to sign in.
    oauth: MCPOAuthConfig | None = None
    # Extra environment variables for the child process (merged over a safe
    # default set) and its working directory (R2d).  (Was ``env``.)
    environment: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    # On unless it says otherwise (v2 ``disabled``, was an opt-in ``enabled``).
    disabled: bool = False
    timeout: MCPTimeout = Field(default_factory=MCPTimeout)
    # R7d: only register these remote tool names (empty = all of them).  Every
    # schema is re-sent on every step, so a 13-tool server can double the tool
    # surface: measured 10,062 of 14,342 characters (docs/r7-analysis.md 1.2).
    tools: list[str] = Field(default_factory=list)
    # R7d: this server's tools skip the approval prompt.  Declared by the user in
    # their own config, and off by default, so an unconfigured run is unchanged.
    auto_approve: bool = False
    # M2: per-tool rules, beside the whitelist they resemble.
    permissions: MCPToolRules = Field(default_factory=MCPToolRules)


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    servers: list[MCPServerConfig] = Field(default_factory=list)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: dict[str, ModelConfig] = Field(default_factory=dict)
    default_model: str = ""
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    # M-D1: the OS-level process sandbox.  Policy and mechanism are separate;
    # the mechanism is built by the composition root from this.
    process_sandbox: ProcessSandboxConfig = Field(default_factory=ProcessSandboxConfig)
    # M-D3: the standing-grant lifetime.
    security: SecurityConfig = Field(default_factory=SecurityConfig)


def _coerce_fallbacks(raw: Any) -> tuple[str, ...]:
    """Normalise a chain into a tuple of model keys.

    A bare ``fallback_models: backup`` parses as a string, and iterating a
    string yields its characters -- so without this the chain would become six
    one-letter model names and only fail later, on the first outage.
    """
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(str(name) for name in raw)


def _number(raw: Mapping[str, Any], key: str, default: float) -> float:
    """``raw[key]``, or ``default`` when it is absent **or explicitly null**.

    Not ``raw.get(key) or default``: ``temperature: 0`` is a value a user means,
    and `or` would quietly turn it into the default.
    """
    value = raw.get(key)
    return default if value is None else float(value)


def _coerce_model(
    raw: dict[str, Any], *, env: Mapping[str, str] | None = None
) -> ModelConfig:
    """Build a domain ``ModelConfig`` accepting snake_case or legacy camelCase keys.

    This is the one place a YAML entry becomes a usable connection (M-H2): the
    provider fills a missing ``base_url`` and decides which environment variable
    carries the key.  ``env`` is injectable so a test never depends on the
    machine it runs on.
    """
    from ai_coding.config.secrets import resolve_api_key
    from ai_coding.providers import GENERIC, default_base_url, provider_id_for

    name = str(raw.get("name") or "")
    # `or ""` rather than a default: YAML `api_key:` with no value parses as
    # None, and `str(None)` would send the literal "None" as the key.
    base_url = str(raw.get("base_url") or raw.get("baseURL") or "").strip()
    explicit = str(raw.get("provider") or "").strip()
    try:
        provider_id = provider_id_for(base_url, explicit=explicit)
        base_url = base_url or default_base_url(provider_id)
        api_key = resolve_api_key(str(raw.get("api_key") or ""), provider_id, env=env)
    except ValueError as exc:
        # Every refusal here is about *this* model, and a multi-model config with
        # one bad entry would otherwise say only "unknown provider 'deapsek'".
        raise type(exc)(f"model {name or '?'!r}: {exc}") from exc
    if explicit and base_url:
        # `provider` wins unconditionally, so a config naming one provider and an
        # endpoint belonging to another would send A's key to B with no warning.
        inferred = provider_id_for(base_url)
        if inferred not in ("", GENERIC) and inferred != provider_id:
            _log.warning(
                "model %r: provider %r but base_url belongs to %r; the key will be "
                "read for %r and sent to %s",
                name,
                explicit,
                inferred,
                provider_id,
                base_url,
            )
    if not base_url:
        _log.warning(
            "model %r has no base_url and its provider has no default; the SDK "
            "will fall back to OPENAI_BASE_URL",
            name,
        )
    return ModelConfig(
        name=name,
        base_url=base_url,
        api_key=api_key,
        streaming=bool(raw.get("streaming", True)),
        max_tokens=int(_number(raw, "max_tokens", 4096)),
        temperature=_number(raw, "temperature", 0.7),
        top_p=_number(raw, "top_p", 0.9),
        timeout=int(_number(raw, "timeout", 60)),
        # P0-1: a negative budget is nonsense, so clamp rather than trust the file.
        max_retries=max(0, int(raw.get("max_retries", 3) or 0)),
        retry_base_seconds=max(0.0, float(raw.get("retry_base_seconds", 2.0) or 0.0)),
        price_input_per_1k=float(raw.get("price_input_per_1k", 0.0) or 0.0),
        price_output_per_1k=float(raw.get("price_output_per_1k", 0.0) or 0.0),
        price_cached_per_1k=float(raw.get("price_cached_per_1k", 0.0) or 0.0),
        fallback_models=_coerce_fallbacks(raw.get("fallback_models")),
        context_window=max(0, int(raw.get("context_window", 0) or 0)),
    )


#: What a model that has not declared a window gets.  It was a hardcoded guess
#: that a 256k model inherited as 48k, so the soft water mark sat at 15% of real
#: capacity.  Unknown now means "assume the small one" rather than "assume the
#: large one": a too-low guess costs an unnecessary compaction, a too-high guess
#: costs a failed request.
DEFAULT_CONTEXT_TOKENS = 48_000


def resolve_context_window(config: Any) -> int:
    """The window the compaction policy and the meter must both agree on.

    Three sources, in order of authority: an explicit ``ai.max_context_tokens``,
    then the active model's own ``context_window``, then the conservative
    default.  The config stays the override (per decision) but stops being the
    only place the number can come from, so switching models cannot silently
    leave the previous model's window in force.
    """
    configured = int(getattr(getattr(config, "ai", None), "max_context_tokens", 0) or 0)
    if configured > 0:
        return configured
    default_model = str(getattr(config, "default_model", "") or "")
    models = getattr(config, "models", None) or {}
    model = models.get(default_model)
    declared = int(getattr(model, "context_window", 0) or 0)
    return declared if declared > 0 else DEFAULT_CONTEXT_TOKENS
