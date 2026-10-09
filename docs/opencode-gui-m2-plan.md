# M2 方案：真实会话（事件桥 + parts 状态模型 + 中断）✅ 已实施

> 分支：`feat/opencode-gui-replica`（接 M1 之后）
> 状态：**已实施并核验**（2026-10-09）
> 基准：OpenCode v1.18.32 会话屏（源码 + 实机）；旧 Textual UI `ai_coding/ui/`（行为基线，M6 才删）
> 原则：最小化、第一性、只改根源；每个 UI 元素对应真实后端能力；库优先；TDD

---

## 验收结果（2026-10-09 实机）

- 真回合全链路：用户块 ┃ / markdown 流式 / 工具卡三态（`✓ 已运行命令 · ls`+预览）/ 推理 `+ Thought` / 审批通知（真实 reason）/ 计划与通知 / 红 turn-end ✅
- Esc 中断：两层取消生效，半成品与 handoff 报告保留，红标记 `已中断（Esc）`，busy 释放，下一轮正常 ✅
- 忙时输入：可编辑、回车忽略、文本保留；`· working` 状态 ✅
- ctrl+t：`+ Thought` ⇄ `- Thought` 全展开（暗淡边框块）✅
- 真实重试事件上屏：`⤷ 重试 [transport] 第 1 次，等 2s：Request timed out.` ✅
- E2E 抓到并修复 1 个真 bug：**scrollbox 内容超屏时 prompt 包裹盒被 flexShrink 压缩**（M0 内容短未暴露）→ 加 `flexShrink={0}`（OC home.tsx 同款修法）
- 计数修正：wire 类型实为 **15 类**（14 种 LoopEvent 映射 + on_delta 的 text.delta），文档旧写 13 是笔误
- 有意裁剪：工具表只移植 verb（`kind` 英文标签旧 UI 仅用于折叠分组，M2 无此概念）；Part 不含 kindLabel

---

## 0. 验收标准

1. Home 输入回车 → Session 屏跑**真实 AgentLoop 回合**：流式文字（markdown）、工具卡三态（spinner/✓/✗ + 真实动词/目标）、推理折叠行、计划块、turn 结束标记
2. Esc 中断运行中的回合：工具批取消 + 模型调用取消 → 红色 `已中断` 标记 + 半成品保留 + 输入框恢复可用；下一轮正常
3. 忙时输入可编辑、回车被忽略且**文本保留**（用户已定）
4. `ctrl+t` 全局切换推理显示/隐藏（OC 的 hide/show 语义）
5. 与 OpenCode 会话屏同尺寸截图结构一致（140×42）；`pytest`/`ruff`/`mypy`/`bun test`/`tsc` 全绿
6. 不回归：后端既有 165+ 测试文件全绿

---

## 1. 范围

### 做什么（M2）
- Python：新事件桥 `server/bridge.py`（LoopEvent → 14 类映射 + text.delta = 15 类 wire 事件）；`session.prompt` 真接 `AgentLoop.process_input`；`session.interrupt` 真中断
- TS：`SessionState{messages[].parts[]}` 纯 reducer；会话屏按 parts 渲染（user 块 / markdown 文本 / 推理 / 工具卡 / 计划 / 通知行 / turn 结束）；Esc 中断；ctrl+t 推理开关
- 测试：Python 桥+handlers+socket 集成；TS reducer+toolmeta 纯逻辑；tmux E2E 截图对比

### 不做什么（明确延后，各自有理由）
| 项 | 归属 | 理由 |
|----|------|------|
| 输入队列 / steering | M5 | 用户已拍板：忙时忽略提交 |
| 审批面板（双向 IPC 请求） | M5 | 保持 `AutoApproveCallback`（合法非交互模式）；`approval.requested` 先以通知行暴露 |
| 历史重放（session.load → 转录） | M3 | 无用户路径（每次 `initialize` 新建会话）；转换器与会话对话框一次做完 |
| 命令面板 / slash 命令 | M3 | keymap 层已有，palette 宿主是 M3 |
| 遥测类事件上屏（IterationStarted/ContextAssembled/ModelStepFinished/InputSteered/Memory×4/Degraded/ScopedEvent 其余内层） | M3 `/trace` 对话框 | 旧 UI 也从不上屏这些；/trace 读 Trace JSONL 比事件流更适合 |
| 推理摘要 / 时长（OC 折叠行里的 `: 摘要 · 2s`） | M5+ | 后端无此数据（reasoning 无流式、无时间戳），不造 |

### 4 个有意偏差（相对旧 UI，逐个有理由；请拍板）
| # | 偏差 | 理由 |
|---|------|------|
| D1 | `TurnOutcome.handoff` 渲染为 assistant 文案 | 真实数据（plan §1.1）；REPL 已展示，旧 Textual 丢弃了它是遗漏；`process_input` 返回值就是 `text or handoff` |
| D2 | `ToolApprovalRequested` → 暗淡通知行「已自动批准（面板 M5）」 | M2 是 AutoApprove，安全门静默执行是遗漏；M5 面板落地后此通知由面板取代 |
| D3 | `TurnFinished(status=ERROR)` → 红色标记（含 reason） | 失败回合不能读作「正常结束」；旧 blocks.py 未映射 ERROR 是遗漏 |
| D4 | `ContextCompacted` → 暗淡通知行「上下文已压缩 N→M 条」 | 历史被重写必须可见（项目自身「降级必须可见」哲学）；旧 UI 不显示是遗漏 |

以上 4 条都是**后端已有事件 + 一行渲染**，成本极小；若拍板砍掉，删对应 1 个 mapping + 1 个用例即可。

---

## 2. 现状与复用契约（全部已核实，非记忆）

### 后端（被复用的既有设计）
| 契约 | 位置 | 已核实内容 |
|------|------|-----------|
| 回合入口 | `core/agent_loop.py:314` | `async process_input(session, user_input, on_delta: DeltaSink|None, observer: LoopObserver|None) -> str`；`DeltaSink = Callable[[str], None]`；返回 `outcome.text or outcome.handoff`（:767） |
| 回合串行 | 同上 `_turn_lock` | 第二次调用**等待**不报错（注释：TUI 排队语义）；我们前端忙时不发，此锁兜竞态 |
| 观察者替换 | `_bind_observer` (:852) | 传入 per-call observer 会替换 `loop.observer`，自动 braid 上 trace 与 ToolObserver（`Braid(SafeObserver(base), SafeObserver(trace), tool_obs)`）→ **桥就是普通 LoopObserver，不需要改 loop** |
| 事件定义 | `core/events.py` | 24 种 `LoopEvent`；本轮上屏 13 种（见 §4） |
| 中断 | `agent_loop.py:570` | `except asyncio.CancelledError:` → `_close_turn(aborted_outcome)`（持久化半成品）→ `re-raise`；TurnFinished(ABORTED) 在 re-raise 前由 reporter 发出（`turn_report.py:203`） |
| cancel 快路径 | `agent_loop.py:932` | 每个工具批**前** `self.cancel.clear()`；批内检查 set 即 `cancelled_by_user` → 一次 Esc 只丢一个批，不清空整个回合 |
| 审批 | `security/approval.py:108` | `AutoApproveCallback.approve → YES`（永不 ALWAYS=永不产生 grant）；硬拒 DENY 在 dispatcher 层先拦，auto 不会放宽 |
| 审批事件先于回调 | `core/dispatch.py:409` | 无论回调死活，`ToolApprovalRequested` 都先 emit → M2 通知行有真数据 |
| turn 结束 guarantee | `core/turn_close.py` | 所有路径（含 cancel）经 `finalize` → reporter 发 `TurnFinished`；handoff 在 finalize 前填好（:221） |
| 计划块 | `domain/task.py:322` | `TaskState.render_display()`（无 `<task-state>` 信封，纯展示）；`done_count`/`total` |
| 账本 | `domain/ledger.py:27` | `UncertaintyItem{id, question, level, evidence}` |
| 旧渲染表 | `ui/blocks.py` | 动词/类目标（`_TOOL_CARD_META`）、目标提取（`_tool_target`）、输出预览 120 字符、reason 60 字符、子代理 120 字符、chunk 节流 150ms、turn-end 4 分支文案 → **TS 端同值移植** |
| 旧回合接线 | `ui/textual_app.py:364` | `process_input(session, text, on_delta=..., observer=...)`；Esc 两层取消（`:378`）；tpM「50ms 缓冲」是 Textual 渲染需要，我方 Solid 细粒度响应式不需要移植 |

### 前端（现状）
| 契约 | 位置 | 已核实内容 |
|------|------|-----------|
| IPC | `src/ipc.ts:55-70` | `request`/`onEvent`（`{type,data}`）/`onRequest`（M5 审批用，M2 不用） |
| 事件通道 | `server/service.py:141` | `notify("event", {type, data})`；writer 自带锁，asyncio 线程直接调安全 |
| keymap | `src/app.tsx:29` | `useBindings(() => ({commands:[{name,desc,run}], bindings:[{key,cmd}]}))`；组件内可注册（M0 只在 App 层用过） |
| Prompt | `src/component/prompt.tsx` | `onSubmit(text)` 当前先清空再回调 → 需改为「回调返回 false 则不清空」（忙时保文本，用户已拍板的语义需要） |
| markdown/spinner 库 | 已核实 | `@opentui/solid` 内建 `<markdown>` 原语（OC 同款用法：`streaming`、`conceal`、`syntaxStyle`）；`opentui-spinner/solid` 提供 `<spinner>`（`import "opentui-spinner/solid"` 注册）；**零新增依赖** |
| 会话屏现状 | `src/routes/session.tsx` | M0 echo + `ChatLine[]` 简陋模型 → M2 整体换 parts 模型 |

---

## 3. 架构与数据流

```
┌─ Python ──────────────────────────────────────────────────────────────┐
│  session.prompt(handler, 线程池)                                       │
│    └─ service.spawn( run_turn() )  ← Future 句柄存入 _WorldState       │
│         run_turn: loop.process_input(session, text,                   │
│                     on_delta = bridge.on_delta,   ← 文字通道           │
│                     observer = bridge)            ← 事件通道           │
│              ├─ CancelledError → 吞掉（turn.finished 已由 loop 发出）  │
│              └─ 意外异常 → 合成 turn.finished{status:"error"} 兜底     │
│                                                                       │
│  core/events.py (24 种 LoopEvent)                                     │
│    └─ server/bridge.py                                                │
│         to_wire(event) -> {type, data} | None   ← 纯函数，13 类映射     │
│         ToolChunk 节流 150ms/call_id（clock 可注入）                   │
│         └─ notify("event", {type, data})                              │
│                                                                       │
│  session.interrupt(handler):                                          │
│    world.cancel.set()          ← 工具批快路径（loop 每批自清）          │
│    future.cancel() ×未完成      ← 模型调用层（CancelledError 路径）     │
└──────────────────────────────────────────────┬────────────────────────┘
                      Content-Length JSON-RPC 2.0 (vscode-jsonrpc ↔ python-lsp-jsonrpc)
┌─ TS (bun + OpenTUI) ──────────────────────────▼────────────────────────┐
│  IpcClient.onEvent                                                    │
│    └─ reduce(state, event)  ← 纯函数 reducer（bun test 主战场）        │
│         └─ createStore<SessionState>                                  │
│              └─ <For messages> → <MessageView>                        │
│                   user      → ┃ 左边界块（agent 色 + raised bg）        │
│                   text      → <markdown streaming>（paddingLeft 3）   │
│                   reasoning → 「+ Thought」单行（ctrl+t 全局展开）      │
│                   tool      → <ToolCard> spinner/✓/✗ + 动词 + 目标     │
│                   plan      → 「计划 x/y」+ render_display 暗淡块       │
│                   notice    → 暗淡单行（retry/fallback/…）             │
│                   turn-end  → 红行（中断/超时/截断/步数/错误）          │
│  Prompt ← esc 命令 session.interrupt；忙时回车被忽略（文本保留）        │
└───────────────────────────────────────────────────────────────────────┘
```

**状态更新是单向的**：wire 事件（+ 2 个本地动作 `submitted`/`submit-failed`）→ reducer → store → 组件。组件不直接改状态。

**竞态处理（已想清）**：turn 的通知可能与 `session.prompt` 响应乱序到达（两个写线程）。解法=**乐观 busy**：提交时本地立即 `submitted`（busy=true），响应只带 turnId 不作状态；`turn.finished` 清 busy。不造 `turn.started` 事件。

---

## 4. Wire 协议（15 类；名字即契约，bridge 单测逐字段钉死）

| LoopEvent | wire `type` | `data` 字段（→ TS 用途） |
|-----------|-------------|--------------------------|
| `AssistantTextDelta`（经 `on_delta`） | `text.delta` | `{text}` → 追加流式文本 part |
| `AssistantMessage` | `message.finished` | `{text, reasoning}` → text 权威定稿；reasoning 生成推理 part |
| `ToolStarted` | `tool.started` | `{callId, toolName, params, iteration}` → 建运行中工具卡 |
| `ToolChunk`（节流后） | `tool.chunk` | `{callId, stream, text, seq}` → 卡片尾部输出缓冲（cap） |
| `ToolFinished` | `tool.finished` | `{callId, toolName, success, outputPreview, durationMs, error, iteration}` → 卡片定稿 ✓/✗ |
| `ToolApprovalRequested` | `approval.requested` | `{toolName, params, reason}` → D2 通知行 |
| `TaskUpdated` | `plan.updated` | `{display, done, total}`（`render_display()` 结果） → 替换计划 part |
| `LedgerUpdated` | `ledger.updated` | `{items: [{level, question, evidence}]}` → 暗淡通知 |
| `TurnFinished` | `turn.finished` | `{status, reason, iterations, truncatedByLength, handoff}` → busy=false、结束标记、D1 handoff 文案 |
| `ProviderRetried` | `provider.retried` | `{kind, attempt, delaySeconds, reason}` → 通知行 |
| `ProviderFallback` | `provider.fallback` | `{fromModel, toModel, kind, reason}` → 通知行 |
| `ToolSurfaceWarning` | `tools.warning` | `{toolsChars, threshold, origins}` → 通知行 |
| `BackgroundTaskDone` | `background.done` | `{runId, name, status, summary, truncated, originSession, ledgerPath}` → 通知行 |
| `ScopedEvent` | `subagent.event` | `{scope, inner}`；inner 仅 3 种：`{kind:"tool", phase:"started"/"finished", toolName, params?/success?, preview?}`、`{kind:"text", text}` → 暗淡 `↳ [scope] …` 行 |
| `ContextCompacted` | `context.compacted` | `{beforeMessages, afterMessages}` → D4 通知行 |

**不上 wire（返回 None，逐类测试）**：`IterationStarted`、`ContextAssembled`、`ModelStepFinished`、`Degraded`、`InputSteered`、`MemoryInjected/Recalled/Harvested/Consolidated`、ScopedEvent 其余内层。
**截断纪律沿用旧值**：reason 200 字符（loop 已截）/ 60（UI）、outputPreview 500（event 已截）/ 120（UI）、子代理 120、证据逐条 items。

命名规范：wire 一律 camelCase 字段（与既有 M0/M1 的 `turnId`/`sessionId` 一致）；Python 侧映射处单点转换。

---

## 5. TS 状态模型与 reducer 规则

```ts
type Part =
  | { kind: "text"; text: string; streaming: boolean }
  | { kind: "reasoning"; text: string }
  | { kind: "tool"; callId; toolName; verb; kindLabel; target;
      status: "running"|"ok"|"failed"; tail: string; preview: string; error: string }
  | { kind: "plan"; title: string; body: string }        // title="计划 x/y"
  | { kind: "notice"; tone: "muted"|"error"; text: string }
  | { kind: "turn-end"; tone: "error"; text: string }
interface SessionState { messages: { role:"user"|"assistant"; parts: Part[] }[]; busy: boolean }
```

reducer 规则（每条一个用例）：
1. `text.delta`：末条 assistant 消息末 part 是 `streaming:true` 的 text → 追加；否则新开 text part（多步回合天然分段）；无 assistant 消息则先建
2. `message.finished`：`text` 非 null → 用权威文本**替换**当前 streaming text（丢了 delta 也不怕）并 `streaming:false`；`text` 为 null 且存在 streaming part → 仅定稿；`reasoning` 非空 → 追加 reasoning part
3. `tool.started`：按 `callId` 建 running 卡；verb/kindLabel/target 由 `toolmeta.ts` 从 params 派生（旧表同值：`bash→(Shell,运行命令)` 等）
4. `tool.chunk`：按 callId 追加 tail（保留末 4000 字符，running 时显示末 3 行 `│` 前缀）
5. `tool.finished`：更新状态/预览/错误；**未知 callId → 仍建已结算卡**（丢 start 也不丢结果）
6. `plan.updated`：当前回合内单例——存在则替换，否则追加
7. `ledger.updated`：逐条 `[level] question` + 证据缩进，dim 通知
8. `approval.requested` / `provider.retried` / `provider.fallback` / `tools.warning` / `background.done` / `context.compacted` / `subagent.event`：拼旧文案（blocks.py 同格式）dim 通知
9. `turn.finished`：定稿所有 streaming part；`handoff` 非空 → 追加 text part；状态分支同旧 4 条红文案 + D3 ERROR 红行；busy=false
10. 本地 `submitted{text}`：追加 user 消息 + busy=true；`submit-failed{reason}`：busy=false + error 通知
11. 通知类事件在无活跃 assistant 消息时：挂到最后一条 assistant 消息（BackgroundTaskDone 可能跨轮到达）；没有任何消息则新建一条 assistant

组件内 UI 状态（不进 reducer）：`thinkingMode: "hide"|"show"`（Session 内 signal，默认 hide=OC 默认）。

---

## 6. 每文件改动

### 6.1 Python

**NEW `ai_coding/server/bridge.py`（~110 行）**
- `to_wire(event) -> dict|None`：纯函数，§4 表（13 类 + 丢弃清单 + ScopedEvent 3 内层）
- `class EventBridge`：`on_event`（调 `to_wire`→sink）、`on_delta`（直接 `text.delta`）、ToolChunk 节流（`_last_chunk: dict[callId, float]`，clock 注入，同旧 150ms）
- 契约：构造函数收 `notify: Callable[[str, dict], None]`；不 import service，可离线测

**EDIT `ai_coding/server/handlers.py`（+~55 −15）**
- `_WorldState`：+`session`（initialize/session_create/session_load 时保存对象引用）、+`turn_futures: set[Future]`
- `make_handlers`：顶部构 `bridge = EventBridge(lambda t, d: service.notify(t, d))`
- `session_prompt`：`run()` 协程 = `process_input(state.session, text, on_delta=bridge.on_delta, observer=bridge)`；`except CancelledError: pass`（loop 已发 turn.finished）；`except Exception as exc:` 合成 `turn.finished{status:"error", reason}`（防 bug 卡死 UI）；`future = service.spawn(run())`；登记 futures，done 回调里移除；返回 `{turnId, sessionId}` 不变
- `session_interrupt`：`world.cancel.set()` + 对所有未 done 的 future `.cancel()`；返回 `{ok, interrupted: n}`；删掉旧的 set+clear 假实现

**EDIT `ai_coding/server/service.py`（+1）**
- `spawn()` 返回 `run_coroutine_threadsafe` 的 future（既有调用方忽略返回值，向后兼容）

**NEW `tests/test_server_bridge.py`（~16 用例）**：见 §7
**EDIT `tests/test_server_ipc.py`（+~4 用例，改 `_FakeLoop`）**：`_FakeLoop.process_input` 变可脚本化（依次调 `on_delta`/`observer.on_event`）；新增 socket 级真实链路测试 + 中断测试；旧 echo 用例改为脚本化序列

### 6.2 TypeScript

**NEW `frontend/src/util/toolmeta.ts`（~70）**：`toolCardMeta(name)`、`toolTarget(name, params)`、`toolTitle(verb, target, running)` —— blocks.py 同值移植（含未知工具 fallback `(name, "运行工具")`）

**NEW `frontend/src/state/session.ts`（~200）**：§5 类型 + `initialState()` + `reduce(state, action)` 纯函数（action = wire 事件 | 本地动作）；导出给测试；不放任何 Solid 依赖

**NEW `frontend/src/component/toolcard.tsx`（~80）**：`<spinner>`/✓/✗ + `verb · target`；running 显示 tail 末 3 行；结算显示 120 字 dim preview；失败红 error 行。结构参照 OC `BlockTool`（左边界卡+raised bg）

**NEW `frontend/src/component/message.tsx`（~110）**：user 块（M0 已有样式）；assistant 按 part 分派：`<markdown>`（`streaming` 绑定 text part 状态）/ reasoning（`<Reasoning>`）/ toolcard / plan / notice / turn-end

**NEW `frontend/src/component/reasoning.tsx`（~50）**：hide=`+ Thought` 单行（warning 色调、dim）；show=左边界 dim markdown 块。无「Thinking」spinner（无流式推理数据，不造）

**EDIT `frontend/src/routes/session.tsx`（重写转录部分，~±120）**：`createStore` + `reduce` 订阅；`useBindings` 注册 `session.interrupt`(esc)、`session.toggle.thinking`(ctrl+t)；`submit` 忙时返回 false；去掉手动 `scrollTo`（交给 `stickyScroll`，E2E 验证）

**EDIT `frontend/src/component/prompt.tsx`（~3）**：`onSubmit: (text) => boolean|void`；返回 `false` → 不清空输入

**EDIT `frontend/src/routes/home.tsx`（1）**：onSubmit 返回 true（签名适配）
**EDIT `frontend/index.tsx`（1）**：`import "opentui-spinner/solid"`（全局注册一次）

**NEW `frontend/test/reducer.test.ts`（~18 用例）+ `frontend/test/toolmeta.test.ts`（~8 用例）**：见 §7

### 明确不碰
`core/agent_loop.py`、`core/events.py`、`core/dispatch.py`、`server/world.py`（AutoApprove 保持）、`ui/`（M6 删）、`domain/`、任何会话持久化逻辑。**零新增依赖。**

---

## 7. TDD 用例清单（先写测试→红→实现→绿）

### tests/test_server_bridge.py（16）
1. `to_wire` 逐类映射 = 精确 dict（13 条各 1 用例，含 TurnFinished.aborted 的 status 序列化 "aborted"）
2. 丢弃清单返回 None（参数化 8 条）
3. `ToolChunk` 节流：同 callId 两次间隔 0.1s → 只发 1（注入 clock）；间隔 0.2s → 发 2
4. `ScopedEvent` 内层 3 种映射；内层 `ToolChunk` → None
5. `on_delta` → 精确 `{"type":"text.delta","data":{"text":...}}`
6. `EventBridge` sink 契约：notify 收到 `("event", {...})`

### tests/test_server_ipc.py（改 2 + 新增 4）
7. `session.prompt` 经真实 socket：脚本回合一（2 delta + tool.started + tool.finished + turn.finished）→ 客户端 events 序列 = 精确 wire 序列
8. `session.prompt` 把 `on_delta` 与 observer 都接到 bridge（fake loop 记录参数）
9. interrupt：slow fake loop（`await asyncio.sleep(30)`）→ 请求 `session.interrupt` → future 取消 + `world.cancel` set + 无异常泄漏
10. interrupt 无运行回合 → `{ok:true, interrupted:0}`（不炸）
11. bug 兜底：fake loop raise RuntimeError → 客户端收到合成 `turn.finished{status:"error"}`
12. 旧 echo 用例改造（test_session_prompt_echoes_events → 脚本序列）

### frontend/test/toolmeta.test.ts（8）
13. `toolCardMeta` 表内 4 例（bash/read/edit/未知 MCP 名）+ `toolTarget` 3 例（glob pattern/路径/命令 60 截断）+ `toolTitle` running/done 文案

### frontend/test/reducer.test.ts（18）
14. delta 追加 / 新步骤开新 text part / 无消息时自动建
15. message.finished 权威替换 + streaming 定稿 + reasoning 落 part
16. tool.started 派生 verb/target/kindLabel；tool.finished 更新同卡；未知 callId 建已结算卡
17. tool.chunk tail cap 4000（超长截尾）；plan 单例替换；ledger 多行
18. 6 类通知文案（retry/fallback/warning/background/compacted/subagent）
19. approval.requested 通知；turn.finished completed（无标记，busy false）+ handoff 追加
20. turn.finished aborted/timeout/max_iterations/truncated/error 红文案（5 分支）
21. 忙时本地 submitted→submit-failed 序列；跨轮 background.done 挂末条 assistant

### E2E（tmux，人工核对 + 截图）
22. 真 prompt：流式 markdown → 工具卡 → 计划 → 完成；140×42 对比 OC 会话屏
23. Esc 中断中回合：红标记、半成品保留、busy 释放、下一轮正常；ctrl+t 展开/收起推理

---

## 8. 方案自检（假设 → 验证方式 → 回退）

| # | 假设 | 验证 | 回退 |
|---|------|------|------|
| A1 | per-call observer 替换后 trace 仍到达（`_bind_observer` 自动 braid） | 读码已核实 + 用例 7 | 无需回退 |
| A2 | `run_coroutine_threadsafe` 的 Future `.cancel()` 能从 handler 线程取消协程 | 用例 9 | service 增 `schedule()->Task`（`call_soon_threadsafe` 取回 Task），1 处改动 |
| A3 | `future.cancel()` 取消排队中的（锁等待）第二个回合 | 用例 9 变体 | 可接受：等锁的上一个跑完才取消 |
| A4 | `<markdown streaming>` 在 0.5.17 可用且流式不卡 | 组件落地后 tmux 实测 | 降级 `<code filetype="markdown">`（OC 也用） |
| A5 | keymap 的 esc 能在 textarea 聚焦时拦截 | tmux 实测 | textarea `onKeyDown` 里处理 |
| A6 | `stickyScroll` 自动跟底、用户上滚不打架 | tmux 实测 | 仅 `turn.finished` 后 `scrollTo(bottom)` |
| A7 | `opentui-spinner/solid` 与 bun preload 兼容 | 落地首日 smoke | OC 同款 `<spinner>` 自绘帧（cli-spinners 数据已有） |
| A8 | TurnFinished 的 outcome 已含 handoff（finalize 在 replace 之后） | 用例 7 断言 handoff 字段 + 读码已确认顺序 | 无 |
| A9 | 事件/响应乱序不发生卡 busy | 乐观 busy 已设计 | 已消除 |

**最终模样走查**（实施完打开会看到什么）：Home 回车 → 左侧 ┃ 用户块；assistant markdown 流式增长；工具卡 spinner→✓；推理折叠行；红「已中断」；忙时输入框可敲、回车无动作、回合一结束回车即发；ctrl+t 推理全展开；140×42 下结构与 OC 会话屏一致（user 块/缩进/卡片/提示行）。

**复杂度盘点**：Python ~3 文件改动 +1 新模块；TS 2 个纯逻辑新模块（测试主场）+ 3 个组件 + 2 处小改；无 schema 校验层（zod 已装但 M2 不引入——两端同仓库、bridge/reducer 双侧测试已钉协议，E2E 兜底；M3 对话框数据再启用）。

---

## 9. 被否方案与砍掉的东西（记录）

| 项 | 否决理由 |
|----|---------|
| 方案 2：后端压 Block 行流给前端 | markdown/diff/展开/三态全做不了，样式决策回流 Python，与 OC 复刻目标冲突 |
| 方案 3：事件行追加不建 messages | 工具卡按 callId 回查行、推理展开回查行——最终退化成 parts，多绕一圈 |
| 合成 `turn.started` 事件 | 乐观 busy 已解决乱序，多一个事件=多一处可不同步 |
| zod 运行时校验层 | 两端同仓库 + 双侧单测钉死形状；冗余，M3 启用 |
| 推理「Thinking」运行态 spinner | 后端无流式推理/时间戳数据，不造 |
| 推理摘要 + 时长 | 同上 |
| 历史重放转换器 | 无用户路径，M3 与会话对话框一起做 |
| 输入队列 | 用户已拍板：忙时忽略提交、文本保留 |

## 10. 实施顺序

1. bridge.py（用例 1-6 红→绿）→ handlers.py（用例 7-12）→ `pytest`+`ruff`+`mypy` 全绿
2. toolmeta.ts（用例 13）→ state/session.ts（用例 14-21）→ `bun test` 绿
3. 组件与接线（toolcard/message/reasoning/session/prompt/index）→ `tsc` 绿
4. tmux E2E（用例 22-23）对照 OC 截图；计划文档勾状态；提交

预计：1.5–2 个专注日（与 plan 的 M2 估算一致）。
