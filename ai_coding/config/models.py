"""Pydantic configuration models with defaults aligned to the original Java values."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_coding.domain.model_config import ModelConfig


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


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto_process_tool_results: bool = True
    max_tool_iterations: int = 10
    max_concurrent_subagents: int = 3
    subagent_worktree_isolation: bool = True
    max_context_tokens: int = 0
    # R3b: demoted from "trim every turn at 50 messages" to "fallback cap used
    # when the token budget is exceeded" (docs/r3-analysis.md 1.15).
    max_messages: int = 400
    snip_keep_head: int = 3
    snip_keep_tail: int = 20
    keep_recent_tool_results: int = 3
    per_result_persist_bytes: int = 30_000
    # R7b: what an elided tool result keeps on each side, and what all of them may
    # keep together.  The body itself is written to transcripts/elided/ first, so
    # a window of 0 is still safe (it only costs the model a read).
    l2_window_chars: int = 200
    l2_window_budget: int = 4_000
    # P3-2 refactor: the L4 tail is token-budgeted (OpenCode parity), not
    # counted.  Effective budget is min(l4_tail_tokens, window // 4): the
    # tail alone can never approach the post-compaction paging target.
    # The default 15k matches OpenCode's keep; backtracked to a user
    # boundary, never empty.
    l4_tail_tokens: int = 15_000
    # ---- R1a orchestration (docs/r1-analysis.md) --------------------------
    # How a turn is allowed to end.  "todo_aware" gates the stop on open items.
    stop_policy: Literal["natural", "todo_aware", "strict"] = "todo_aware"
    max_todo_nudges: int = 2
    # R9a: refuse to start/finish an item whose depends_on is unmet, and refuse
    # to create a dependency on a missing or self id.  Off = the R1 behaviour,
    # which exists for legacy lists only (docs/r9-analysis.md D-R9-3).
    enforce_dependencies: bool = True
    # Where the task list is injected: a volatile trailing message (cache
    # friendly) or appended to the system prompt (survives compaction).
    task_injection: Literal["message", "system"] = "message"
    max_concurrent_tools: int = 4
    # R7c: how long a human gets to answer an approval prompt.  0 means "only the
    # turn clock bounds it" — a slow human is not a refusal, but a turn with a
    # clock must still be able to end.
    approval_timeout_seconds: float = 0.0
    subagent_timeout_seconds: int = 600
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
    turn_timeout_seconds: float = 600.0
    # R1-1: iteration ceiling for one plan-runner step.  Measured: simple steps
    # close in 5-7 iterations; 8 leaves headroom without funding a wander.
    plan_step_max_iterations: int = 8
    # C3: when a normal turn's iteration budget dies with todo items still
    # open, the loop injects a continuation prompt and starts a fresh round
    # instead of stopping.  0 disables.  The cap keeps a model that never
    # closes its items from running forever.  Fires regardless of stop_policy.
    max_auto_continuations: int = 3
    # R1-4: turn ceiling for one /plan-run.  Steps are bounded but the run was
    # not — a plan that keeps "completing" spent steps x (1-2) turns with no
    # cap, and each process_input starts a FRESH wall-clock deadline
    # (AgentLoop._begin_turn), so wall-clock was unbounded too.  One knob
    # bounds all three: turns directly, tokens via turns x step-iterations x
    # max_tokens, wall-clock via turns x turn_timeout_seconds.  0 disables.
    plan_max_step_turns: int = 20
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


def _coerce_model(raw: dict[str, Any]) -> ModelConfig:
    """Build a domain ``ModelConfig`` accepting snake_case or legacy camelCase keys."""
    return ModelConfig(
        name=str(raw.get("name", "")),
        base_url=str(raw.get("base_url", raw.get("baseURL", ""))),
        api_key=str(raw.get("api_key", "")),
        streaming=bool(raw.get("streaming", True)),
        max_tokens=int(raw.get("max_tokens", 4096)),
        temperature=float(raw.get("temperature", 0.7)),
        top_p=float(raw.get("top_p", 0.9)),
        timeout=int(raw.get("timeout", 60)),
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
