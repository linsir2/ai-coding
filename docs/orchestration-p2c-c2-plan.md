# P2-C2 实施方案（评审修订稿）：任务日志、claim/lease/fence、取消级联与恢复

> 前置：P2-C1 已提交（`67c53ae`，评审修复 `f889b74`）。总体方案
> `docs/orchestration-p2c-plan.md`。本稿已吸收 C2 方案评审 P0/P1：
> 取消终态、fence 覆盖、前台任务可寻址、恢复误判、journal 单写者/半行、
> reaper 生命周期、needs_reconciliation 可见性、幂等键与多 turn 日志、影响面/测试。

## 0. 目标

1. `TaskJournal`：任务级 append-only JSONL；同步 append + 线程锁；半行修复。
2. claim/lease/fence：任务代次；取消后晚到结果不得覆盖；每 turn 注入 fence。
3. 取消级联：`cancel_tree`（停止）/`close_tree`（关闭）；Esc 不变；`stop` 终态；
   `interrupt` 可续。
4. 恢复：启动扫描全部 session journal；未结算 → `needs_reconciliation`，不重跑。
5. 幂等：`spawn`/`send`/`fanout` 键 + payload digest。
6. 单 reaper：续租/巡检；**不用执行队列**，但每个 child/fanout 调用有可取消的
   `asyncio.Task` 句柄（前台任务可寻址）。

## 1. 现状（关键行号）

| # | 事实 | 证据 |
|---|---|---|
| J1 | Scheduler 只有内存视图；无持久任务记录 | `core/scheduler.py:32-64`、`core/task_registry.py:104-149` |
| J2 | 结算点：`_finish_background`/`_reap`/follow-up/interrupt/close | `core/subagent.py:1346,1453,803,882,1015` |
| J3 | runner 有 `scheduler` 引用（builder 设置） | `tools/builder.py:496-503`、`core/subagent.py:281` |
| J4 | `lastStatus` 默认 completed；spawn 时 meta 已写但未跑 | `core/subagent.py:180,648,1962` |
| J5 | `cancel` 只取消 task，handle 随后 idle，mailbox 可再开 turn | `core/subagent.py:685-692,1276-1295` |
| J6 | `stop` 只是 `runner.cancel`；不写 journal/fence | `core/scheduler.py:256-258` |
| J7 | 同步/fanout 直接在调用者 task 里执行，无任务句柄 | `core/scheduler.py:72-104,155-213` |

## 2. 设计

### 2.1 `TaskJournal`（`core/task_journal.py`，新）

- 路径 `<workspace>/.ai-coding/tasks/<session_id>.jsonl`（`_orphan` 兜底）。
  与 `.ai-coding/agents` 是兄弟目录，不冲突。
- 行：`{"v":1,"event":"queued|claimed|renewed|running|settled|cancelled|stopped|
  interrupted|closed|recovered","task_id","parent_id","kind","state",
  "claim":{owner,lease_until,fence},"session_id","run_id","turn","key","digest",
  "branch","commit","error","at"}`。
- **同步 append + `threading.Lock`**：runner 的 `_finish_background`/`_reap` 是同步
  函数，不能 await asyncio.Lock；单进程内用线程锁。append 前若文件末字节不是
  `\n` 先补一个，避免崩溃残行与下一行拼接。
- 读：跳过坏行/残行；按 `task_id` 折叠，最后事件决定状态。
- 只记任务级事实；文件写仍归 `write_ahead`。

### 2.2 claim / lease / fence

- `TaskRecord`：`task_id,parent_id,kind,state,revision,claim{owner,lease_until,
  fence},cancel_epoch,turn,key,digest,request`。
- 提交时同步 `_claim`：锁内 `fence += 1`，`lease_until = now + ttl`；
  `ttl = 2 * (runner.timeout_seconds or 1200)`，最小 60s。
- **fence 存 record，并随每个 turn 注入**：
  - `Scheduler.run/spawn` 把 `task_id/fence` 传给 runner/handle；
  - runner 在 `_run_turn` 开始时 `task_fence.set(handle.fence)`；结算回调带
    `task_id` + 本次 turn 开始的 fence；
  - **结算门先于 row/notice**：`_finish_background`/`_reap` 在更新 row、发 notice、
    写 Registry 之前先调 `scheduler.accepts(task_id, fence)`；返回 False 直接 return
    （取消后的晚到结果既不覆盖 row，也不发通知）。stop 自己写终态 row。
  - `Scheduler._settle(task_id, fence, ...)` 只有 fence 匹配才写 journal/Registry；
    取消会 `fence += 1`。
- 续租：`TaskReaper` 每 `ttl/3` 对 open 记录写 `renewed`、推进 lease；**不**取消活任务。
- 不引入写租约；写仍由 P2-B claim 管。

### 2.3 任务句柄与单一 reaper（不用执行队列）

- `Scheduler` 为每个 child/fanout 调用保存 `asyncio.Task`：
  - `run`：`asyncio.create_task(_execute_child(...))`，`await` 它；父取消时
    `except CancelledError: task.cancel(); await gather; raise`；任务可 `cancel_tree` 寻址。
  - `spawn`：runner 自己持 supervised task；Scheduler 记录 `run_id`；句柄来自 runner。
  - `fanout`：整次调用一个 task，并登记一个 `kind="fanout"` 的父记录
    `id=token`（`token = parent_span or uuid`）；每个问题记录 `parent_id=token`。
    **按问题取消不支持**（ResearchFanOut 不暴露子 task），`cancel_tree(token)`
    取消整次 fanout；文档写明。
  - `parent_id` 来源：`ContextVar("current_task_id")`。`run/spawn` 提交时取当前值
    （顶层为空）；`fanout` 执行期间把它设为 fanout token，所以问题记录
    `parent_id=token`，token 的父记录 `parent_id=调用时的值`。
    **不用 `parent_span`**（那只是 trace 的 call id，不是任务链）。
    `cancel_tree(root)` 按 `parent_id` 链找子。
- `TaskReaper`：一个 asyncio task，懒启动（第一次 claim），`aclose()` 停止；
  `try/except` 隔离单次巡检异常；与 builder/world 生命周期同处接入。
- **为什么不用执行队列**：C1 的 `run` 在 claim 后立即执行；没有“排队未领取”状态。
  任务可寻址靠上面的 Task 句柄，不需要队列。C3 合并队列自己会是队列。
- 该偏离记录：总体方案写“队列+driver”，C2 用“任务句柄 + 单 reaper”。

### 2.4 取消 / 停止 / 关闭（终态语义）

- 先修 runner 的根因：新增 `stop_run(run_id)`：
  - 取消当前 task；**清空 mailbox**；置 `handle.stopped=True`（终态闩锁）；
    关闭 loop（`handle.loop=None`）；
  - 在 `_supervised`/`_run_followup` 的所有 finally 分支先查 `handle.stopped`：
    stopped 时不再置 idle/interrupted、不再调度 mailbox；
  - `send_message`/`_schedule_turn`/自动 follow-up 对 stopped 一律返回 `stopped`；
    `resume`（含跨进程）先查 Scheduler journal：终态 → `stopped`，不重建；
  - stop 自己把 row 置 `cancelled`、写 journal `stopped`、fence += 1、
    `_release_slot` 幂等；之后 `_supervised` 的收尾因 row 非 running 直接返回。
- 语义：
  - `interrupt`：只切当前 turn，可 send/resume（现状不变）；
  - `stop`：kill run，终态；`/tasks stop` 用它；
  - `close`：关 loop、移出 handles，可显式 resume（现状）；
  - `cancel_tree`：对所有子调用 `stop`；`close_tree`：对所有子调用 `close`。
- `cancel_tree(root_id)` 从 TaskRecord 的 `parent_id` 找子；fanout 子以 fanout token
  为 parent；**只对已登记任务**；同步 child 用其 Task 句柄取消；后台用 run_id。
- `CloseAgentTool` 改 `close_tree`；Esc 不变（只取消当前 turn，不杀后台子）。

### 2.5 恢复

- `Scheduler.recover()` 由 builder 组装后调用一次；扫描
  `<ws>/.ai-coding/tasks/*.jsonl`。
- 折叠每个 task：
  - 终态：`settled/cancelled/stopped/closed` → 跳过；
  - `interrupted` 是**可恢复的非终态** → 视为 open；
  - `settled` 之后若还有 `pending`（mailbox 里还有 follow-up 消息）→ 视为 open；
    runner 先写 `settled`，再写 `pending`（顺序不能反，否则折叠按最后事件仍判终态）；
    下一次 turn 开始写 `running` 即可清除 pending 语义（最后事件变 running）；
  - 其他 open → 登记 `TaskView(status="needs_reconciliation",
    needs_reconciliation=True, kind="recovered")`；写 journal `recovered`（不写终态）。
  - **不重跑、不重建 loop、不重新 spawn**；保留 session/run/branch/commit。
- **不读 meta 判终态**：spawn 时 meta 已写且 `lastStatus` 默认 completed，不可信；
  journal 是唯一结算依据。崩溃在“工作完成、journal 未 settled”之间 → open
  （needs_reconciliation），这是有意的保守。
- 幂等：同一 task 再次 recover 仍产出同一 view（按 task_id），不重复登记；
  不把 `recovered` 当终态，避免二次重启跳过未核对任务。
- `needs_reconciliation` 进入 `TaskView`/`to_dict`/`merge`；`/tasks` 显示“需核对”；
  `list_agents`/`subagent_list` 不过滤掉 `kind="recovered"`，行显示“需核对”。

### 2.6 幂等键

- `TaskRequest` 增 `idempotency_key: str = ""`、`payload_digest: str = ""`。
  digest 必须**排除随机 id**：
  - spawn：`sha256(json.dumps(task.to_dict(), sort_keys=True))`；
  - send：`sha256(message)`；
  - fanout：每问题 `sha256(question.question + scope + shared_context)`。
- Scheduler 内存表 `key -> {task_id,digest,request_id,run_id,result/packets}`；
  空键跳过（不误合并）。
- `spawn`：键 = 父 `ctx.call_id`；同键同 digest → 返回已有 ids；异 digest →
  `IdempotencyConflict`（工具映射 `conflict`）。
- `send`：Scheduler.send 增 `key`；同键同 digest 不重复发送；异 digest → conflict。
- `fanout`：每问题键 = `call_id + ":" + question.id`；同键同 digest 返回**已有 packets**
  （不是仅 view），异 digest → conflict。
- 重启后内存表丢失：recover 从 journal 的 `key/digest/run_id` 重建映射；
  未终结的同键返回 `needs_reconciliation`（不重跑）。
- merge 键留 C3。

## 3. 数据流

```
Scheduler.run/spawn/fanout
  → journal.queued(key,digest) → _claim(fence) → journal.claimed/running
  → child task / runner / fanout
  → 每个 turn start：runner 写 journal.running(turn,fence)
  → _settle(task_id,fence,result) → journal.settled + Registry
  → stop/cancel_tree：runner.stop_run（清 mailbox、终态闩锁、fence++）
                     → journal.stopped → 释放槽
  → 重启：recover() → journal 折叠 → 终态跳过；open/interrupted →
                     needs_reconciliation view（不重跑）

runner 回调（_finish_background/_reap/interrupt/close）
  → scheduler.on_runner_event(task_id, run_id, status, fence)
  → journal + Registry（fence 不匹配则丢弃）
```

## 4. 逐文件改动

| 文件 | 改动 |
|---|---|
| `core/task_journal.py`（新） | 同步 append（线程锁、补换行）、fold/recover、半行容错 |
| `core/scheduler.py` | TaskRecord/claim/fence、Task 句柄、TaskReaper、stop/cancel_tree/close_tree、recover、幂等表、runner 回调、start/stop reaper |
| `core/subagent.py` | `stop_run`（清 mailbox、stopped 闩锁、关 loop、拒绝 send/resume/follow-up）；所有 finally 分支先查 stopped；`_run_turn` 注入 fence + 写 running；结算前 `pending`；`accepts/on_settle/on_turn_start`；handle 增 task_id/fence/stopped |
| `domain/orchestration.py` | `idempotency_key`、`payload_digest`、`parent_id` |
| `core/task_registry.py` | `needs_reconciliation` 字段、`merge` 保留、保护不被剪 |
| `tools/builder.py`、`cli.py`、`server/world.py` | 构造 journal、`scheduler.recover()`、reaper 生命周期（world/loop aclose） |
| `tools/agents_tool.py` | `CloseAgentTool` → `close_tree`；send 传 key；list 显示“需核对” |
| `tools/subagent_tool.py` | spawn 传 key；list 显示“需核对” |
| `tools/parallel_research_tool.py` | fanout 传 origin_session + key |
| `app/commands.py` | `/tasks` 显示“需核对”；stop 走 `Scheduler.stop` |
| `server/bridge.py`、前端 | `needsReconciliation` 字段（可选展示） |
| `docs/data-contracts.md` | journal/TaskView/runner 回调 |

**不改**：槽池/occupies_slot、P2-A packet、P2-B claim/hold/notice、Esc 语义。

## 5. RIPR 用例

| # | 文件 | 用例 | 期望 |
|---|---|---|---|
| R1 | `tests/test_task_journal.py`（新） | append/fold/半行/补换行 | 坏行跳过；下一行可读 |
| R2 | `tests/test_scheduler.py` | 取消后晚到 `_settle` | fence 不匹配 → 不写 |
| R3 | 同上 | 短 ttl 续租 | `renewed` 出现；不取消活任务 |
| R4 | `tests/test_cancel_cascade.py`（新） | cancel_tree 两级 fake、幂等、终态不倒退、Esc 不变、stop≠interrupt | — |
| R5 | `tests/test_idempotency.py`（新） | spawn/send/fanout 同键同/异 digest；空键 | 不重复；异内容 conflict |
| R6 | `tests/test_recovery.py`（新） | open/interrupted、二次 recover、meta completed 不误判 | needs_reconciliation；不重跑 |
| R7 | `tests/test_task_registry.py` | needsReconciliation 字段/三读口/不被剪 | — |
| R8 | `tests/test_subagent_runner.py` | stop_run 清 mailbox、拒绝 send/resume、finally 不改 idle、on_settle 一次 | — |
| R9 | `tests/test_agent_messages.py`/`test_boot_resume.py` | send key、boot recover | — |
| R10 | `tests/test_subagent_runner.py` | 晚到结算：stop 后 `_finish_background` 不改 row/不发 notice | — |
| R11 | `tests/test_task_journal.py` | mailbox pending：settled 后 `pending` 让 recover 视为 open | — |
| R12 | `tests/test_scheduler.py` | `run` 包裹 Task 后父取消传播、cancel_tree 能取消同步 child | — |

### P

- 全量 pytest 0 failed；ruff/mypy；前端（若改）。
- 突变：去掉 fence 校验 → R2 红；去掉 recover → R6 红；去掉幂等表 → R5 红；
  cancel_tree 不递归 → R4 红；append 不补换行 → R1 红。

## 6. 风险

1. **两本账**：journal 只任务；文件写归 write_ahead。
2. **runner 回调**：`on_settle` 只在 row 真正结算（row-live 判断之后）调用一次；
   `on_turn_start` 每 turn 一次；scheduler=None 静默；避免与 P2-B 释放/NoticeStore 冲突。
3. **恢复保守**：journal 唯一结算依据；工作完成但未 settled 会标需核对，不自动重跑。
4. **stop 语义变化**：stop 会清 mailbox 并终态；interrupt/close 保持可续；文档与
   工具描述同步更新。
5. **fence 注入**：sync/background/follow-up 都要注入；fanout 只支持整次取消。
6. **不做执行队列**：任务可寻址靠 Task 句柄；偏离记录在案。
7. **journal 路径**：`.ai-coding/tasks` 与 `.ai-coding/agents` 并列；走 `paths.py`
   新增属性统一（若已有路径模块）。

## 7. 实施记录（完成）

- **TaskJournal**：`core/task_journal.py`（同步 append + 线程锁、补换行、坏行跳过、
  按 task 折叠；`pending` 晚于 `settled` 视为 open）。
- **claim/lease/fence**：Scheduler `_Record` 保存 fence/lease；`accepts()` 在
  `_finish_background`/`_reap` 更新 row/notice **之前**拦截晚到结果；`on_turn_start`
  每 turn 写 `running`；`TaskReaper` 懒启动，`ttl/3` 续租，单 pass 异常不杀 driver。
- **任务句柄**：`Scheduler.run/fanout` 用 `asyncio.create_task` 持有可取消句柄；
  `cancel_tree/close_tree` 按 `parent_id` 链级联；fanout 登记父记录 + 每问题记录。
- **stop/close**：`SubagentRunner.stop_run` 取消 task、清 mailbox、置 `stopped` 闩锁、
  关 loop、row 标 cancelled、释放槽；`send/resume/follow-up` 对 stopped 一律拒绝；
  `CloseAgentTool` 走 `close_tree`；`/tasks stop` 走同步 `stop`（终态 + fence++）。
- **恢复**：builder 构造后 `scheduler.recover()` 扫全部 session journal；未结算
  （含 interrupted/pending）登记 `TaskView(kind="recovered",
  needs_reconciliation=True)`；不重跑；meta 不参与终态判断。
- **幂等**：`TaskRequest` 增 `parent_id/idempotency_key/payload_digest`；spawn/send/
  fanout 同键同 digest 返回已有结果/ids，异 digest 抛 `IdempotencyConflict`；空键跳过。
- **校验**：全量 `pytest` exit 0；`ruff` 通过；`mypy` 180 文件。
- **偏离记录**：未建“执行队列”；用“每个 run/fanout 一个可取消 Task 句柄 + 单 reaper”。
  claim 在提交后同步完成，没有排队未领取窗口；C3 合并队列会自带队列。
- **新增测试**：`tests/test_task_journal.py`、`tests/test_scheduler.py`（C2 段）、
  `tests/test_subagent_handles.py`（stop_run 终态/清 mailbox/拒绝 send/resume）。
- **tmux 真机（新进程恢复）**：先用 `TaskJournal` 写入 queued/claimed/running +
  一条 settled，再用真实 `_build_loop` 启动新进程；builder 的 `recover()` 输出
  `[('crashed-1','recovered','needs_reconciliation',True)]`，已 settled 的 done-1
  不再出现；没有重跑任何任务。
- **未做（按 C2 范围）**：合并队列（C3）、跨进程租约、WAL/收据链。
- **已知边界**：fanout 只支持整次取消（ResearchFanOut 不暴露子 task）；`interrupted`
  保持可续，不自动恢复；journal 的 `pending` 仅由 runner 在结算后写入。

## 8. 成功后的样子

- 每个任务在 `<ws>/.ai-coding/tasks/` 可折叠；重启后 `/tasks` 显示“需核对”的任务，
  不静默重跑。
- 取消后晚到的子结果被 fence 丢弃；`interrupt`（可续）与 `stop`（终态）分开。
- 重复 spawn/send/fanout 不重复执行；同键异内容报 conflict。
- C1 槽/视图/工具契约不变。
