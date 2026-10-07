I have all the source I need. Here are the facts.

---

# Hermes Agent — Orchestration Layer

Repo root: `/home/linsir365/.hermes/hermes-agent`

**Upfront correction to the premise:** there is no `turn` package, and no modules named `turn_preflight` / `turn_api_call` / `turn_tool_round`, and no `INLINE_TOOL_EXECUTOR`. Verified by exhaustive grep across the repo (excluding `venv/`, `node_modules/`):

```
grep -rn "turn_preflight\|turn_api_call\|turn_tool_round" --include=*.py --include=*.md .
→ (no output)
grep -rn "INLINE_TOOL_EXECUTOR" --include=*.py .
→ (no output)
```

The whole turn loop is one method body: `AIAgent.run_conversation` at `run_agent.py:7107`–`9645` (~2,540 lines). "Phases" are inline `if`/`try` blocks, not modules.

---

## 1. THE TURN LOOP

### Loop body

Condition (`run_agent.py:7420`):

```python
7420: while api_call_count < self.max_iterations and self.iteration_budget.remaining > 0:
```

Head of body (`run_agent.py:7421`–`7439`):

```python
7421:     # Reset per-turn checkpoint dedup so each iteration can take one snapshot
7422:     self._checkpoint_mgr.new_turn()
7423: 
7424:     # Check for interrupt request (e.g., user sent new message)
7425:     if self._interrupt_requested:
7426:         interrupted = True
7427:         _turn_exit_reason = "interrupted_by_user"
7428:         if not self.quiet_mode:
7429:             self._safe_print("\n⚡ Breaking out of tool loop due to interrupt...")
7430:         break
7431: 
7432:     api_call_count += 1
7433:     self._api_call_count = api_call_count
7434:     self._touch_activity(f"starting API call #{api_call_count}")
7435:     if not self.iteration_budget.consume():
7436:         _turn_exit_reason = "budget_exhausted"
```

Tail of loop — the two arms (`run_agent.py:9164`–`9253` tool path, `9255`–`9409` text path):

```python
9164:     self._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)
...
9236:     if self.compression_enabled and _compressor.should_compress(_real_tokens):
9237:         self._safe_print("  ⟳ compacting context…")
9238:         messages, active_system_prompt = self._compress_context(...)
...
9252:     # Continue loop for next response
9253:     continue
```

```python
9255:     else:
9256:         # No tool calls - this is the final response
9257:         final_response = assistant_message.content or ""
...
9406:         _turn_exit_reason = f"text_response(finish_reason={finish_reason})"
9407:         if not self.quiet_mode:
9408:             self._safe_print(f"🎉 Conversation completed after {api_call_count} OpenAI-compatible API call(s)")
9409:         break
```

### Iteration unit

One iteration = **one model API call** (`api_call_count += 1` at 7432), optionally followed by one tool round. Retries are nested *inside* an iteration (`while retry_count < max_retries` at `run_agent.py:7610`, `max_retries = 3` at 7596) and do **not** increment `api_call_count`.

### Default max budget

`max_iterations: int = 90` — constructor default (`run_agent.py:475`):

```python
475:         max_iterations: int = 90,  # Default tool-calling iterations (shared with subagents)
```

Config default `"max_turns": 90` (`hermes_cli/config.py:252`). CLI resolution order (`cli.py:1590`–`1600`):

```python
1590:         # Max turns priority: CLI arg > config file > env var > default
1591:         if max_turns is not None:  # CLI arg was explicitly set
1593:         elif CLI_CONFIG["agent"].get("max_turns"):
1595:         elif CLI_CONFIG.get("max_turns"):  # Backwards compat: root-level max_turns
1597:         elif os.getenv("HERMES_MAX_ITERATIONS"):
1600:             self.max_turns = 90
```

There is **no token-budget or cost budget** on the loop. `IterationBudget` (`run_agent.py:171`–`212`) is a plain thread-safe counter with `consume()`/`refund()`; reset per turn at `run_agent.py:7187`. Refund only for pure-`execute_code` turns (`run_agent.py:9177`–`9179`) and compression restarts (`8788`).

Subagents get a **separate** budget, default 50 (`hermes_cli/config.py:514`, `tools/delegate_tool.py:39`):

```python
37: MAX_CONCURRENT_CHILDREN = 3
38: MAX_DEPTH = 2  # parent (0) -> child (1) -> grandchild rejected (2)
39: DEFAULT_MAX_ITERATIONS = 50
```

### Exit conditions

| Condition | Location |
|---|---|
| `_interrupt_requested` at loop top | `7425` |
| `iteration_budget.consume()` returns False | `7435` |
| `interrupted` set during API call | `8782`–`8784` |
| `response is None` after all retries | `8802`–`8806` |
| Final text response (`finish_reason`) | `9406`–`9409` |
| Error near max iterations (`>= max_iterations - 1`) | `9455`–`9461` |
| Post-loop: budget/max exhausted → summary call | `9463`–`9470` |

`run_agent.py:9463`–`9473`:

```python
9463: if final_response is None and (
9464:     api_call_count >= self.max_iterations
9465:     or self.iteration_budget.remaining <= 0
9466: ):
9467:     _turn_exit_reason = f"max_iterations_reached({api_call_count}/{self.max_iterations})"
9470:     final_response = self._handle_max_iterations(messages, api_call_count)
9472:     # Determine if conversation completed successfully
9473:     completed = final_response is not None and api_call_count < self.max_iterations
```

There are also ~20 early `return {...}` exits inside the loop (7809, 7830, 7913, 7953, 7981, 7998, 8010, 8334, 8427, 8452, 8503, 8545, 8572, 8651, 8731, 8772, 8916, 8968, 9016) for: invalid response shape, truncation, invalid tool names, invalid JSON args, non-retryable client errors, max retries, scratchpad truncation, interrupt.

### Interruption

`interrupt()` (`run_agent.py:2555`–`2592`):

```python
2579:         self._interrupt_requested = True
2580:         self._interrupt_message = message
2581:         # Signal all tools to abort any in-flight operations immediately
2582:         _set_interrupt(True)
2583:         # Propagate interrupt to any running child agents (subagent delegation)
2584:         with self._active_children_lock:
2585:             children_copy = list(self._active_children)
2586:         for child in children_copy:
2588:                 child.interrupt(message)
```

`_set_interrupt` is a **process-global** `threading.Event` (`tools/interrupt.py:15`):

```python
15: _interrupt_event = threading.Event()
26: def is_interrupted() -> bool:
27:     """Check if an interrupt has been requested. Safe to call from any thread."""
28:     return _interrupt_event.is_set()
```

Checked in 4 places inside the loop: loop top (`7425`), before each sequential tool (`6551`), after each sequential tool (`6851`), concurrent batch pre-flight (`6331`), plus inside the API-call threads (`4340`, `4913`, `3916`).

### Grace call after budget exhaustion

**No "grace round" with tools.** When budget is exhausted, `_handle_max_iterations` (`run_agent.py:6956`) makes **one extra tool-free API call** purely to produce a prose summary:

```python
6956:     def _handle_max_iterations(self, messages: list, api_call_count: int) -> str:
6957:         """Request a summary when max iterations are reached. Returns the final response text."""
6960:         summary_request = (
6961:             "You've reached the maximum number of tool-calling iterations allowed. "
6962:             "Please provide a final response summarizing what you've found and accomplished so far, "
6963:             "without calling any more tools."
6964:         )
6965:         messages.append({"role": "user", "content": summary_request})
```

Tools are explicitly stripped (`7005`: `codex_kwargs.pop("tools", None)`; `7035`: `tools=None`). This call is **outside** the `while` loop and does **not** consume budget. It retries once on empty response (`7057`–`7099`).

There is no similar grace round after an interrupt — interrupt exits immediately.

---

## 2. PHASE DECOMPOSITION

There is **no phase module tree**. Dispatch is inline. Mapping docs' claimed names to reality:

| Claimed module | Reality |
|---|---|
| `turn_preflight` | Inline block, `run_agent.py:7300`–`7356` (preflight compression, up to 3 passes) plus `pre_llm_call` hook `7371`–`7391` |
| `turn_api_call` | Inline `while retry_count < max_retries` block, `run_agent.py:7610`–`8779`; helper methods `_interruptible_api_call` (4303) / `_interruptible_streaming_api_call` (4414) |
| `turn_tool_round` | Method `_execute_tool_calls` (`6214`) dispatching to `_execute_tool_calls_sequential` (6545) / `_execute_tool_calls_concurrent` (6321) |

The only dispatch statement in the loop is:

```python
9164:                     self._execute_tool_calls(assistant_message, messages, effective_task_id, api_call_count)
```

and the executor's own branch (`run_agent.py:6221`–`6233`):

```python
6221:         tool_calls = assistant_message.tool_calls
6223:         # Allow _vprint during tool execution even with stream consumers
6224:         self._executing_tools = True
6225:         try:
6226:             if not _should_parallelize_tool_batch(tool_calls):
6227:                 return self._execute_tool_calls_sequential(
6228:                     assistant_message, messages, effective_task_id, api_call_count
6229:                 )
6230: 
6231:             return self._execute_tool_calls_concurrent(
6232:                 assistant_message, messages, effective_task_id, api_call_count
6233:             )
```

Plugin hook points (the closest thing to phase boundaries) — `hermes_cli/plugins.py:55`:

```python
55: VALID_HOOKS: Set[str] = {
56:     "pre_tool_call",
57:     "post_tool_call",
58:     "pre_llm_call",
59:     "post_llm_call",
60:     "pre_api_request",
61:     "post_api_request",
62:     "on_session_start",
63:     "on_session_end",
```

---

## 3. CONTEXT ASSEMBLY

### System prompt: 7 layers, not 6, with per-session caching

`_build_system_prompt` (`run_agent.py:2709`) documents the order in its own docstring (`2717`–`2724`):

```python
2717:         # Layers (in order):
2718:         #   1. Agent identity — SOUL.md when available, else DEFAULT_AGENT_IDENTITY
2719:         #   2. User / gateway system prompt (if provided)
2720:         #   3. Persistent memory (frozen snapshot)
2721:         #   4. Skills guidance (if skills tools are loaded)
2722:         #   5. Context files (AGENTS.md, .cursorrules — SOUL.md excluded here when used as identity)
2723:         #   6. Current date & time (frozen at build time)
2724:         #   7. Platform-specific formatting hint
```

Actual append order in code: identity (`2731`/`2736`) → tool guidance (`2748`) → Nous subscription (`2753`) → tool-use enforcement + model-specific guidance (`2776`/`2781`/`2785`) → `system_message` (`2792`) → memory + user profile (`2794`–`2803`) → external provider block (`2808`) → skills (`2823`) → context files (`2839`) → timestamp/model/provider line (`2846`–`2853`) → Alibaba model-identity workaround (`2860`) → platform hint (`2868`). Joined at `2871`: `return "\n\n".join(prompt_parts)`.

**Caching is real and session-scoped.** `run_agent.py:7248`–`7275`:

```python
7248:         # ── System prompt (cached per session for prefix caching) ──
7249:         # Built once on first call, reused for all subsequent calls.
...
7259:         if self._cached_system_prompt is None:
7260:             stored_prompt = None
7261:             if conversation_history and self._session_db:
7263:                     session_row = self._session_db.get_session(self.session_id)
7265:                         stored_prompt = session_row.get("system_prompt") or None
7269:             if stored_prompt:
7270:                 # Continuing session — reuse the exact system prompt from
7271:                 # the previous turn so the Anthropic cache prefix matches.
7272:                 self._cached_system_prompt = stored_prompt
7273:             else:
7274:                 # First turn of a new session — build from scratch.
7275:                 self._cached_system_prompt = self._build_system_prompt(system_message)
```

Invalidated only on compression (`run_agent.py:3032` `_invalidate_system_prompt`, called at `6134`) and model switch (`1426`). Memory inside the prompt is a **frozen snapshot** (`tools/memory_tool.py:120`–`134`, `335`–`348`) so mid-session writes don't mutate the prefix.

Ephemeral content is deliberately kept **out** of the system prompt (`run_agent.py:7535`–`7538`):

```python
7535:             # NOTE: Plugin context from pre_llm_call hooks is injected into the
7536:             # user message (see injection block above), NOT the system prompt.
7537:             # This is intentional — system prompt modifications break the prompt
7538:             # cache prefix.  The system prompt is reserved for Hermes internals.
```

### History assembly and truncation

Per-iteration: copy `messages`, inject current-turn user-message injections, strip internal fields, prepend system, insert prefill, apply cache control, sanitize orphans (`run_agent.py:7479`–`7560`):

```python
7560:             api_messages = self._sanitize_api_messages(api_messages)
7563:             total_chars = sum(len(str(msg)) for msg in api_messages)
7564:             approx_tokens = total_chars // 4  # Rough estimate: 4 chars per token
```

Injection into the user message (`7488`–`7499`):

```python
7488:                 if idx == current_turn_user_idx and msg.get("role") == "user":
7491:                         _fenced = build_memory_context_block(_ext_prefetch_cache)
7495:                         _injections.append(_plugin_user_context)
7499:                             api_msg["content"] = _base + "\n\n" + "\n\n".join(_injections)
```

`_strip_budget_warnings_from_history` (`384`) removes prior-turn budget warnings on entry (`7207`–`7208`).

### The 50% / 85% thresholds — what they actually are

**50%** is the compaction trigger, config default (`run_agent.py:1159`):

```python
1159:         compression_threshold = float(_compression_cfg.get("threshold", 0.50))
```

and `hermes_cli/config.py:336`–`340`:

```python
336:     "compression": {
337:         "enabled": True,
338:         "threshold": 0.50,            # compress when context usage exceeds this ratio
339:         "target_ratio": 0.20,         # fraction of threshold to preserve as recent tail
340:         "protect_last_n": 20,         # minimum recent messages to keep uncompressed
```

`ContextCompressor.__init__` default is 0.95 but is always overridden by the config value (`agent/context_compressor.py:67`, `93`):

```python
67:         threshold_percent: float = 0.95,
93:         self.threshold_tokens = int(self.context_length * threshold_percent)
```

**85% and 95% are *user-facing warning tiers relative to the 50% threshold***, not a second compression layer (`run_agent.py:9211`–`9218`):

```python
9211:                     if _compressor.threshold_tokens > 0:
9212:                         _compaction_progress = _real_tokens / _compressor.threshold_tokens
9214:                         _warn_tier = 0.0
9215:                         if _compaction_progress >= 0.95:
9216:                             _warn_tier = 0.95
9217:                         elif _compaction_progress >= 0.85:
9218:                             _warn_tier = 0.85
```

Declared at `run_agent.py:684`–`688`:

```python
686:         # fills up.  Purely informational — displayed in CLI output and sent via
687:         # status_callback for gateway platforms.  Does NOT inject into messages.
688:         # Tiered: fires at 85% and again at 95% of compaction threshold.
```

So the real numbers are 50% of context = compact; 25% and 47.5% of context = warn user. There is **no two-layer compression**.

Separately, the *LLM-facing* budget warning is two-tier at 90%/95% of `max_iterations` (`run_agent.py:680`–`681`):

```python
680:         self._budget_caution_threshold = 0.9   # 70% — nudge to start wrapping up
681:         self._budget_warning_threshold = 0.95   # 90% — urgent, respond now
```

(The inline comments contradict the values.)

### Summarization — yes

`ContextCompressor.compress` (`agent/context_compressor.py:612`), 4 phases:

```python
616:           1. Prune old tool results (cheap, no LLM call)
617:           2. Protect head messages (system prompt + first exchange)
618:           3. Find tail boundary by token budget (~20K tokens of recent context)
619:           4. Summarize middle turns with structured LLM prompt
620:           5. On re-compression, iteratively update the previous summary
```

```python
681:         summary = self._generate_summary(turns_to_summarize)
```

Structured template with sections `## Goal / ## Constraints & Preferences / ## Progress / ## Key Decisions / ## Relevant Files / ## Next Steps / ## Critical Context / ## Tools & Patterns` (`context_compressor.py:321`–`352`). Iterative update path when `_previous_summary` exists (`309`–`352`). Uses auxiliary/cheap model (`context_compressor.py:396`–`404`, `task="compression"`).

Budgets (`context_compressor.py:39`–`43`, `97`–`101`):

```python
39: _MIN_SUMMARY_TOKENS = 2000
41: _SUMMARY_RATIO = 0.20
43: _SUMMARY_TOKENS_CEILING = 12_000
```

Failure cooldown 600 s (`50`). Compression **splits the SQLite session** (`run_agent.py:6138`–`6160`) with `parent_session_id` lineage, and warns at ≥2 passes (`6168`).

Tool-result size control is a separate 3-layer system (`tools/budget_config.py:12`–`20`): per-result default 100 000 chars (`read_file` pinned to `inf`), per-turn aggregate 400 000 chars, preview 1 500 chars.

---

## 4. THE TOOL LAYER

### Registration

`tools/registry.py` — module-level singleton, import-time registration (`tools/registry.py:1`–`15`):

```python
1: """Central registry for all hermes-agent tools.
2: 
3: Each tool file calls ``registry.register()`` at module level to declare its
4: schema, handler, toolset membership, and availability check.  ``model_tools.py``
5: queries the registry instead of maintaining its own parallel data structures.
```

`register()` (`tools/registry.py:59`–`93`), `ToolEntry` slots at `27`–`45`. Schema emission filters by `check_fn()` — `get_definitions` (`116`–`143`):

```python
136:                 if not check_results[entry.check_fn]:
138:                         logger.debug("Tool %s unavailable (check failed)", name)
139:                     continue
```

MCP dynamic add/remove: `registry.deregister()` (`95`–`110`).

### Dispatch — two paths

Registry dispatch (`tools/registry.py:149`–`166`):

```python
149:     def dispatch(self, name: str, args: dict, **kwargs) -> str:
...
156:         entry = self._tools.get(name)
157:         if not entry:
158:             return json.dumps({"error": f"Unknown tool: {name}"})
159:         try:
160:             if entry.is_async:
161:                 from model_tools import _run_async
162:                 return _run_async(entry.handler(args, **kwargs))
163:             return entry.handler(args, **kwargs)
164:         except Exception as e:
165:             logger.exception("Tool %s dispatch error: %s", name, e)
166:             return json.dumps({"error": f"Tool execution failed: {type(e).__name__}: {e}"})
```

But `_invoke_tool` (`run_agent.py:6237`–`6319`) **short-circuits 7 agent-level tools before the registry**: `todo`, `working_checkpoint`, `session_search`, `memory`, memory-manager tools, `clarify`, `delegate_task`. Fallthrough:

```python
6313:         else:
6314:             return handle_function_call(
6315:                 function_name, function_args, effective_task_id,
6316:                 tool_call_id=tool_call_id,
6317:                 session_id=self.session_id or "",
6318:                 enabled_tools=list(self.valid_tool_names) if self.valid_tool_names else None,
6319:             )
```

`handle_function_call` (`model_tools.py:467`) → `registry.dispatch` (`model_tools.py:531`–`535`).

### Sequential vs concurrent — opt-in, default sequential

Gate `_should_parallelize_tool_batch` (`run_agent.py:268`–`309`): requires `len(tool_calls) > 1`, no `clarify`, and **every** tool either in `_PARALLEL_SAFE_TOOLS` (11 read-only tools, `220`–`232`) or `_PATH_SCOPED_TOOLS` with non-overlapping paths (`235`).

```python
306:         if tool_name not in _PARALLEL_SAFE_TOOLS:
307:             return False
```

Worker cap `_MAX_TOOL_WORKERS = 8` (`run_agent.py:238`). Execution (`6440`–`6447`):

```python
6440:             with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
6443:                     f = executor.submit(_run_tool, i, tc, name, args)
6447:                 concurrent.futures.wait(futures)
```

**Threads, not processes.** Results are re-ordered back into call order by index-slot array (`results = [None] * num_tools`, `6413`) and appended sequentially at `6514`–`6519`.

Sequential path is the duplicate inline `if/elif` chain (`run_agent.py:6632`–`6788`).

### Permission / approval gate — NONE at the orchestration layer

Grep for `permission|requires_approval|confirm(` in `run_agent.py`, `model_tools.py`, `tools/registry.py`, `hermes_state.py` → **no output**.

Approval exists **only inside `terminal_tool`**, at one call site (`tools/terminal_tool.py:1302`–`1303`):

```python
1302:         if not force:
1303:             approval = _check_all_guards(command, env_type)
```

It is skippable and defaults to open in several paths (`tools/approval.py:554`–`573`):

```python
554:     if env_type in ("docker", "singularity", "modal", "daytona"):
555:         return {"approved": True, "message": None}
557:     # --yolo: bypass all approval prompts
558:     if os.getenv("HERMES_YOLO_MODE"):
559:         return {"approved": True, "message": None}
...
572:     if not is_cli and not is_gateway:
573:         return {"approved": True, "message": None}
```

Modes: `manual` / `smart` / `off` (`tools/approval.py:473`–`476`), default `manual` with 60 s timeout (`hermes_cli/config.py:577`–`580`). `smart` mode calls an auxiliary LLM to vote APPROVE/DENY/ESCALATE (`_smart_approve`, `tools/approval.py:487`–`536`).

**File writes, patches, code execution, web fetches, MCP calls and browser actions have no approval gate in the loop.** The only pre-execution action for mutating tools is a filesystem snapshot (`run_agent.py:6607`–`6628`).

### Results returned to the model

Appended as `{"role": "tool", "content": result, "tool_call_id": tc.id}` (`run_agent.py:6836`–`6841`), after oversize persistence (`maybe_persist_tool_result`, `6824`) and subdir hints (`6832`). Per-turn aggregate budget enforcement at `6870`: `enforce_turn_budget(messages[-num_tools_seq:], env=...)`.

Error/duplicate handling before dispatch: `_cap_delegate_task_calls` (`9106`), `_deduplicate_tool_calls` (`9109`, keys on `(name, arguments)`), invalid-name → error tool results + 3 retries (`9000`–`9037`), invalid JSON → retry or inject error results (`9062`–`9100`).

---

## 5. THE MEMORY LAYER

### Provider interface

`agent/memory_provider.py:42` — abstract base with lifecycle contract documented at `16`–`31`:

```python
16: Lifecycle (called by MemoryManager, wired in run_agent.py):
17:   initialize()          — connect, create resources, warm up
18:   system_prompt_block()  — static text for the system prompt
19:   prefetch(query)        — background recall before each turn
20:   sync_turn(user, asst)  — async write after each turn
21:   get_tool_schemas()     — tool schemas to expose to the model
22:   handle_tool_call()     — dispatch a tool call
23:   shutdown()             — clean exit
```

Optional hooks (`memory_provider.py:144`–`186`): `on_turn_start`, `on_session_end`, `on_pre_compress`, `on_memory_write`, `on_delegation`.

### Manager — enforces built-in + 1 external

`agent/memory_manager.py:1`–`10`:

```python
1: """MemoryManager — orchestrates the built-in memory provider plus at most
2: ONE external plugin memory provider.
...
7: The BuiltinMemoryProvider is always registered first and cannot be removed.
8: Only ONE external (non-builtin) provider is allowed at a time
```

Rejection enforced at `memory_manager.py:95`–`108`. Config `memory.provider` defaults to `""` (`hermes_cli/config.py:502`).

**However**: in the actual wiring, `run_agent.py:1084`–`1111` only ever registers the *external* provider — `BuiltinMemoryProvider` is **never instantiated by `run_agent.py`** (grep confirms: only `tests/agent/test_memory_plugin_e2e.py` references it). Built-in memory is handled directly via `self._memory_store`, not through the manager. The "built-in + 1 external" framing describes the manager's design, not the runtime composition.

### What gets remembered / when written

Built-in = `MEMORY.md` + `USER.md`, bounded markdown entry lists (`tools/memory_tool.py:124`–`125`, limits 2 200 / 1 375 chars, `hermes_cli/config.py:496`–`497`). Actions: `add` / `replace` / `remove` (`tools/memory_tool.py:459`–`470`).

Write points:
1. **Model-driven** — `memory` tool, intercepted at `run_agent.py:6273` / `6670`; bridges to external provider at `6284`–`6292`.
2. **Flush before loss** — `flush_memories` (`run_agent.py:5937`), one tool-restricted auxiliary call, min 6 user turns by default (`5951`–`5957`, `1030`), invoked with `min_turns=0` before compression (`6111`).
3. **Background review** — `_spawn_background_review` (`run_agent.py:1822`), a full `AIAgent` fork with `max_iterations=8` (`1854`), stdin/stdout redirected to devnull (`1849`–`1851`), sharing the parent's live stores (`1859`–`1861`). Triggered post-turn at `9612`–`9618`.

### When read / how injected

Read: `MemoryStore.load_from_disk()` (`tools/memory_tool.py:120`) at agent init (`run_agent.py:1046`). Re-loaded on cache invalidation (`_invalidate_system_prompt`, `3032`).

Injected **two different ways**:
- **Full injection, frozen**: entire MEMORY.md + USER.md content into the *system prompt* (`run_agent.py:2794`–`2803`), from the snapshot taken at load time (`tools/memory_tool.py:335`–`348`). Not searchable — it's full text, char-capped.
- **Search/prefetch, ephemeral**: external provider recall fetched **once per turn** before the loop (`run_agent.py:7407`–`7418`):

```python
7407:         # External memory provider: prefetch once before the tool loop.
7408:         # Reuse the cached result on every iteration to avoid re-calling
7409:         # prefetch_all() on each tool call (10 tool calls = 10x latency + cost).
7416:                 _ext_prefetch_cache = self._memory_manager.prefetch_all(_query) or ""
```

Injected into the **user message**, not the system prompt, inside a fence (`agent/memory_manager.py:54`–`69`):

```python
63:     return (
64:         "<memory-context>\n"
65:         "[System note: The following is recalled memory context, "
66:         "NOT new user input. Treat as informational background data.]\n\n"
```

Written into `api_messages` per-iteration at `run_agent.py:7490`–`7493`. Fence escapes stripped by `sanitize_context` (`memory_manager.py:49`).

### "Periodic light push"

`memory.nudge_interval`, default 10 user turns (`run_agent.py:1029`, `1038`). Checked at turn start (`7231`–`7237`) — turn-counted, **not** a background push:

```python
7231:         if (self._memory_nudge_interval > 0
7232:                 and "memory" in self.valid_tool_names
7233:                 and self._memory_store):
7234:             self._turns_since_memory += 1
7235:             if self._turns_since_memory >= self._memory_nudge_interval:
7236:                 _should_review_memory = True
7237:                 self._turns_since_memory = 0
```

The actual "push" is the **background review fork** (`9612`–`9618`), which is turn-gated, not iteration-gated. A separate iteration-gated nudge exists for skills (`9594`–`9598`, `skills.creation_nudge_interval` default 10).

### "End-of-session extraction"

`on_session_end` is **not** called from the turn loop. Comment at `9622`–`9627`:

```python
9622:         # Note: Memory provider on_session_end() + shutdown_all() are NOT
9623:         # called here — run_conversation() is called once per user message in
9624:         # multi-turn sessions.  Shutting down after every turn would kill the
9625:         # provider before the second message.  Actual session-end cleanup is
9626:         # handled by the CLI (atexit / /reset) and gateway (session expiry /
9627:         # _reset_session).
```

Called only via `shutdown_memory_provider` (`run_agent.py:2646`–`2661`), from `cli.py:621` and `gateway/run.py:1318`, `1498`. `sync_all` + `queue_prefetch_all` do run per turn (`9605`–`9606`).

Provider plugins present: `byterover`, `hindsight`, `holographic`, `honcho`, `mem0`, `openviking`, `retaindb`, `supermemory` (`ls plugins/memory/`). Honcho has an auto-migration path (`run_agent.py:1064`–`1082`).

---

## 6. SESSION LIFECYCLE

### ID

Generated in `AIAgent.__init__` (`run_agent.py:940`–`947`):

```python
940:         if session_id:
941:             # Use provided session ID (e.g., from CLI)
942:             self.session_id = session_id
943:         else:
944:             # Generate a new session ID
945:             timestamp_str = self.session_start.strftime("%Y%m%d_%H%M%S")
946:             short_uuid = uuid.uuid4().hex[:6]
947:             self.session_id = f"{timestamp_str}_{short_uuid}"
```

Separate `effective_task_id` per turn for VM/browser isolation (`run_agent.py:7157`).

### Created

`__init__` (`run_agent.py:972`–`995`):

```python
974:             self._session_db.create_session(
975:                 session_id=self.session_id,
976:                 source=self.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
977:                 model=self.model,
978:                 model_config={
979:                     "max_iterations": self.max_iterations,
```

DB is `SessionDB` (`hermes_state.py:115`), SQLite with 3 tables — `schema_version`, `sessions`, `messages` (`hermes_state.py:37`, `41`, `71`) — plus FTS5. JSON mirror at `~/.hermes/sessions/session_<id>.json` (`run_agent.py:953`, written by `_save_session_log` at `2489`).

### Loaded / resumed

Stored system prompt reused verbatim (`run_agent.py:7261`–`7272`, quoted in §3). History restored via `SessionDB.get_messages_as_conversation` (`hermes_state.py:952`), called from `cli.py:2607` and `cli.py:2819`. Resume by session id or title (`cli.py:3738`–`3781`, `resolve_session_id` at `hermes_state.py:598`, `get_session_by_title` at `710`). `reopen_session` clears `ended_at` (`hermes_state.py:394`).

### Saved

`_persist_session` (`run_agent.py:1946`) → `_save_session_log` (JSON) + `_flush_messages_to_session_db` (SQLite). The flush is **cursor-based** to avoid duplicate writes:

```python
1978:             start_idx = len(conversation_history) if conversation_history else 0
1979:             flush_from = max(start_idx, self._last_flushed_db_idx)
1980:             for msg in messages[flush_from:]:
...
2003:             self._last_flushed_db_idx = len(messages)
```

Called at the normal turn exit (`9482`) and on **every** early-return error path. Mid-turn incremental save at `9250`.

### Checkpointing

Two distinct mechanisms:
- **Session split on compression** — new session row, `parent_session_id` lineage, auto-numbered title (`run_agent.py:6138`–`6160`).
- **Filesystem checkpoints** — `CheckpointManager` (`tools/checkpoint_manager.py:181`), git-shadow-repo snapshots, `enabled`/`max_snapshots: 50` (`hermes_cli/config.py:326`–`329`), taken once per turn before `write_file`/`patch`/destructive `terminal` (`run_agent.py:6607`–`6628`), restored via `/rollback`.

**There is no mid-turn loop checkpoint / no re-entry point.** An interrupted turn is persisted and the next turn starts a fresh `run_conversation` with the persisted history.

---

## 7. MULTI-SESSION / DELEGATION

`tools/delegate_tool.py` — in-process sub-agents, **not** separate processes.

Depth + concurrency caps (`tools/delegate_tool.py:37`–`39`):

```python
37: MAX_CONCURRENT_CHILDREN = 3
38: MAX_DEPTH = 2  # parent (0) -> child (1) -> grandchild rejected (2)
39: DEFAULT_MAX_ITERATIONS = 50
```

Depth check (`533`–`540`):

```python
533:     depth = getattr(parent_agent, '_delegate_depth', 0)
534:     if depth >= MAX_DEPTH:
535:         return json.dumps({
536:             "error": (
537:                 f"Delegation depth limit reached ({MAX_DEPTH}). "
```

Child construction — a full `AIAgent` on the **main thread** (`586`–`607`, child build at `287`–`316`):

```python
287:     child = AIAgent(
...
295:         max_iterations=max_iterations,
...
304:         skip_context_files=True,
305:         skip_memory=True,
...
315:         iteration_budget=None,  # fresh budget per subagent
316:     )
```

Parent toolset is intersected, never widened (`242`–`243`): `child_toolsets = _strip_blocked_tools([t for t in toolsets if t in parent_toolsets])`.

Results return to the parent synchronously as one JSON tool result (`688`–`691`):

```python
688:     return json.dumps({
689:         "results": results,
690:         "total_duration_seconds": total_duration,
691:     }, ensure_ascii=False)
```

Per-child envelope (`450`–`463`): `task_index`, `status` (`completed`/`failed`/`interrupted`/`error`), `summary` (= `final_response`), `api_calls`, `duration_seconds`, `model`, `exit_reason`, `tokens`, `tool_trace`.

Memory notification to parent (`673`–`684`):

```python
678:                 parent_agent._memory_manager.on_delegation(
679:                     task=_task_goal,
680:                     result=entry.get("summary", "") or "",
681:                     child_session_id=getattr(children[entry["task_index"]][2], "session_id", "") or "",
```

**Concurrency:** batch mode only, `ThreadPoolExecutor` (`619`–`629`):

```python
609:     if n_tasks == 1:
610:         # Single task -- run directly (no thread pool overhead)
611:         _i, _t, child = children[0]
612:         result = _run_single_child(0, _t["goal"], child, parent_agent)
613:         results.append(result)
614:     else:
615:         # Batch -- run in parallel with per-task progress lines
619:         with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN) as executor:
```

The loop caps task count at 3 (`559`: `task_list = tasks[:MAX_CONCURRENT_CHILDREN]`), and `_cap_delegate_task_calls` (`run_agent.py:2957`) also truncates excess `delegate_task` tool_calls in one turn.

Also present: `mixture_of_agents_tool.py` (multi-reference-model query), `acp_command`/`acp_args` for spawning external ACP child agents (`tools/delegate_tool.py:516`–`517`, `284`–`285`).

---

## 8. SUBAGENTS / PARALLEL

Four distinct parallelism mechanisms:

| Mechanism | Concurrency | Coordination |
|---|---|---|
| Tool batch | `ThreadPoolExecutor`, ≤8 workers (`run_agent.py:6440`) | Opt-in via `_should_parallelize_tool_batch`; results index-ordered back into `messages` |
| Delegation batch | `ThreadPoolExecutor(max_workers=3)` (`delegate_tool.py:619`) | `as_completed` (631), results sorted by `task_index` (671) |
| Streaming | ≤3 poll threads per call (`run_agent.py:4874`, `4336`) | Interrupt polling at 0.3 s / 0.2 s join |
| Background review | 1 daemon thread (`run_agent.py:1925`) | `threading.Thread(target=_run_review, daemon=True, name="bg-review")` |

**No orchestrator, scheduler, work-stealing, or task graph.** There is no multi-agent coordination protocol; each thread runs an independent `run_conversation`.

Cross-agent coordination is limited to:
- Interrupt propagation (`run_agent.py:2584`–`2590`)
- Credential pool leasing (`delegate_tool.py:360`–`370`, released `482`–`486`)
- A **process-global tool-name mutation workaround** — building children clobbers `model_tools._last_resolved_tool_names`, so it's saved and restored (`delegate_tool.py:580`–`607`, `488`–`494`). This is a global-mutable-state hazard, not a coordination mechanism.

At the gateway level, sessions are serialized by a sentinel guard (`gateway/run.py:2304`, `_AGENT_PENDING_SENTINEL` at `289`), and interrupts are polled by a per-run asyncio task (`gateway/run.py:7084`–`7104`).

---

## 9. ERROR HANDLING / RECOVERY

### Classification — 14 levels, not 7

`agent/error_classifier.py:25`–`58`, `FailoverReason` enum:

```python
25: class FailoverReason(enum.Enum):
26:     """Why an API call failed — determines recovery strategy."""
28:     # Authentication / authorization
29:     auth = "auth"                        # Transient auth (401/403) — refresh/rotate
30:     auth_permanent = "auth_permanent"    # Auth failed after refresh — abort
32:     # Billing / quota
33:     billing = "billing"                  # 402 or confirmed credit exhaustion — rotate immediately
34:     rate_limit = "rate_limit"            # 429 or quota-based throttling — backoff then rotate
36:     # Server-side
37:     overloaded = "overloaded"            # 503/529 — provider overloaded, backoff
38:     server_error = "server_error"        # 500/502 — internal server error, retry
40:     # Transport
41:     timeout = "timeout"                  # Connection/read timeout — rebuild client + retry
43:     # Context / payload
44:     context_overflow = "context_overflow"  # Context too large — compress, not failover
45:     payload_too_large = "payload_too_large"  # 413 — compress payload
47:     # Model
48:     model_not_found = "model_not_found"  # 404 or invalid model — fallback to different model
50:     # Request format
51:     format_error = "format_error"        # 400 bad request — abort or strip + retry
53:     # Provider-specific
54:     thinking_signature = "thinking_signature"  # Anthropic thinking block sig invalid
55:     long_context_tier = "long_context_tier"    # Anthropic "extra usage" tier gate
57:     # Catch-all
58:     unknown = "unknown"                  # Unclassifiable — retry with backoff
```

Verified count: **14 members**. Priority-ordered pipeline documented at `242`–`249` (7 *stages*, not 7 levels): provider-specific patterns → HTTP status → error code → message pattern → server-disconnect heuristic → transport → unknown.

Recovery hints (`76`–`79`):

```python
76:     retryable: bool = True
77:     should_compress: bool = False
78:     should_rotate_credential: bool = False
79:     should_fallback: bool = False
```

Consumed in the loop at `run_agent.py:8177`–`8190`.

### Retry

Inner loop `while retry_count < max_retries`, `max_retries = 3` (`run_agent.py:7596`, `7610`). Backoff via `jittered_backoff` (`agent/retry_utils.py:19`), base 5 s, cap 120 s, jitter_ratio 0.5 — called at `run_agent.py:7826`. `Retry-After` honored (`8740`–`8745`). Interrupt-aware sleep loop polls every 0.2 s (`7831`–`7838`).

Per-error-type one-shot recoveries (each guarded by a `*_attempted` flag, `7599`–`7602`):
- Surrogate chars → sanitize + retry (`8160`–`8167`)
- Credential pool rotation (`8192`–`8198`, `_recover_with_credential_pool` at `4245`)
- Codex / Nous / Anthropic credential refresh on 401 (`8200`–`8229`)
- Thinking-signature invalid → strip `reasoning_details` from all messages (`8253`–`8271`)
- Context overflow / 413 → `_compress_context`, up to 3 attempts (`max_compression_attempts = 3`, `7598`; exits at `8427`, `8503`, `8545`, `8572`)
- Empty response → 3 retries then `"(empty)"` (`9346`)
- Invalid tool names → 3 retries (`9012`), invalid JSON → 3 retries (`9069`)
- Truncated response → length continuation, 3 attempts (`7953`); truncated tool args → refuse to execute (`7974`)
- Incomplete scratchpad → 2 retries (`8903`)

### Fallback to another provider/model — yes

`_try_activate_fallback` (`run_agent.py:4959`) advances a configured chain (`_fallback_chain`, `874`–`881`), swaps client/model/provider/`api_mode` in place (`5015`–`5052`), and **recomputes the context compressor limits for the new model** (`5065`–`5078`).

Invoked from three places: non-retryable client error (`8609`), max-retries exhausted (`8673`), invalid response shape (`7808`). Resets `retry_count = 0` on success. `Retry-After`-less 429 also rotates credentials via the pool.

Primary restored per-turn (`run_agent.py:7142` → `_restore_primary_runtime` at `5095`), so each turn gets a fresh primary attempt.

---

## 10. CONFIGURATION

Config file: `~/.hermes/config.yaml` — `hermes_cli/config.py:148`–`150`:

```python
148: def get_config_path() -> Path:
149:     """Get the main config file path."""
150:     return get_hermes_home() / "config.yaml"
```

Loop-governing keys (`hermes_cli/config.py`):

```python
245: DEFAULT_CONFIG = {
246:     "model": "",
247:     "providers": {},
248:     "fallback_providers": [],
...
251:     "agent": {
252:         "max_turns": 90,
257:         "gateway_timeout": 1800,
263:         "tool_use_enforcement": "auto",
267:         "gateway_timeout_warning": 900,
268:     },
```

```python
336:     "compression": {
337:         "enabled": True,
338:         "threshold": 0.50,
339:         "target_ratio": 0.20,
340:         "protect_last_n": 20,
341:         "summary_model": "",
```

```python
492:     # Persistent memory -- bounded curated memory injected into system prompt
493:     "memory": {
494:         "memory_enabled": True,
495:         "user_profile_enabled": True,
496:         "memory_char_limit": 2200,   # ~800 tokens at 2.75 chars/token
497:         "user_char_limit": 1375,     # ~500 tokens at 2.75 chars/token
502:         "provider": "",
503:     },
```

```python
509:     "delegation": {
510:         "model": "",       # empty = inherit parent model
511:         "provider": "",    # empty = inherit parent provider + credentials
514:         "max_iterations": 50,  # per-subagent iteration cap
516:     },
```

```python
526:     "skills": {
529:         "creation_nudge_interval": 10,
...
543:     "working_checkpoint": {
544:         "enabled": True,
545:         "max_chars": 1200,
546:     },
```

```python
577:     "approvals": {
578:         "mode": "manual",
579:         "timeout": 60,
580:     },
```

Code-level constants (not config-driven): `_MAX_TOOL_WORKERS = 8` (`run_agent.py:238`), `_budget_caution_threshold = 0.9` / `_budget_warning_threshold = 0.95` (`680`–`681`), `protect_first_n = 3` hardcoded (`1201`), `_CONTEXT_PRESSURE_COOLDOWN = 300` (`453`), `_memory_nudge_interval` / `_memory_flush_min_turns` defaults 10 / 6 (`1029`–`1030`), `_SUMMARY_TOKENS_CEILING = 12_000` (`context_compressor.py:43`).

Env overrides: `HERMES_MAX_ITERATIONS` (`cli.py:1597`), `HERMES_YOLO_MODE` (`approval.py:558`), `HERMES_API_TIMEOUT` / `HERMES_STREAM_READ_TIMEOUT` / `HERMES_STREAM_STALE_TIMEOUT` (`run_agent.py:4463`, `4464`, `4853`).

---

## 11. WHAT IS ABSENT

| Capability | Verdict | Evidence |
|---|---|---|
| **Persistent event bus / event sourcing** | **No** | Grep for `event_bus\|EventBus\|event_sourcing\|append_event\|emit_event` across all non-venv/non-test Python → **no output**. State is mutable `messages: list` + SQLite rows written by `_flush_messages_to_session_db` (`run_agent.py:1959`). No event log, no replay, no subscribers. Closest analogues are *in-process* callback hooks (`hermes_cli/plugins.py:55` — 8 named hooks, `invoke_hook` returns values synchronously) and a **class-level dict** for warning dedup (`run_agent.py:452`: `_context_pressure_last_warned: dict = {}`), which is ephemeral per-process. |
| **Session inbox / queue** | **No** | The agent has no queue: `run_conversation` is called once per user message and returns. There is no pending-message store for the agent. Gateway-side there *is* a per-session pending slot, but it is a single-slot interrupt latch, not a queue — `gateway/platforms/base.py:1656`–`1662`: `has_pending_interrupt` / `get_pending_message` where `get_pending_message` is `self._pending_messages.pop(session_key, None)`. And a second message does not queue: it calls `agent.interrupt(...)` (`gateway/run.py:2034`), killing the current turn (`run_agent.py:2583`). Platform adapters do have `asyncio.Queue`s for transport (`gateway/platforms/whatsapp.py:143`, `gateway/platforms/api_server.py:310`) — transport-level, not session-level. |
| **Doorbell / wake pattern** | **No** (for the agent) | Grep for `doorbell\|wake_up\|on_wake\|wakeword` in `gateway/`, `hermes_cli/`, `run_agent.py` → only Telegram **wake-word regex for group-chat mention triggers** (`gateway/platforms/telegram.py:1955`, `2061`) and Railway/Fly auto-wake HTTP comments (`telegram.py:475`, `585`). There is no wake signal that starts an idle session: the only "waking" is a poll loop checking an interrupt latch (`gateway/run.py:7089`–`7102`) and a 10-minute keep-alive notifier (`gateway/run.py:7106`–`7108`). |
| **Context compaction with LLM summarization** | **Yes** | `agent/context_compressor.py:612` `compress()`, 4 phases, LLM call at `681` `self._generate_summary(turns_to_summarize)` → `call_llm(task="compression", ...)` at `396`–`404`. Iterative update of prior summary at `309`–`352`. Failure cooldown 600 s (`50`), static fallback marker when the LLM is unavailable (`696`–`706`). |
| **Permission layer** | **No** (at the orchestration layer) | Zero occurrences of `permission` in `run_agent.py`, `model_tools.py`, `tools/registry.py`, `hermes_state.py`. `_execute_tool_calls` (6214) and `_invoke_tool` (6237) contain no approval check. Approval lives only inside `terminal_tool.py:1303` and is bypassed for container backends, `--yolo`, and any non-CLI/non-gateway context (`approval.py:554`–`573`). No other mutating tool (write/patch/execute_code/web/MCP/browser) has any gate. |

Additional absences worth naming:

- **No token or cost budget on the loop.** `IterationBudget` is a pure counter (`run_agent.py:191`–`203`); token counters are recorded (`1218`–`1229`) but never enforced as a stop condition.
- **No mid-turn checkpoint / no loop resume.** Persisted state is only re-enterable at a turn boundary.
- **No loop-level scheduler.** Parallelism is ad-hoc thread pools; no work queue, priority, or fairness.
- **No turn-level plugin hook between phases.** `post_llm_call` fires only once per *turn* after the loop ends (`run_agent.py:9532`–`9543`), not per iteration — so there is no per-iteration interception point in the loop body.