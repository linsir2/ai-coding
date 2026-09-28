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
    # R6c: the model asking a human.  `timeout_seconds` is how long it may wait for
    # an answer (the tool adds 15s of slack for the dispatcher, like bash/subAgent).
    # Only registered where somebody can actually answer — see tools/builder.py.
    ask_user: ToolConfig = Field(
        default_factory=lambda: ToolConfig(timeout_seconds=300)
    )


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto_process_tool_results: bool = True
    max_tool_iterations: int = 10
    max_concurrent_subagents: int = 3
    subagent_worktree_isolation: bool = True
    max_context_tokens: int = 48_000
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
    l4_keep_tail: int = 6
    # ---- R1a orchestration (docs/r1-analysis.md) --------------------------
    # How a turn is allowed to end.  "todo_aware" gates the stop on open items.
    stop_policy: Literal["natural", "todo_aware", "strict"] = "todo_aware"
    max_todo_nudges: int = 2
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
    # R7e-3: warn once per loop when the serialised tool schemas cost more than
    # this.  0 disables.  Advisory only: the request is byte-identical either way
    # (measured: 4,280 chars without MCP, 13,301 with a 13-tool server).
    tools_chars_warn: int = 12_000
    # R7e-2: what one turn may absorb from a user who keeps typing (docs §12 Q6).
    steering_max_messages: int = 5
    steering_max_chars: int = 2_000
    # R2a: wall-clock ceiling for one user turn.  Iteration counts cannot bound a
    # call that never returns, so any stuck step or tool must still end the turn.
    # Includes time spent waiting for a human approval prompt; 0 disables it.
    turn_timeout_seconds: float = 600.0
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
    # destroying prompt-cache hits.  Compact at 0.8 of the budget, down to 0.5.
    compress_soft_ratio: float = 0.8
    compress_target_ratio: float = 0.5
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
    recall_min_relevance: float = 0.05
    recency_half_life_days: float = 14.0
    # Budgets for the two injection surfaces (the tail constraint block and the
    # on-demand tool answer), in characters.
    index_max_chars: int = 600
    detail_max_chars: int = 1200
    harvest_max_messages: int = 12
    dedupe_similarity: float = 0.80
    pinned_kinds: list[str] = Field(default_factory=lambda: ["decision"])


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    command: str = ""
    enabled: bool = False
    args: list[str] = Field(default_factory=list)
    # Extra environment variables for the child process (merged over a safe
    # default set) and its working directory (R2d).
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    # R7d: only register these remote tool names (empty = all of them).  Every
    # schema is re-sent on every step, so a 13-tool server can double the tool
    # surface: measured 10,062 of 14,342 characters (docs/r7-analysis.md 1.2).
    tools: list[str] = Field(default_factory=list)
    # R7d: this server's tools skip the approval prompt.  Declared by the user in
    # their own config, and off by default, so an unconfigured run is unchanged.
    auto_approve: bool = False


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    auto_discover: bool = True
    # R7d: npx-class servers measured ~27s to cold start, which left 3s against the
    # old 30s default (docs/features.md, R4 measurements).
    connection_timeout: int = 60
    servers: list[MCPServerConfig] = Field(default_factory=list)


class UIConfig(BaseModel):
    """Terminal-facing switches (R7e-2)."""

    model_config = ConfigDict(extra="ignore")

    #: Show a steering prompt while a turn is running.  Off = today's quiet REPL.
    steering: bool = True


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
    ui: UIConfig = Field(default_factory=UIConfig)


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
        price_input_per_1k=float(raw.get("price_input_per_1k", 0.0) or 0.0),
        price_output_per_1k=float(raw.get("price_output_per_1k", 0.0) or 0.0),
    )
