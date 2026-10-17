# 01 · 我们的 ReAct 循环：一回合端到端数据流（代码级）

> 仓库：本仓（ai-coding）。行号以当前工作区为准；论断以代码为准。
> 主线口径：TUI/CLI 经 IPC `session.prompt` 跑一次日常对话回合（可能含 1 个工具调用）。
> 姊妹篇：`02-dsh编排数据流.md`、`03-opencode编排数据流.md`（并行调研 agent 产出、已抽检）、`04-三方对比.md`。
> 结论速览：我们是三家唯一"**显式策略对象驱动**"的循环——暂停/续跑/收尾由纯函数 `TurnPolicy` 决定（可表格测试），dsh/opencode 则由机制（工具未完/事件）决定。

---

## 0. 一句话主链

```
TUI/CLI ──IPC──▶ session_prompt (server/handlers.py:294)
  → _launch_turn (:255) spawn 协程（CancelledError/异常漏斗在这里）
  → AgentLoop.process_input (core/agent_loop.py:382)   [_turn_lock 串行]
  → _run_turn (:453)
      ├ _capture_snapshot (:466)           # git pin pre-turn + _forget_redo(L5)
      ├ _begin_turn (:473)                 # user 消息入 history(:804)、turn_count+1、open_turn(:855)
      └ while True (:496)
          ├ policy.decide_next_iteration (:503)   # FINISH/NUDGE/CONTINUE（纯决策）
          ├ _take_steering (:585)                 # 吸收中途输入（[steering] 消息 :879）
          ├ assembler.assemble (:591)             # 压缩 + instructions + volatile tail
          ├ _model_step (:610)                    # engine.step(:1338) → assistant 落 history(:1402)
          ├ 无工具 → decide_after_step (:682) → FINISH/收尾 or nudge+checkpoint
          └ 有工具 → _run_tools (:710)            # dispatch_batch(:1175) → write_back(:1201) → checkpoint(:1204)
  → _finish_turn (:758/926)                # skill outcome → _close_turn(:1043) → harvest → turn.end hooks
事件：LoopEvent → SafeObserver → EventBridge (server/bridge.py:275) → IPC notify → 前端 reducer
```

---

## 1. 入口

### 1.1 IPC handler（`server/handlers.py`）

1. `session_prompt`（:294）：校验会话已加载（:305-308，foreign id 直接拒绝）→ 图片 data URL 落盘（:313-330）→ 构造 `run_turn` 闭包（调 `world.loop.process_input`，把 `bridge.on_delta` / `bridge` 分别接到 delta 与事件两个通道）。
2. `_launch_turn`（:255）：统一 spawn——`CancelledError` 静默穿过（循环已自己持久化了部分回合）、其它异常映射为一条 `turn.finished(error)` 事件（:270-290）；`turn_id` 记账（:291-294）。`session_resume`（:344）也是它。
3. 另外两个入口：`initialize`（:189，新建会话）、`session.load`（:232，换载 + 返回恢复事实 P5）。

### 1.2 与另两家的入口模型差异（细节见 04）

我们没有 durable inbox：prompt 直接 spawn 到 loop；用户消息在 `_begin_turn` 进内存 history（:804），**首个 checkpoint 才落盘**（P2 接受的窗口，见 06-补充式方案 §5.3）（**回戳 2026-10-17**：窗口已关——`_begin_turn` 入口即 save，`agent_loop.py:1022-1024`，11-plan T5；20 §A）。dsh/opencode 都是"先 durable 入队、再唤醒"（崩溃可恢复）。我们靠 `open_turn` marker + `resume_turn` 覆盖同场景。

---

## 2. 循环结构（`core/agent_loop.py`）

### 2.1 驱动骨架

- `process_input`（:382）：`async with self._turn_lock` —— 循环级串行，第二个调用者等待而不是被拒。
- `_run_turn`（:453）：回合本体（resume 也走它，`append_user=False`）。
- 回合前段：`_capture_snapshot`（:466）pin pre-turn 工作区（git stash create → refs/ai-coding/undo，best-effort）+ `_forget_redo`（L5：新回合提交已 staged 的回退，条件 save）；`_begin_turn`（:473 调用 / :768 定义）：bind observer/trace → user 消息 append（:804）→ `turn_count += 1` → task 初始化 → `guards.reset()` → `reporter.begin_turn` → `session.open_turn = turn_count`（:855，P2，搭下一次 save 的车）。

### 2.2 一次迭代（:496-740）按序

| 序 | 做什么 | 位置 |
|---|---|---|
| 1 | `index++`；deadline 预算检查 | :497-499 |
| 2 | `_refresh_ledger`：开放未知集（边缘触发发 `LedgerUpdated`） | :502 / :1207 |
| 3 | **`policy.decide_next_iteration`**：纯函数 → FINISH / NUDGE | :503 |
| 4 | FINISH 分支：有验收契约先 `_verify_acceptance`；预算耗尽时 `_ask_for_closing` 要个收尾词（G8-2，一次/回合）→ break | :504-556 / :547 |
| 5 | NUDGE 分支：synthetic user 消息（[task-gate]/fatigue/strategy）+ checkpoint；continuation 则 `index=0` 重开预算 | :557-582 |
| 6 | `books.note_iteration`；`_take_steering` 吸收中途输入（每条 `[steering]` + checkpoint） | :584-585 / :865 |
| 7 | `IterationStarted` 事件 | :586-590 |
| 8 | **`assembler.assemble`**：压缩 + instructions + tail → request | :591-601 |
| 9 | **`_model_step`**：`engine.step`（唯一不可降级的等待）；ThinkingOnly 重试一次；`EngineError(overflow)` → `compact_now` 一次 + continue | :610-674 |
| 10 | 无工具：`_verify_acceptance` → `decide_after_step`（含 acceptance 状态）→ FINISH 建 `TurnOutcome` break；否则 nudge+checkpoint | :676-708 |
| 11 | 有工具：`_run_tools`（见 §4）→ `note_outcomes` → 约束检查（SOP router）→ `halted` 则 `stopped_outcome` break | :710-740 |

异常漏斗（唯一）：`CancelledError` → `_close_turn(aborted)` 再抛（:741-752）；`DeadlineExpired/EngineError/EmptyStepError/ThinkingOnly` → `failed_outcome`（:753-756）。收尾统一走 `_finish_turn`（:758/926）：skill outcome 记账 → `_close_turn`（:1043，清 `open_turn`:1064 后 `closer.close` 一次写盘）→ memory harvest → `turn.end` hooks → 返回 text/handoff。

### 2.3 决策：policy 而不是机制（本仓独有）

`TurnPolicy`（`core/turn_policy.py`，`TurnAction` :109-114）是纯函数：迭代循环的每一步"停/催/续"（`decide_next_iteration`）与拿到最终文本后的"真的完了吗"（`decide_after_step`，含 acceptance verdict、疲劳、连续 continuation 上限）都在它里面，可表格测试。dsh 的续跑条件是机制（本步有工具执行未完→null），opencode 是 `needsContinuation` 位；我们的 policy 同时管 nudge、验收、预算——这是三家最大的架构差异。

### 2.4 终止 / 续跑 / 打断

- 终止：policy FINISH（模型说完了 / 验收全绿 / 预算尽且无欠账）；工具批 `halted`；异常。
- 续跑：模型带工具 → `Continue`；NUDGE 重来；`session.continuations` 跨回合存活（C3 额度）。
- 打断：`session.interrupt` → 两层（tool batch 的 `cancel` Event + turn future.cancel）→ `CancelledError` 路径持久化部分回合。
- 崩溃续跑：`open_turn` + `resume_turn`（:400，见 §7）。

---

## 3. 请求装配（`core/request_assembly.py` + `core/request_builder.py`）

### 3.1 `assemble`（:120）——"压缩、指令、注入、尾块，按这个顺序，每个迭代"

1. router 先取压缩前锚点（I3，:135-137）。
2. **压缩**：`compactor.compress`（:139，预算驱动；`auto_compact` 关掉时直接用原 history，:146-150）→ `did_compact`（:154）→ 首次发 `ContextCompacted`（:156-164）。
3. **instructions**：`_build_instructions`（agent_loop.py:1765）＝项目指令 + skill 目录（`skills/catalog.py:82 build_catalog`，`superseded_note` 防目录静默增长）；policy 可注入 task 文本（:170-171）。
4. **volatile tail**（:259-272，声明序，从不排序/合并）：memory → mcp_catalog → task → recommended_skills → referenced_files → error_recovery → anchor → hook → digest → budget。每块"没事就不出现"（`volatile_block` 空即 None）。
5. `request = history + tail`（:273）；`reporter.report_context`（:274）产出 **canonical `previous_request`**——供下一迭代对比"前缀是否移动"（前缀缓存诊断，R4a）。

### 3.2 与另两家的装配模型（细节见 04）

- 历史真源：我们是**可变 `SessionData.messages` 列表**（整文件快照持久化）；dsh 是**追加日志 surface + `deriveMessages` 纯派生**；opencode 是 **SQLite 行 + 事件投影**。
- 前三块（system、history、tool schema）在迭代间字节稳定，只允许 tail 变——这是我们对 prefix-cache 的显式契约（request_builder.py 头注释）。

### 3.3 工具 schema

`tool_schemas`（`tools/schema.py:32`）在 `AgentLoop.__init__` 算一次（:314），`engine.step(tools=self._tool_schemas)`（:1338）。engine 接口见 `ai/engine.py:94`（`step(messages, tools, instructions, on_delta, image_loader)`；instructions 独立参数，保持 history 纯净）。

---

## 4. 工具层（`core/dispatch.py` + `tools/`）

### 4.1 注册与准备

- 注册表 `ToolRegistry`（`tools/registry.py`），每个工具 `BaseTool`（`name/params_json_schema/execute/precheck/parallel_safe`）。
- `_run_tools`（agent_loop.py:1151）：clear cancel（:1174，R8c 一次 Escape 只丢当批）→ 组 `ToolContext`（:1177-1192：task/workspace/session/history/instructions/emit/turn/note_checkpoint/**note_approval**）→ `dispatch_batch`。
- `_prepare`（dispatch.py:228）：每 call 解析工具 + **gate verdict 一次**（`PermissionGate.check`）。

### 4.2 并行规则（批次级，最保守的一家）

`_can_run_parallel`（:236）要求**每一个** call 都 `parallel_safe` 且 ALLOW——满足才 `asyncio.gather` + semaphore（:202-209）；否则严格串行，`halted` 后剩余标 skipped（:211-224）。对比：dsh 每 call 按 `executionMode` 分类组池（上限 10，模型序提交）；opencode 每 call 一个 fiber 全并发 + `awaitAll`。

### 4.3 单调用决策链 `_decide`（:283）

顺序：unknown → `DENY` → guards（REWRITE 后**重新过 gate**，防改写绕过）→ user hooks TOOL_BEFORE（BLOCK/REWRITE）→ `precheck` → **WARN** → 执行 `_execute`（:475）→ guards.after → TOOL_AFTER hooks。每个分支产出 `ToolResult` + `denied/halted` 旗标；`_handle`（:247）在前后发 `ToolStarted`/`ToolFinished` 事件。

### 4.4 审批落线（P3，本仓独有 durability）

WARN 分支（:349-375）：**先** `ctx.note_approval({callId, toolName, params, reason})` 写 marker（`_note_approval` :1137 → `SessionService.mark_pending_approval` :120，`pending/<id>.json`），**再** `_ask`（:428）等 `ApprovalCallback`；`finally` 里 `note_approval(None)` 清 marker。崩溃在等审批上 → 重启 `load` 读回 `pending_approval`（P3）。服务端 provider 由 `_install_providers`（handlers.py:63-75）安装成 `approval.request` IPC → 前端 permission 面板。
对照：dsh 是 `tools/pre-execute` 瀑布 + sandbox escalation 两条通道；opencode 是 leaf 工具内 `permission.assert` + ephemeral `Permission.Asked` 事件（拒绝走 defect 隧道，模型看不到 "user said no"）。**两家都没有审批崩溃持久化**——这是我们的 P3 相对优势。

### 4.5 结果回写

`closer.write_back`（turn_close.py:164 / 调用 :1201）：每 outcome 一条 `role=tool` 消息（`ToolOutput{call_id, output, success, data, artifacts}`）；`todo_write` 触发 `TaskUpdated`（:1202-1203）；`closer.checkpoint`（:1204）节流落盘（消息数 ≤ `persist_checkpoint_messages` 或 `index % LONG_SESSION_STRIDE == 0`，turn_close.py:184-188）。下一步迭代重新 assemble 时 tool 消息自然在 history 里。

---

## 5. 事件流

- 事件家族（`core/events.py`）：`IterationStarted`(:27)、`AssistantTextDelta`(:35)、`AssistantMessage`(:42)、`ToolApprovalRequested`(:54)、`ToolStarted`(:63)、`ToolFinished`(:75)、`ToolChunk`(:94)、`ModelStepFinished`(:114)、`ContextAssembled`(:138)、`ContextCompacted`(:163)、`Compaction*`(:181-231)、`TaskUpdated`/`LedgerUpdated`/`MemoryInjected`/`InputSteered`/`Degraded`/`TurnFinished` 等。
- 观察者：`SafeObserver` 包裹用户 observer（构造处），异常不伤回合；`server/bridge.py:275 EventBridge` 把 `LoopEvent` 经 `to_wire` 映射成 wire 事件（`text.delta` 由 `on_delta`:294 直接发；`ToolChunk`/`CompactionDelta` 有节流 :301-310；`command_run` 时 `turn.finished` 改写成 `turn.step`:316-317）。
- 前端：IPC notify → `frontend/src/state/session.ts` 的 `reduce` 纯 reducer（我们 4 视角日志里最"事件化"的一层）。

---

## 6. 持久化触点（一回合内）

| 时刻 | 写什么 | 位置 |
|---|---|---|
| 回合前 | git pin pre-turn（stash create → refs/ai-coding/undo/\<turn\>） | agent_loop.py:466 / snapshots |
| 回合前 | 提交 staged undo：清 session.undo_state + sidecar，条件 save（L5） | _forget_redo（:1489 附近） |
| 首个 checkpoint 起 | **整会话文件**（tmp + `os.replace` 原子写）；带 `openTurn`/`undoState` 键 | turn_close.py:190 → session_service.py:157 |
| 等审批时 | `pending/<id>.json` marker（不 save 会话，防 ensure_tool_pairing 吃掉半批） | dispatch WARN → agent_loop:1137 → session_service.py:120 |
| undo 时 | 消息半边进 `undoState`（随会话文件）+ 文件字节 sidecar（原子写 M2） | agent_loop undo/redo + core/undo_store.py |
| 回合结束 | `closer.close` 一次写盘（清 `openTurn`、digest、usage 行） | agent_loop.py:1043 → turn_close.py:220 |
| 恢复读取 | `load` 派生 `recovered_interrupted`/`pending_approval`；`resume_turn` 续跑 | session_service.py:164 / agent_loop.py:400 |

对照：dsh 是 **JSONL 每事件 append**（200ms 批）；opencode 是 **SQLite 行 + durable 事件同事务**。我们是唯一的"整快照"派——换来了简单和原子，代价是写放大与 crash 窗口（已用 marker/sidecar 针对性补）。

---

## 7. skill 层（三面 + 一政策）

1. **系统提示目录**：`_build_instructions`（agent_loop.py:1765）→ `build_catalog`（skills/catalog.py:82）＋ `superseded_note`（:168，目录变化显式告知）；被禁技能（E-5）不进目录。
2. **skill 工具**：`SkillTool`（tools/skill_tool.py:29，`execute`:77）——模型按名加载，`set_on_loaded` 回调记 `session.loaded_skill`（供 outcome 记账）。
3. **@mention 与推荐（volatile tail）**：`request_assembly.py` 的 `recommended_skills` 块——SOP router 的 `route`/`intervene`（F2 阶段判定 :219、F3 干预 :221-242）、F1 错误恢复缓存（:211-217）、用户 `@skill` 显式加载（:248-250 → `_requested_skills_block`:353，被禁的明说"不要读取"）。
4. 政策：`denied_skill_ids`（skills/policy.py）三面同时生效（目录、mention、工具调用）。

对照：dsh 的目录/`/name` 注入是 **durable user 消息**（可回放）；opencode 目录进 instructions、加载走 skill 工具 + `Skill.Activated` 事件。我们的推荐注入是 **volatile**（每迭代重建、不落 history）——三家唯一。

---

## 8. 压缩（4 层，回合内触点）

- 主动：`assemble` 内 `compactor.compress`（request_assembly.py:139，预算驱动，pin `_current_instruction` :600）。
- 溢出救援：`_model_step` 抛 `EngineError(overflow)` → `compact_now` 一次 + `continue`（agent_loop.py:651-674）——重跑迭代而不是就地重试。
- 手动：`/compact` 命令 → `compact_now`。
- 效果面：`did_compact` 才注入 digest ring（request_builder / `render_working_memory`）；`ContextCompacted` 事件 → 前端上下文表。
- 4 层策略（L4 摘要/L3 落盘/L2 placeholder/L1 snip）为三家最丰富；对照 dsh（surface replace）/opencode（checkpoint 边界 + native）见 04。

---

## 9. Mermaid：全链时序（参与者=真实模块名）

```mermaid
sequenceDiagram
    autonumber
    participant UI as "TUI/CLI"
    participant IPC as "server.handlers (session_prompt:294)"
    participant Loop as "AgentLoop (_run_turn:453)"
    participant Pol as "TurnPolicy (turn_policy.py)"
    participant Asm as "RequestAssembler (assemble:120)"
    participant Eng as "ChatEngine (ai/engine.py:94)"
    participant Dsp as "ToolDispatcher (dispatch_batch:185)"
    participant Ses as "SessionData.messages + SessionService"
    participant Br as "EventBridge (bridge.py:275)"

    UI->>IPC: "session.prompt {text, id}"
    IPC->>Loop: "spawn process_input (382)"
    Loop->>Ses: "_begin_turn: user msg(804) + openTurn(855)"
    loop "while True (496)"
        Loop->>Pol: "decide_next_iteration (503)"
        Pol-->>Loop: "CONTINUE / NUDGE / FINISH"
        Loop->>Asm: "assemble (591): compress(139) + tail(259)"
        Asm-->>Loop: "request + canonical(274)"
        Loop->>Eng: "_model_step: step (1338)"
        Eng-->>Loop: "StepResult (+on_delta → Br)"
        Loop->>Ses: "assistant 落 history (1402)"
        alt "有工具调用"
            Loop->>Dsp: "_run_tools (1151): dispatch_batch (1175)"
            Dsp->>Dsp: "_decide (283): gate→hooks→WARN→approval→execute"
            Dsp-->>Loop: "DispatchOutcome[]"
            Loop->>Ses: "write_back (1201) + checkpoint (1204)"
        else "无工具"
            Loop->>Pol: "decide_after_step (682)"
        end
    end
    Loop->>Ses: "_finish_turn (758): _close_turn → closer.close (一次写盘)"
    Loop-->>Br: "TurnFinished"
    Br-->>UI: "wire events (text.delta/tool.*/turn.finished)"
```

---

## 10. 步骤表（对齐 02/03 的口径）

| # | 步骤 | 调用链（file:line） | in → out | 备注 |
|---|---|---|---|---|
| 1 | IPC 收单 | handlers.py:294 → :255 | `{text,id}` → spawned turn | foreign id 拒绝(:305) |
| 2 | 进回合 | agent_loop.py:382 → :453 | SessionData → Frame | `_turn_lock` 串行 |
| 3 | 回合前 | :466 capture + _forget_redo | git pin / staged 清 | best-effort |
| 4 | 开回合 | :473 → :768 | user 消息入 history(:804) | `openTurn`(:855) 首个 save 落盘 |
| 5 | 决策 | :503 policy（纯函数） | Books → TurnDecision | 我们独有 |
| 6 | 组装 | :591 → assemble:120 | session → Assembled | 压缩:139 + tail:259 + canonical:274 |
| 7 | 模型 | :610 → engine.step:1338 | request → StepResult | on_delta 流式 |
| 8 | 落 assistant | :1402 | StepResult → ChatMessage | 无工具=回合可能结束 |
| 9 | 分发 | :710 → dispatch_batch:185 | ToolCallRef[] → DispatchOutcome[] | 全 ALLOW 才并行(:236) |
| 10 | 单调用 | _decide:283 | _Prepared → (result,denied,halted) | WARN→marker→approval |
| 11 | 回写 | :1201 → checkpoint:1204 | outcomes → tool 消息 + 落盘 | 节流 |
| 12 | 收尾 | :758/926 → _close_turn:1043 | outcome → 会话写盘 | `openTurn` 清(:1064) |
| 13 | 事件 | bridge.py:297 → IPC notify | LoopEvent → wire | 前端 reduce |
| 14 | 恢复 | load:164 / resume_turn:400 | 盘 → 派生事实 | P2/P3/P4 |

---

## 11. 不展开 / 边界

- subagent（`core/subagent.py`）复用 AgentLoop 的链路、memory harvest（`core/memory_turn.py`）细节、SOP router 打分细节：与"对话主回路"数据流无关，不展开。
- 前端渲染（Solid 组件树）只到 reducer 边界。
- 行号会随开发漂移；以代码为准。
