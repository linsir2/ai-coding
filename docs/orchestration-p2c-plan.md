# P2-C 实施方案（评审修订稿）：统一编排、任务日志/恢复与合并队列

> 前置：P1、P2-A（`ef81377`）、P2-B（`5a1a61c`）已完成。
> 依据：`docs/orchestration-unified-research.md`、`docs/orchestration-unified-design.md`、
> 本地三入口/契约/取消/恢复/合并/槽位调研、`/tmp/accl` 逐层阅读、
> Codex/Gemini CLI/DSH/OpenCode/Claude Code/OpenHands 对比。
> 用户拍板：分 C1–C4 全量设计、分批实施；合并显式工具触发；重启标
> `needs_reconciliation` 不自动重跑；并发合并为 `max_execution_slots`、编排工具不占槽。
> 本稿已吸收第一轮独立评审的 P0/P1（槽嵌套、控制类占槽、Scheduler 能力、租约/超时、
> 合并判空/预检/verify、恢复与 meta、幂等键、Registry 覆盖、配置分期、影响面）。

## 0. 目标与非目标

**做**
1. **C1 统一契约**：`TaskRequest` + `Scheduler` + `TaskRegistry` + `ExecutionSlotPool`，
   并加 `ai.max_execution_slots`。
2. **C2 任务日志与恢复**：`TaskJournal`、claim/lease/fence、单一驱动器、取消级联、
   重启 `needs_reconciliation`。
3. **C3 合并队列**：显式 `merge_agent(target, verify_cmd?)`；单写者；git 3-way +
   校验；失败回滚；merge worker 独立于父 turn 取消。
4. **C4 收口**：三读口统一、旧配置别名、文档、tmux 真机矩阵。

**不做**
- 不照搬 ACCL 的 Raft/跨主机租约、持久 WAL/收据链/预算树、TLA+、跨进程 flock。
- 不用 JSON-aware `merge3` 处理代码文件（改用 git 3-way）。
- 不自动合并、不自动重跑已开始写过的子代理、不恢复后台运行中的子代理。
- 不新增第三张持久任务表；`TaskRegistry` 是只读视图。
- 不改 P1 wire、P2-A packet v3、P2-B claim 键/`NoticeStore` 语义。

## 1. 现状事实

| # | 事实 | 证据 |
|---|---|---|
| S1 | 三入口：inline dispatcher、child runner、fanout | `core/dispatch.py:237-307`、`core/subagent.py:356-385,541-650`、`core/research_fanout.py:112-174` |
| S2 | 三套容量：tools/subagents/jobs；子代理 `_active`+semaphore 双重计数 | `config/models.py:436,374`、`core/jobs.py:295`、`core/subagent.py:264,268,375-378` |
| S3 | 控制类工具在工具面：wait/list 也成 barrier/并行组 | `tools/agents_tool.py:255,324`、`tools/subagent_tool.py:449` |
| S4 | P2-B claim 调用级短持有；bash 后台 hold 到 settle；无 lease/fence | `core/write_coordinator.py:110-168`、`tools/bash_tool.py:232-242` |
| S5 | Esc 只取消当前 turn；后台 spawn 不随 Esc 取消 | `server/handlers.py:836-858`、`core/subagent.py:851-1049` |
| S6 | 后台子明确不恢复；write_ahead 只 turn 内；NoticeStore 可重放 | `domain/subagent.py:590-592`、`core/write_ahead.py:77-168` |
| S7 | 无 3-way/merge queue/cherry-pick；Worktree 无 base_commit | `core/worktree.py:37-43,106-116,128-177` |
| S8 | 读口字段/键名不一致；sync/fanout 无 `_background` 行 | `app/commands.py:1120-1200`、`tools/agents_tool.py:320-377`、`core/subagent.py:1590-1600` |
| S9 | 编排工具当前不 claim；可 `parallel_safe` 成组；父可能持槽等子 | `tools/agents_tool.py:60`、`core/dispatch.py:292-300` |
| S10 | ACCL merge3 是 JSON-aware；代码文件 modify/modify 必冲突 | `/tmp/accl/harness/rebase.py:217-315` |

## 2. C1 统一契约与调度

### 2.1 `TaskRequest`（`domain/orchestration.py`）

“调度策略 + 载荷”，不复制 `SubagentTask`：

```python
TaskRequest(
  id: str,                    # uuid；Registry 唯一键
  idempotency_key: str,       # 见 §3.6
  kind: "child" | "fanout",
  task: SubagentTask,         # P2-A 的 brief/context/agent/readonly/isolate/schema
  name: str,                  # 子代理标签
  origin_session: str,
  parent_span: str,
  timeout_s: float | None,    # None = runner 配置
)
```

- 校验：`kind=fanout` ⇒ `readonly=True & isolate=False`；`child` 可写可读。
- **运行期通道不进 request**：`parent/observer/emit/worktree_manager` 由 Scheduler
  方法参数传入（它们是一次调用的通道，不是任务身份）。
- `inline` 无 TaskRequest；Registry 只投影进程内事件。

### 2.2 `Scheduler`（`core/scheduler.py`）

```
Scheduler
  run(request, *, parent, observer, emit, worktree_manager) -> SubagentResult
  spawn(request, *, parent, observer, emit, worktree_manager) -> (request_id, run_id)
  fanout(requests, ...) -> list[SubagentResult]
  send(target, text) / interrupt(target) / resume(target) / close(target)
  wait(target, timeout_ms) / close_info(target)   # target 由 Registry 解析
  resolve_target(target) / resolve_run_id(prefix)
  stop(target)                    # /tasks stop：kill run（接受 run_id/request_id）
  cancel_tree(root_id)            # C2 级联
  merge(target, verify_cmd)       # C3；target 由 Registry 解析
  merge_status(target)            # 被取消的等待者事后查询
  task_list() / tasks() -> TaskRegistry
```

- 工具层只依赖 Scheduler；`SubAgentTool.runner` 等私有引用删除；`/tasks` 通过
  `Scheduler.tasks()`/`task_list()`/`stop()`。`AgentLoop` 暴露 `scheduler` 供
  commands/`_runner_of` 取；不再从工具私有属性拿 runner。
- `run` 与 `spawn` 共用准入与登记；后台执行仍由 runner 的 supervised task 承担，
  Scheduler 不造第二套后台生命周期。
- `spawn` 同时返回 `request_id` 与 `run_id`；`wait/send/interrupt/close/stop/
  merge_status` 的 `target` 都经 Registry 解析（request_id 或 run_id 前缀或 branch），
  避免 `spawn→wait` 拿 request_id 查不到 run。
- `interrupt`（当前 turn，非终态）与 `stop`（kill run，终态）保持现有两种语义，
  不混名。

### 2.3 `TaskRegistry`（`core/task_registry.py`，只读）

- 聚合：Scheduler 内存 `request_id -> TaskView`、`_handles`（真源）、`_background`
  （投影）、`_active`、C2 journal（C1 为 `None`，缺失时省略
  `needs_reconciliation`）、inline 事件。
- 统一内部 `TaskView`：`id, kind, state, agent, readonly, isolation, run_id,
  session_id, branch, commit, changed_paths, conflicts, usage, ledger_path, error,
  origin_session, created_at, updated_at, needs_reconciliation`。
- sync/fanout 没有 `_background` 行：Scheduler 在内存保留最终 TaskView（进程内可见，
  不承诺跨重启）；background 走 handles/row。
- 三个读口各自格式化，但**数据同源**；`list_agents`/`background.done` 的 camelCase
  兼容层保留（P2-A wire 不改）。

### 2.4 `ExecutionSlotPool`（`core/execution_slots.py`）

- 配置 `ai.max_execution_slots`（默认 4）。C1 即新增；若该字段**未显式设置**
  （`model_fields_set` 不含它），C1 就按 `max_concurrent_subagents`（默认 4）取值，
  `config.example.yaml` 同步；`max_concurrent_tools` 在 C4 变兼容别名。
- **槽单位 = 最外层执行单元**：一个 child run / 一个 fanout child / 一个工具组。
- **嵌套不申请**：`slot_held` contextvar **只**在 child run 的 execute task 内设置；
  子 loop 内部 dispatcher/工具组由该 task 创建，继承“已在槽内”，不再向池申请。
  工具组本身不设置 contextvar（避免同组内 read 的标记泄漏给 subAgent/fanout）。
- **队列与上下文**：`spawn` 同步 `try_reserve` 一个 slot lease（满则拒绝）；
  driver 的 execute task **在自身任务内** set/reset `slot_held`，并携带
  `parent/observer/emit/worktree_manager`（随队列项保存，不能从 driver 上下文继承）。
  `run` 的等待者取消时只取消执行 task，driver 不退出。
- **编排/控制工具不占槽**：`BaseTool.occupies_slot=False`，覆盖：
  `subAgent/spawn_agent/parallel_research/send_message/interrupt_agent/resume_agent/
  close_agent/wait_agent/list_agents/subagent_list/merge_agent`。
  dispatcher 拆出含这些调用的组：组内只对 `occupies_slot` 的调用取槽；全为编排
  则整组不取槽。
- child run 的 `_active` 与 semaphore 合并为槽池的一个 child key；不再双重计数。
- 池满：后台 `spawn` 拒绝（`SubAgentPoolFull`）；同步 `run` 排队；工具组排队。
- 模型步不占槽（一轮一个；把模型步计入会在 `slots=1` 时父子互等——对
  unified-design §4 的有意偏离）。

## 3. C2 任务日志、租约与恢复

### 3.1 `TaskJournal`（`core/task_journal.py`）

- 路径 `<workspace>/.ai-coding/tasks/<session_id>.jsonl`；单行 JSON：
  `{"v":1,"event":"queued|claimed|renewed|running|settled|cancelled|orphaned|reconciled|merged",
   "task_id","parent_id","kind","state","revision","claim":{...},"session_id","run_id",
   "branch","commit","error","at"}`。
- **单写者**：所有 append（Scheduler、runner 结算、recover）都经同一
  `asyncio.Lock`/`TaskJournal.append`；不 `fsync`（崩溃丢最后一行等价于未登记）。
- 读时跳过半行/坏行（崩溃可能截断最后一行）。
- **只记任务级事实**；文件写仍由 `write_ahead` 负责，避免两本账。

### 3.2 claim / lease / fence（单进程版）

- `TaskRecord`：`task_id, parent_id, state, revision, claim{owner, lease_until, fence}`、
  `cancel_epoch`。
- `_claim`：锁内 `fence += 1`；`lease_until = now + ttl`；
  `ttl = 2 * (request.timeout_s or runner.timeout_seconds or 1200)`，最小 60s
  （`subagent_timeout_seconds` 默认 1200，不能写死 600）。
- **续租**：driver 每 `ttl/3` 对运行中的 claim 写 `renewed` 并推进 `lease_until`；
  不因租约到期取消活任务（活任务由 runner 自己的超时结束）。
- **fence 只放 `contextvars.ContextVar`**，不写回共享 record；结算时校验 fence，
  不匹配 → `REJECT_STALE`，丢弃晚到结果。
- 租约只服务“重启判定失主”和“取消后旧任务晚到”；**不是**跨进程锁。
- 不引入写租约；写仍由 P2-B claim 管，两张表互不替代。

### 3.3 单一驱动器

- Scheduler 持有 `asyncio.Queue` + 一个 `_driver` task：
  - `run`：`queued` → 入队 → 等 Future；`spawn`：`queued` → 入队 → 立即返回 id。
  - driver：`claim` → `create_task(execute)`；`call_later(next_ttl/3)` 续租与巡检；
    **一个进程一个 driver**。driver 的 `set_result` 必须先 `future.done()` 判断，
    单个 ticket 异常只结算该 ticket，driver 继续下一项（等待者取消绝不杀 driver）。
  - **不套外层 `wait_for`**：同步/后台子代理的超时由 runner 的 `_run_turn`
    `asyncio.wait(timeout)` 负责；driver 只做登记/续租/巡检，避免与
    “`_supervised` 吞 CancelledError 返回 cancelled”冲突。
- 后台 `spawn` 的执行仍交给 runner supervised task；driver 记录 claim/run_id。
- 超时结算以 `result.status` 为准（`timeout` 已由 runner 产出）；driver 不把
  `CancelledError` 改写成 timeout。
- **queued 取消**：`claim` 前重读 record；`cancelled/cancel_epoch` 非当前 → 跳过
  执行、结算 cancelled。`stop`/`cancel_tree` 接受 request_id，能取消未 claim 的项。

### 3.4 取消级联

- `Scheduler.cancel_tree(root_id)`：
  - 从 Registry + journal 找 `parent_id` 子链（当前 depth=1，结构支持孙）。
  - 先子后父；子用 `interrupt`（当前 turn）；`close_tree` 才 `close`（关 loop）。
  - 写 journal `cancelled`；槽由 finally/结算释放；写 claim 由工具 finally 释放。
  - 幂等；终态不倒退；fence 递增，晚到结果被丢弃。
- **Esc 语义保持**：只取消当前 turn；`cancel_tree` 由 `close_agent`/显式命令调用，
  不把 Esc 改成级联后台子。
- `close_agent` 走 `cancel_tree`（depth=1 等价，结构统一）。
- `/tasks stop` 与 `interrupt` 分开：`stop` = kill run（终态）。

### 3.5 恢复

- `Scheduler.recover(workspace)` 在组装时执行，**扫描
  `<ws>/.ai-coding/tasks/` 全部 session 文件**（Scheduler 在会话前构造）。
- 对 `queued/claimed/running`：
  - **以 journal 最后一条 `settled/cancelled` 为唯一结算依据**；meta 只在 journal
    缺失时兜底，终态集合含 `completed/error/timeout/max_iterations/cancelled/
    interrupted`。后台续 turn 开始前先写 `running`，避免 `lastStatus` 默认
    `completed` 把运行中任务误判为已结算；
  - merge journal 的 `merging` ticket：重启若 `.git/MERGE_HEAD` 存在且无 worker，
    标 `needs_reconciliation`（不自动 abort/reset），交给人/显式命令；
  - 无活 handle、无完成 meta → 标 `needs_reconciliation`，写 `reconciled`；
  - **不重跑、不重建 loop、不重新 spawn**；保留 session/run/branch/commit；
  - 同一 run 已有 `reconciled` 记录则跳过（幂等）。
- `TaskView` 暴露 `needs_reconciliation`；`/tasks` 显示“需核对”。
- 幂等键：同键未终结 → 返回已有状态；已成功 → 返回引用；已失败且可重试的
  merge 使用新 attempt key（见 §3.6）。

### 3.6 幂等键范围

- `spawn`：`idempotency_key = parent_call_id`（一次工具调用一个后台子）。
- `fanout`：每个子 `parent_call_id + ":" + question_id`，避免 N 子被并成 1。
- `send_message`：`parent_call_id`（每次发送是一次调用）。
- `merge_agent`：**同分支进行中**去重；结束后总是新 ticket，不做成功缓存。
  后续 turn 可能在同一分支追加提交，缓存会吞掉新工作；已合并分支由
  `git merge-base --is-ancestor` 判为 `nothing_to_merge`，语义仍幂等。
  此条以 `docs/orchestration-p2c-c3-plan.md` v4 为准。
- 同键同内容 → 返回已有记录；同键异内容 → `conflict`；不静默覆盖。

## 4. C3 合并队列（显式触发）

### 4.1 `merge_agent(target, verify_cmd=None)`

- 新工具 `MergeAgentTool`，`permission_name` 默认（未知工具 → WARN）；
  `occupies_slot=False`、barrier。`verify_cmd` 在 gate 里按 bash 组合判定
  （DENY/WARN 都返回），不能借 `subAgent` 的 ALLOW 绕过。
- 输入 `target`（**request_id / run_id / 前缀 / name / branch**，由
  `Scheduler._merge_source` 解析）+ `verify_cmd?`；同步子没有 run_id，但
  Registry/journal 有 request_id/name/branch。
- 显式调用才入队；等待结果：`merged|merge_skipped|conflict|verify_failed|
  verify_unavailable|dirty|not_found|not_settled|commit_failed|branch_moved|
  head_moved|busy|journal_failed|unrecoverable`。
- 只读子/无分支：`nothing_to_merge`；**不以 `changed_paths=[]` 判定**——用
  `git diff --name-only <base>..<tip>`（bash 写入也可见）。
- 工具 data + `TaskRegistry(kind=merge)`；失败保留分支可重试。
  C3 v4 不新增 notice/event（没有真实消费者）；`/tasks` 与 journal 是读口。
- 等待者被父 turn 取消时，ticket 结果仍写 Registry/journal，可 `merge_status(target)`
  再查；不把结果留在已取消的 Future 里。

### 4.2 `MergeQueue`（`core/merge_queue.py`）

- 单写者 `asyncio.Queue` + 一个 worker；ticket `queued→merging→merged|skipped|failed`。
- **独立任务**：worker task 不挂在父 turn 的取消链上；父 turn 取消不中断 ticket。
- 步骤：
  1. 读 meta/journal：`branch, commit, base_commit`；旧 run 无 base 用
     `merge-base head0 tip0` 回退；`branch` 缺失 → `nothing_to_merge`；
     校验 branch/ref/sha 后才进入 git argv。
  2. 若 `tip0` 已是 HEAD 祖先（`git merge-base --is-ancestor`）→
     `merge_skipped(nothing_to_merge)`；否则 `git diff --name-only -z
     <base>..<tip0> --` 为空 → `merge_skipped`。
  3. 主工作区必须**无 tracked 改动**（`git diff-index --quiet HEAD --`）；
     untracked/ignored 与 branch 新增/修改路径求交，命中 → `dirty`。
     `status` 必须 `--untracked-files=all --no-renames -z`；ignored 用
     `ls-files -o -i --exclude-standard -z`。
  4. 拿主工作区**全局写 claim**（`owner="merge:<branch>"`, `paths=()`,
     timeout=30）；固定 owner，避免与 P2-B 重入语义打架。
  5. `head0 = rev-parse HEAD`；`git merge-tree --write-tree --name-only -z
     --no-messages head0 tip0` 预检；退出码 1 → 冲突路径；>1 → 跳过预检。
  6. **候选合并在临时 worktree**：`worktree checkout(head0)`，在 `<tmp>` 内
     `git -c core.hooksPath=/dev/null -c commit.gpgsign=false
     -c user.name=ai-coding -c user.email=merge@local
     merge --no-ff --no-verify tip0`；冲突 → 删临时 worktree → `conflict`。
  7. 释放 claim；在 `<tmp>` 用 **sandbox/process 层**跑 `verify_cmd`（限时 120s）。
     `policy` 必须 `enforce` 且 `not bypass`，backend 可用，`spawn_argv` 必须
     真的 wrap；否则 `verify_unavailable`。非 0/超时 → `verify_failed` + 输出尾部。
     `verify_cmd` 省略 → `verified=false`，不执行。verify 改文件只影响临时区。
  8. 重取 claim；HEAD == head0？tip == tip0？主区仍干净（含新 untracked/ignored）？
     tip 变化 → `branch_moved`（不重试）；HEAD 变化 → 丢弃候选、重建并重跑
     verify（最多 2 次）；仍变化 → `head_moved`。然后
     `git merge --ff-only <候选 sha>`；成功 → journal `merged`，返回 merge sha。
- **写 claim 必须 try/finally 释放**：conflict/verify_failed/dirty/unrecoverable 都
  不能永久占住全局 claim。
- 主区只在 verify 通过后进入合并；失败不留下半合并。临时 worktree 清理失败 →
  `unrecoverable`（路径写进结果），释放 claim，worker 继续。
- 不自动入队；子完成仍只报 branch/commit（未合并）。

## 5. 数据流

```
父 turn（inline）→ dispatcher（工具组取槽；编排/控制工具不取）
  └─ subAgent → Scheduler.run(TaskRequest, parent/emit/...) → 槽（最外层）
       └─ 子 loop 内部 slot_held=True → 不再申请槽
  └─ spawn_agent → Scheduler.spawn → 准入 → runner.spawn_background → journal
  └─ parallel_research → Scheduler.fanout → 每个子一个槽
  └─ merge_agent → MergeQueue（独立 task）→ 写 claim → merge-tree(HEAD,branch)
       → 临时 worktree 候选合并 → verify(sandbox) → HEAD 未变 → ff-only 落主区 → commit
       失败 → 删临时区 / head_moved 重试；主区不进入半合并
Esc → 只取消当前 turn；close_agent → Scheduler.cancel_tree → journal cancelled
重启 → Scheduler.recover → journal 未结算 + meta 交叉 → needs_reconciliation
/tasks / subagent_list / list_agents → TaskRegistry.list() → TaskView[]（同源）
```

## 6. 逐文件改动

| 期 | 文件 | 改动 |
|---|---|---|
| C1 | `domain/orchestration.py`（新） | `TaskRequest`、校验、`to_dict` |
| C1 | `core/scheduler.py`（新） | 门面 + 准入/登记；控制/观测 API |
| C1 | `core/task_registry.py`（新） | `TaskView`、`list/get`；聚合 handles/background/active/journal |
| C1 | `core/execution_slots.py`（新） | 单池；`acquire/try_acquire/release`；`slot_held` contextvar |
| C1 | `tools/base.py` | `occupies_slot`（默认 True）；编排/控制设 False |
| C1 | `core/dispatch.py` | 工具组取槽；编排组拆出/不取；嵌套检测 |
| C1 | `core/agent_loop.py` | dispatcher 注入共享 SlotPool/Scheduler；loop 暴露 `scheduler` |
| C1 | `core/subagent.py` | `_active`/semaphore → 槽池；`run/spawn` 收 TaskRequest；Registry 读口 |
| C1 | `tools/{subagent,agents,parallel_research}_tool.py` | 改调 Scheduler；控制类全量委托 |
| C1 | `tools/builder.py`、`cli.py`、`server/world.py` | 组装 Scheduler/SlotPool/Registry；`max_execution_slots` |
| C1 | `config/models.py` | 新增 `max_execution_slots`（C4 别名） |
| C1 | `app/commands.py`、两个 list 工具 | 三读口走 Registry；camelCase 兼容 |
| C2 | `core/task_journal.py`（新） | append/recover/半行容错 |
| C2 | `core/scheduler.py` | claim/lease/renew/fence、driver、cancel_tree、recover、幂等 |
| C2 | `core/subagent.py` | 结算走 journal+锁；fence 校验；cancel/stop 落点 |
| C2 | `server/handlers.py` | 显式 cancel_tree 入口（如需）；Esc 不变 |
| C3 | `core/merge_queue.py`（新） | 单写者、独立 task、git 3-way、verify、失败矩阵 |
| C3 | `tools/merge_agent_tool.py`（新） | 显式工具；等待结果；data；WARN |
| C3 | `core/worktree.py` | `Worktree.base_commit`（创建时 HEAD）+ `checkout` |
| C3 | `core/subagent.py`、`domain/subagent.py` | meta/result/row 加 `base_commit`（加法） |
| C3 | `core/task_registry.py`、`app/commands.py` | `TaskView(kind=merge)`；`/tasks` 行；`merge_status` |
| C3 | `server/world.py` | `aclose` 关主 Scheduler（不关子 loop 共享实例） |
| C4 | config/文档/测试 | 旧配置别名、三读口收口、docs、真机 |

**不改**：P1 wire、P2-A packet 字段语义、P2-B claim 键/`_active` 幂等语义（只迁移
到槽池）、NoticeStore at-least-once、depth=1。

## 7. TDD 测试清单

| 期 | 文件 | 用例 |
|---|---|---|
| C1 | `tests/test_task_request.py`（新） | fanout 校验、id 唯一、SubagentTask 不复制、运行期通道不在 request |
| C1 | `tests/test_scheduler.py`（新） | run/spawn/fanout/control/wait/stop 全走门面；emit 仍到事件 |
| C1 | `tests/test_execution_slots.py`（新） | `slots=1` 父等子不死锁；子内部不申请槽；编排/控制不占槽；池满语义 |
| C1 | `tests/test_task_registry.py`（新） | 三读口同源字段；sync/fanout 内存 TaskView；background run |
| C1 | `tests/test_tool_subagent.py`/`test_agents_tools.py`/`test_agents_command.py` | 工具不再持有 runner；wait/list `/tasks stop` 仍可用 |
| C2 | `tests/test_task_journal.py`（新） | append/recover/半行；未结算→needs_reconciliation；reconciled 幂等 |
| C2 | `tests/test_scheduler.py` | fence 过期结果丢弃；lease 续租；后台超时不误写 cancelled |
| C2 | `tests/test_cancel_cascade.py`（新） | 父 cancel 子、**queued 未 claim 也能取消**、幂等、终态不倒退、Esc 不级联、stop≠interrupt |
| C2 | `tests/test_idempotency.py`（新） | spawn/send/fanout/merge 键；同键异内容 conflict；merge 失败可重试 |
| C2 | `tests/test_task_journal.py` | 等待者取消不杀 driver；单个 ticket 异常 driver 继续 |
| C3 | `tests/test_merge_queue.py`（新） | 干净合并；已祖先/空 diff；HEAD/tip 前进；冲突；verify 失败/不可用；dirty；**untracked/ignored 碰撞**；claim 释放；串行；cleanup 失败；journal 写失败 |
| C3 | `tests/test_merge_agent_tool.py`（新） | 显式、target=request_id/run_id/前缀/name/branch、只读子、nothing_to_merge（bash 写入也识别）、commit_error、WARN/verify 红线、取消后 `/tasks`/`merge_status` |
| C4 | 既有 | 全量 + 前端 + tmux 真机 |

**同步修改**：`test_subagent_runner`（槽池）、`test_background_tasks`（池满/cancel/
recover）、`test_builder`（Scheduler 组装）、`test_subagent_handles`（stop/close）、
`test_subagent_limits`（槽）、`test_tool_subagent_list`/`test_agents_command`（读口）、
`test_boot_resume`（recover）、`test_repl`（`/tasks` merge 行）。

## 8. 风险与边界

1. **槽嵌套**：靠 `slot_held` contextvar；child run 持槽、内部不再申请；编排/控制
   不占槽且拆组。测试必须覆盖 `slots=1`。
2. **两本账**：任务 journal 只记任务；文件写归 `write_ahead`。
3. **fence**：contextvars，不写回共享 record；只防取消后晚到。
4. **合并**：独立 task、is-ancestor/HEAD/tip 预检、**verify 在临时 worktree**、
   tracked + untracked/ignored 干净检查、失败删临时区；主区不进入半合并；
   清理失败报 unrecoverable，不 reset。
5. **恢复**：只标 `needs_reconciliation`；与 meta 的**所有终态**交叉；merge
   `merge_queued/merging` 都标需核对；不重跑。
6. **配置**：C1 加 `max_execution_slots`；C4 处理别名与旧配置。
7. **Registry**：只读；sync/fanout 进程内保留；三读口数据同源、格式兼容。
8. **幂等键**：fanout 按 question；merge 同分支进行中去重，结束后新 ticket，
   重复合并由 `is-ancestor` 变 no-op；不做成功缓存。

## 9. 验收

- 全量 `pytest` 0 failed；`ruff`/`mypy`；前端 `bun test` + `tsc`。
- 槽：`slots=1` 父执行子代理不互等；子内部工具直接跑；编排/控制不占槽。
- 统一：三读口同源；工具只依赖 Scheduler；`wait/list/stop` 能力不丢。
- 日志/恢复：journal 半行容错；未结算重启标需核对且不重跑；meta 已完成不误标；
  fence 丢弃晚到；后台超时不被改写。
- 取消：父 cancel 子、幂等、Esc 不级联、stop 与 interrupt 分开。
- 合并：已祖先/空 diff；HEAD/tip 预检；bash 写入也能识别；冲突/verify/dirty/
  branch_moved/head_moved 回退；verify 在临时 worktree，改文件不影响主区；
  合并独立于父 turn 取消；dirty 查 tracked + untracked/ignored 碰撞。
- **tmux 真机**：`slots=1` 长任务；两可写子 + 显式 merge；冲突 merge 失败；
  verify 失败回退；重启后 `/tasks` 显示需核对。

## 10. 实施顺序

1. **C1**：TaskRequest/Scheduler/TaskRegistry/SlotPool + 工具迁移 + 配置（独立可回退）。
2. **C2**：TaskJournal/claim-lease-fence/driver/cancel/recover。
3. **C3**：base_commit、MergeQueue、merge_agent、冲突/verify。
4. **C4**：配置别名/读口/docs/tmux 真机；三轮评审与修复。
