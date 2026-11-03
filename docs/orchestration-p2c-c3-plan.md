# P2-C3 实施方案 v4（评审收口稿）：显式 `merge_agent` 与单写者合并队列

> 前置：P2-C1（`67c53ae`/`f889b74`）、P2-C2（`1ed2331`/`ec98912`）已完成并推送。
> 总体方案 `docs/orchestration-p2c-plan.md` §4；本稿只做 C3，可独立回退。
>
> v4 在 v3 上补第三轮收口：关闭中的 enqueue、landing/aclose 竞态、
> dirty 只与 changed 碰撞、base 用 merge-base、recover 保留 child kind、
> merge view 名字不冲突、verify 的 linked-worktree gitdir 边界。
>
> v3 吸收两轮独立评审：
> - 第一轮 4 个 P0：verify_cmd 权限绕过、非 enforce 下 verify 不隔离、
>   `commit_error` 被当成功、ignored 文件被覆盖；以及 base_commit、journal、
>   源解析、tip、merge-tree、队列关闭与测试失真。
> - 第二轮 3 个 P0：子 loop 会关闭全局 Scheduler、重复 merge 缺少 is-ancestor、
>   enqueue 失败永久挂起；以及 source 身份、status 统一、灰区字段与文档冲突。
>
> 用户已拍板：显式工具触发；不自动合并；verify 只在临时 worktree；失败不碰主区；
> 恢复以 journal 为唯一结算依据；不照搬 JSON-aware merge3 / WAL / 收据链 / Raft；
> `merge_agent` 全部 WARN；`verify_cmd` 可选，省略时显示“未验证”；
> merge 结果进入 `/tasks`；`merge_status` 从该视图读。

## 0. 目标与非目标

**做**

1. `merge_agent(target, verify_cmd?)` 显式工具；`target` 支持 request_id /
   run_id / run-id 前缀 / name / branch。
2. `MergeQueue`：单写者 FIFO；出队后重新取主区写 claim、重新校验 HEAD/tip 才落地。
3. git 3-way：`merge-tree` 预检 + 临时 worktree 候选合并 + 沙箱 verify。
4. 失败回滚：冲突 / verify 失败 / dirty / HEAD 前进 / branch 前进都不改变主区；
   临时区清理。
5. `Worktree.base_commit`；`SubagentResult` / `BackgroundTask` / `TaskView` /
   `AgentHandle` meta / journal 的 `base_commit` 加法字段。
6. journal `merge_queued / merging / merged / merge_failed / merge_skipped`；
   `merge_queued`/`merging` 重启标 `needs_reconciliation`，不自动重试。
7. merge 进入 `/tasks` 读口：`TaskRegistry` 注册只读 `TaskView(kind="merge")`；
   `Scheduler.merge_status(target)` 过滤 `kind="merge"` 后读同一视图。

**不做**

- 不做自动合并、batch/speculation、跨进程队列、持久化队列。
- 不删除源分支；不自动 rebase。
- 不新增第三张持久任务表。
- 不改 P1 wire、P2-A packet 字段语义、P2-B claim 键、C2 fence/stop/recover 语义。
- 不新增 notice/event/bridge 的 base 字段：没有真实消费者。
- 不实现失败自动重试、成功结果缓存、merge 取消票。
- 不做 `merge_status` 的独立工具或 slash：它由 `/tasks stop` 分支消费。

## 1. 现有设计与复用契约

### 1.1 现有设计（C1/C2）

| 能力 | 现有实现 | C3 复用 |
|---|---|---|
| 子代理隔离 | `WorktreeManager.create/temporary/commit_if_dirty/remove`；每轮提交；分支保留 | 只加 `base_commit` 与 `checkout` |
| 任务调度 | `Scheduler.run/spawn/fanout/stop/cancel_tree/close_tree` | 只加 `merge` |
| 任务读口 | `TaskRegistry` 聚合 `_handles`/`_background`/内存 view；`/tasks` 读它 | 注册 merge view；不新建表 |
| 任务日志 | `TaskJournal` JSONL，按 task_id 折叠，`pending()` 供 recover | 只加事件/字段 |
| 写协调 | `WriteCoordinator.claim(owner, root, paths, timeout)`；`paths=()` 整库 | 队列落地阶段取整库 claim |
| 进程隔离 | `spawn_argv` + `run_process_async`；`enforce` 缺机制 fail-closed | verify 复用，要求实际 wrap |
| 权限 | `PermissionGate.check` + `bash_denied` | merge_agent WARN；verify 组合 bash 判定 |
| 工具契约 | `permission_name/precheck/write_paths_for/mutates_world_for/occupies_slot` | 只实现钩子，不新增框架 |
| 父子包 | `SubagentResult` v3、`AgentPacket`、ledger、meta | 只加 `base_commit` 可选字段 |

### 1.2 复用契约

- `WorktreeManager.create()` 返回 `Worktree(name, branch, path, base_commit)`。
- `commit_if_dirty()` 返回 `(sha|None, error|None)`。
- `WriteCoordinator.claim()` 整库 `paths=()`；`Claim.release()` 幂等。
- `gitcmd.git()` 统一 git 入口；解析值必须 `merge_stderr=False`。
- `TaskJournal.append/records/pending` 唯一任务日志。
- `Scheduler._record_for/_settle/on_runner_event/recover/aclose` 只加法参数。
- `TaskRegistry.register/list/get` 唯一任务读口；merge 只是 `TaskView`。
- `BaseTool` 钩子、`ToolContext` 字段、`spawn_argv`、`run_process_async`、
  `bash_denied`。
- 不复制 `MergeQueue` 的源解析：源解析只在 `Scheduler._merge_source`。

### 1.3 当前边界（C3 不改）

- 后台子不自动恢复、不重跑。
- `changed_paths` 不含 bash 写入；merge 用 `git diff base..tip`。
- 写 claim 只有单进程语义。
- `TaskJournal.append` 仍 best-effort，但 merge 关键点检查返回值。
- `TaskView` 是投影；权威仍是 journal + runner handles。
- `/tasks` 是 merge 的用户读口；`list_agents`/`subagent_list` 仍是子代理专用，
  不显示 merge（它们不是 merge 的消费者）。

## 2. 影响面与执行范围

### 2.1 新增文件

| 文件 | 作用 | 消费者 |
|---|---|---|
| `core/merge_queue.py` | 单写者队列、git 3-way、临时区、verify、journal | `Scheduler.merge` |
| `tools/merge_agent_tool.py` | 模型可见显式合并工具 | `tools/builder.py` |
| `tests/test_merge_queue.py` | 纯 git / 失败矩阵 | CI |
| `tests/test_merge_agent_tool.py` | 工具/权限/target/取消 | CI |

### 2.2 修改文件

| 文件 | 改动 | 为什么 |
|---|---|---|
| `core/worktree.py` | `Worktree.base_commit`；`create` 显式基线；`checkout` | 候选区与可追溯基线 |
| `domain/subagent.py` | `SubagentResult.base_commit`；`BackgroundTask.base_commit` | packet/行表 |
| `core/subagent.py` | handle/meta/base 传播；`_finish_background`/`_restore_row`/`_scheduler_event`；`close_info` 加 base | 后台/resume/重启 |
| `core/task_registry.py` | `TaskView.base_commit/verified`；`is_running`；`from_background`/`_merge`/`to_dict` | `/tasks` 与源解析 |
| `core/task_journal.py` | 新事件/终态；`base_commit/name/commit_error` fold；append bool | 恢复索引 |
| `core/scheduler.py` | `merge/merge_status/_merge_source/_merge_inflight`；`_settle/on_runner_event` 加法；`recover`；`aclose` 队列 | 统一门面 |
| `security/permission_gate.py` | merge_agent 的 verify_cmd 组合 bash 判定 | 不绕过红线/规则 |
| `core/dispatch.py` | `CONTROL_REPLAY_TOOLS` 加 `merge_agent` | 不重放旧合并 |
| `tools/builder.py` | 单 `TaskJournal` + `MergeQueue`；注入 Scheduler；注册工具 | 装配 |
| `server/world.py` | `aclose` 先关主 Scheduler | 只关主实例，不误关子 loop |
| `app/commands.py` | `/tasks` merge 行；stop 分支用 `merge_status` | 用户可见 |
| `docs/data-contracts.md` | C3 契约 | 文档同步 |
| `docs/orchestration-p2c-plan.md` | 修正 §4.1/§4.2/§5/§6/§8 | 消除冲突 |
| `docs/orchestration-handoff.md` | 更新 C3 v4 状态与授权名 | 交接 |

### 2.3 不修改

- `TaskRequest`、`SubagentTask`、`AgentPacket` 字段语义。
- `WriteCoordinator` claim 键/owner/重入。
- `Scheduler` fence/stop/close/cancel_tree/recover 判定。
- `server/bridge.py`、前端：不加 wire 字段。
- `tools/subagent_tool.py` 的 tool data：不加 base（无模型消费者）。
- `AgentLoop.aclose`：不在这里关 Scheduler；子 loop 与主 loop 共用同一实例。

## 3. 数据流

### 3.1 正常 merge

```
父 turn
  → merge_agent(target, verify_cmd?)
      dispatcher:
        gate: merge_agent → WARN；
              verify_cmd 非空 → 先按 bash 组合判定；DENY/WARN 一并返回
        precheck: target 非空、verify_cmd 类型/空白
        write_paths_for → None（不取 dispatcher claim）
        CONTROL_REPLAY_TOOLS 含 merge_agent（不写 write-ahead）
      → Scheduler.merge(target, verify_cmd, session_id=ctx.session_id)
          _merge_source(target)：
            registry child view（kind != merge；id/run 前缀/name/branch）
            → journal(kind=child，按 session 过滤）
            → close_info(run_id) 交叉补 branch/base/commit/commit_error
            未终态 → not_settled；commit_error → commit_failed；
            无 branch → nothing_to_merge；无记录 → not_found；歧义 → ambiguous
          _merge_inflight[branch] 已存在 → await 同一 ticket
          否则建 ticket；注册 TaskView(kind="merge", status="queued")
          enqueue 失败（满/journal）→ 结算 ticket、done.set、移 inflight、
                                     失败 view；不永久挂起
          → MergeQueue.enqueue(ticket)
              journal merge_queued
              worker:
                1. pre_head = rev-parse HEAD；tip0 = rev-parse <branch>（校验 ref/sha）
                   pre_base = git merge-base pre_head tip0（真实 merge-base）
                   ticket.base_commit 只作审计/回退；merge-base 失败 → invalid_ref
                2. 若 tip0 已是 pre_head 祖先 → merge_skipped(nothing_to_merge)
                3. git diff --name-only -z pre_base..tip0 --；空且非祖先 → merge_skipped
                4. 主区 tracked 干净；只把“与 changed 路径碰撞”的 untracked/ignored
                   判 dirty（不是所有 untracked；运行时目录不参与）
                5. claim(owner="merge:<branch>", paths=(), timeout=30) → busy
                6. 在锁内重读 head0 = rev-parse HEAD；base = merge-base head0 tip0；
                   若 head0/pre_base 改变 → 重跑步骤 2-4；然后
                   merge-tree --write-tree --name-only -z --no-messages head0 tip0
                     exit 1 → conflict(paths)；>1 → 跳过预检
                7. worktree checkout(head0) → git -c core.hooksPath=/dev/null
                   -c commit.gpgsign=false -c user.name=ai-coding
                   -c user.email=merge@local merge --no-ff --no-verify tip0
                     冲突 → conflict(paths from diff --diff-filter=U) → 清理
                     得到 candidate
                8. 释放 claim（verify 不长期占主区写锁）
                9. verify（仅 ticket.verify_cmd，strip 后非空）：
                     policy 必须 enforce 且 not bypass；backend 可用；
                     spawn_argv 必须真的返回 wrapped argv；否则 verify_unavailable
                     cwd=tmp，roots=[tmp]，cancel event，输出尾部 4000
                     非 0/超时/不可用 → verify_failed
                   省略 → verified=false，不执行
                10. claim 重取 → busy
                    HEAD == head0？tip 仍 == tip0？主区仍干净（含新 untracked/ignored）？
                      tip 变化 → branch_moved（不重试）
                      HEAD 变化 → 丢弃候选，重建并重跑 verify（最多 2 次）
                      仍变化 → head_moved
                    git merge --ff-only candidate
                11. journal merged；结果 {commit, main_head, verified}
              finally: worktree remove；释放 claim；on_update；done.set()
          → ticket.done.wait() → ToolResult
```

### 3.2 `/tasks` 与恢复

```
TaskRegistry.register(TaskView(kind="merge", status=queued|merging|...))
  → /tasks 显示 merge 行
  → /tasks stop <merge-target> → Scheduler.merge_status(target)
     （过滤 kind="merge"；不调用 Scheduler.stop）
  → list_agents/subagent_list 不显示 merge（子代理专用读口）

重启：
  journal.pending() 中 kind=child → 现有 needs_reconciliation
  journal.pending() 中 kind=merge（merge_queued/merging）
    → TaskView(kind="merge", status="needs_reconciliation",
               branch/base_commit/commit/name)
    → /tasks 显示“需核对”；不自动重跑
```

### 3.3 主区不变量

- 任何失败在 `git merge --ff-only` 之前：主区 HEAD / index / worktree 不变。
- `ff-only` 失败：不执行 `merge --abort`/`reset`；结果 `head_moved`/`dirty`/`error`。
- `ff-only` 成功后才写 `merged`；写 journal 失败 → `merged + journal_error`，
  重启按 `merging` 保守标 `needs_reconciliation`。
- 临时区删除失败：落地前 → `unrecoverable(worktree_path)`；
  落地后 → `merged + cleanup_error`。

## 4. 接口与数据契约

### 4.1 `MergeTicket`

```python
@dataclass
class MergeTicket:
    key: str                  # branch；同分支唯一
    source_task_id: str       # 子任务 task_id，作 parent_id
    source_run_id: str
    source_name: str
    branch: str
    tip: str = ""             # worker 开始时钉住
    base_commit: str | None = None
    verify_cmd: str | None = None
    session_id: str = ""
    status: str = "queued"    # queued|merging|merged|merge_skipped|merge_failed|cancelled
    phase: str = "queued"     # queued|verify|landing（aclose 用）
    started: float
    ended: float | None = None
    attempts: int = 1
    outcome: dict[str, Any] | None = None
    done: asyncio.Event
    on_update: Callable[[MergeTicket], None] | None = None
```

- 不设 `ticket_id`：branch 是唯一键；journal `task_id=merge:<branch>`。
- `done` 是 Event，不是 Future；等待方取消不影响 ticket。
- `on_update` 由 Scheduler 注入；回调异常被吞掉并记日志。

### 4.2 `MergeQueue`

```python
class MergeQueueFull(RuntimeError): ...
class MergeJournalError(RuntimeError): ...
class MergeQueueClosed(RuntimeError): ...

class MergeQueue:
    def __init__(self, repo_root, *, worktree_manager, journal,
                 write_coordinator, policy, backend,
                 capacity=32, verify_timeout=120.0, claim_timeout=30.0,
                 max_attempts=2): ...
    def enqueue(self, ticket: MergeTicket) -> None
    async def wait(self, ticket: MergeTicket) -> dict
    async def aclose(self) -> None
```

- `enqueue`：首行检查 `closed`（→ `MergeQueueClosed`）；再检查容量
  （满 → `MergeQueueFull`），写 `merge_queued`（失败 → `MergeJournalError`），
  再 `put_nowait`；`put_nowait` 失败时写 `merge_failed` 并抛 `MergeQueueFull`。
- worker 懒启动；单票异常只结算该票；worker 顶层 `try/except/finally` 不死。
- `aclose`：置 closed；**若当前票处于 landing（已开始 `ff-only`）则不取消 worker，
  等待该票结束**（避免 git 线程仍在改主区时释放 claim）；否则设置当前 verify
  的 cancel event 并取消 worker。随后排空 queued（`cancelled` 终态），
  每票 `done.set()` + `on_update`，await worker。
- `wait` 只等 `done`，返回 `ticket.outcome`。

### 4.3 `Scheduler.merge`

```python
async def merge(self, target: str, verify_cmd: str | None = None, *,
                session_id: str = "") -> dict
def merge_status(self, target: str) -> dict
```

- `_merge_source(target, session_id)`：
  1. `TaskRegistry.list()` 中 `kind != "merge"` 的 child view：id / run_id 前缀 /
     name / branch；有 `session_id` 且 view.origin_session 非空时按它过滤；
     多个 → `ambiguous`。
  2. `TaskJournal.records()` 中 `kind == "child"`：task_id / run_id / name / branch；
     有 `session_id` 且记录 session_id 非空时按它过滤；多个 → `ambiguous`。
     同一来源同时出现在 registry/journal 时按 `(task_id, run_id, branch)` **合并**，
     不重复计数，也不据此报 ambiguous。
  3. 有 `run_id` 时**始终** `runner.close_info(run_id)` 交叉补 branch/base/commit/
     commit_error；即使 registry 已有 branch，也补缺字段并检查 commit_error。
- 终态：有 journal 记录 → `is_terminal`；无 → view 非 running 且
  `needs_reconciliation=False`。`not_settled` 覆盖 running/needs_reconciliation/
  recovered-child。
- `commit_error` 非空 → `commit_failed`；`branch is None` → `nothing_to_merge`。
- `_merge_inflight` 按 branch；已有 → `await done`；结束后 `on_update` 移除。
  **不做成功缓存**；重复合并由 `is-ancestor` 变 no-op。
- `merge_status(target)`：先 `registry.list()` 过滤 `kind=="merge"`，按 id/name/
  branch/run_id 前缀唯一匹配；in-flight 直接返回 ticket 当前状态；否则
  `{"status": "not_found"}`。
- `enqueue` 抛 `MergeQueueFull`/`MergeJournalError`/`MergeQueueClosed`：
  `_fail_ticket(ticket, status)`，设置 outcome/done、移 inflight、同步失败 view；
  `merge` 返回失败，不等待。`closed` 映射为 `unavailable`。
- `merge_queue is None` → `{"status": "unavailable"}`。

### 4.4 `Worktree`

```python
@dataclass(frozen=True)
class Worktree:
    name: str
    branch: str
    path: Path
    base_commit: str | None = None
```

- `create()`：`git rev-parse --verify HEAD`（`merge_stderr=False`，校验 40/64 hex）
  → `worktree add -b <branch> <path> <base>`；失败统一 `WorktreeError`。
- `checkout(name, commit)`：`worktree add --detach <path> <commit>`；`branch=""`，
  `base_commit=commit`；`commit` 必须已校验。
- `temporary()` 不变。

### 4.5 元数据（只加可选字段）

| 位置 | 字段 | 消费者 |
|---|---|---|
| `SubagentResult` / `to_dict` | `base_commit` | packet/ledger、merge 源 |
| `BackgroundTask` / `to_dict` | `base_commit` | `/tasks` 行、`TaskView.from_background` |
| `TaskView` / `to_dict` | `base_commit`、`verified` | `/tasks`、merge 源 |
| `AgentHandle` / meta | `base_commit` / `"baseCommit"` | resume、跨 turn |
| `TaskRecordState` / fold | `base_commit`、`name`、`commit_error` | 重启后 merge 定位 |
| `Scheduler._settle/on_runner_event` | 可选 `branch/commit/base_commit/commit_error` | 后台结算写 journal |

- 同步隔离：`result = replace(result, base_commit=getattr(wt, "base_commit", None))`；
  测试替身没有该属性时为 `None`，不抛 `AttributeError`。
- 后台：`handle.base_commit = getattr(wt, "base_commit", None)`；
  `_finish_background` 行、`_record_handle_facts`、`_write_meta`、`_restore_row` 同步。
- `_scheduler_event(handle, result)` 传：
  `branch=result.worktree_branch or handle.worktree_branch`、
  `commit=result.commit or handle.commit`、
  `base_commit=getattr(result, "base_commit", None) or handle.base_commit`、
  `commit_error=result.commit_error or handle.commit_error`。
- `close_info` 增加 `baseCommit`，供 journal 缺 branch 时兜底。
- 不加：bridge/event/`_details_projection`/`tools/subagent_tool.py` 的 base；
  这些没有真实消费者。

### 4.6 journal 事件

| 事件 | 终态 | 字段 | 说明 |
|---|---|---|---|
| `merge_queued` | 否 | task_id=`merge:<branch>`, parent_id=source task, kind=`merge`, branch, tip, base_commit, name, session_id | 入队成功才写 |
| `merging` | 否 | 同上 + worktree_path, candidate | 主区任何写之前写 |
| `merged` | 是 | commit, main_head, verified, branch, base_commit | `ff-only` 成功后 |
| `merge_skipped` | 是 | error=`nothing_to_merge` | 空 diff / 已祖先 |
| `merge_failed` | 是 | error=status, conflicting_paths, worktree_path | 其余失败 |
| `cancelled` | 是 | error=`cancelled` | `aclose` 排空 queued 票 |

- `TERMINAL_EVENTS` 增加 `merged/merge_skipped/merge_failed`（`cancelled` 已存在）。
- `merge_queued`/`merging` 非终态 → `recover()` 标 `needs_reconciliation`；
  R19 必须断言两者。
- merge 记录 `task_id` 与子任务分开；子任务不会被重新打开。
- `TaskJournal.append` 返回 `bool`；`merge_queued`/`merging` 写失败不触碰主区，
  返回 `journal_failed`。
- `TaskRecordState` 只 fold `base_commit/name/commit_error`；`tip` 保留在 journal 行
  供审计，不进入 fold（无恢复消费者）。

### 4.7 `MergeAgentTool`

- `name="merge_agent"`；`permission_name` 默认（未知工具 → WARN）。
- `mutates_world=True`；`parallel_safe=False`；`occupies_slot=False`；
  `write_paths_for → None`；`max_duration_seconds=300.0`。
- 参数：`target`（必填 string）、`verify_cmd`（可选 string；`strip()` 后空视为未提供）。
- `precheck`：target 非空、verify_cmd 类型；不复制 bash 红线（gate 已组合判定）。
- 结果映射：`merged`/`merge_skipped(nothing_to_merge)` → success=True；
  其余 → success=False + `error=status`；失败保留源分支，可重试。
- `data`（snake_case）：`status, branch, base_commit, commit, main_head,
  conflicting_paths, verify{exit_code, output, verified}, error, attempts,
  worktree_path, verified`。

### 4.8 权限：verify_cmd 不得绕过 bash

`PermissionGate.check` 在 hard tier 之后、规则层之前增加：

```python
if name == "merge_agent":
    verify_cmd = str(params.get("verify_cmd") or "").strip()
    if verify_cmd:
        bash_decision = self.check(
            "bash", {"command": verify_cmd},
            session_rules=session_rules, agent_rules=agent_rules, posture=posture,
        )
        if bash_decision.decision in (
            PermissionDecision.DENY, PermissionDecision.WARN
        ):
            return bash_decision
```

- 复用同一个 `check()`：红线、agent veto、规则层、grant 全部生效。
- merge_agent 自身仍未知工具 → WARN；verify 的 DENY/WARN 优先返回。
- 不新建 `merge.verify` 权限名，不复制 `bash_denied`。

## 5. 逐文件实现步骤

### 5.1 `core/worktree.py`

1. `Worktree` 加 `base_commit`。
2. `create()`：`rev-parse --verify HEAD`（`merge_stderr=False`，40/64 hex）→
   `worktree add -b branch path base`；失败 `WorktreeError`。
3. `checkout(name, commit)`：detach worktree；`branch=""`；`base_commit=commit`。
4. `temporary/remove/commit_if_dirty` 不变。

### 5.2 `domain/subagent.py`

1. `SubagentResult.base_commit`；`to_dict` 加 `"base_commit"`。
2. `BackgroundTask.base_commit`；`to_dict/from_dict` 加 `baseCommit`。
3. 不 bump `SUBAGENT_PACKET_VERSION`。

### 5.3 `core/subagent.py`

1. `AgentHandle.base_commit`。
2. `_run_isolated`：`replace(result, base_commit=getattr(wt, "base_commit", None))`。
3. 后台 `_attach_for_run`：`handle.base_commit = getattr(wt, "base_commit", None)`。
4. `_record_handle_facts`：`if result.base_commit: handle.base_commit = ...`。
5. `_write_meta` 加 `"baseCommit"`；`_load_meta`/resume 写回 handle。
6. `_finish_background` 行 `replace(row, ..., base_commit=result.base_commit or
   row.base_commit)`。
7. `_restore_row` 复制 branch/commit/base。
8. `_scheduler_event` 传 branch/commit/base/commit_error（handle 兜底）。
9. `close_info` 加 `baseCommit`。
10. `_details_projection`/notice 不加 base。

### 5.4 `core/task_registry.py`

1. `TaskView` 加 `base_commit`、`verified`；`to_dict` 加 `baseCommit`/`verified`。
2. `is_running` 改为 `status in {"running", "queued", "merging"}`；
   `is_settled` 不变（queued/merging 不修剪）。
3. `from_background` 填 base。
4. `_merge` 保留 base/verified（row 优先，view 兜底）。
5. **不改 `get`**：merge view 由 `Scheduler.merge_status` 过滤 `kind="merge"` 解析。

### 5.5 `core/task_journal.py`

1. `TERMINAL_EVENTS` 加 `merged/merge_skipped/merge_failed`。
2. `TaskRecordState` 加 `base_commit/name/commit_error`；`_fold` 复制。
3. `append` 返回 `bool`；`OSError` 返回 False。
4. `records/pending` 不变。

### 5.6 `core/merge_queue.py`（新）

1. `MergeTicket`、`MergeQueueFull`、`MergeJournalError`、`MergeQueueClosed`。
2. `enqueue`：closed → `MergeQueueClosed`；容量 → `merge_queued`（失败
   `MergeJournalError`）→ `put_nowait`。
3. worker 懒启动；`_run_ticket`；异常→`merge_failed`；`finally` 清理、
   `on_update`、`done.set()`。
4. `_precheck`：
   - 校验 branch/ref/sha（`check-ref-format`、hex、`--end-of-options`）；
   - `base = merge-base HEAD tip0`（真实 merge-base；失败 `invalid_ref`）；
   - `is-ancestor tip0 HEAD` → `merge_skipped`；
   - `diff --name-only -z base..tip0 --`；空且非祖先 → `merge_skipped`；
   - tracked dirty：`git diff-index --quiet HEAD --`；非 0 → dirty；
   - untracked：`status --porcelain -z --untracked-files=all --no-renames`；
   - ignored：`ls-files -o -i --exclude-standard -z`；
   - assume-unchanged/skip-worktree：`ls-files -v -z` 中 `h`/`S` 且与 changed
     重叠 → dirty（保守）；
   - **只对与 changed 路径前缀/normcase 碰撞的 untracked/ignored 判 dirty**；
     不是所有 untracked 都阻断；
   - claim（整库）→ busy；锁内重读 `head0`/`base`，若变化重跑本步；
   - `merge-tree`。
5. `_candidate`：`checkout(head0)` + `git -c core.hooksPath=/dev/null
   -c commit.gpgsign=false -c user.name=ai-coding -c user.email=merge@local
   merge --no-ff --no-verify tip0`；冲突路径
   `diff --name-only --diff-filter=U -z`。
6. `_verify`：strip 后为空 → `verified=False`；否则 `bash_denied`。
   `policy is None or mode != "enforce" or bypass` → `verify_unavailable`；
   backend 不可用 → `verify_unavailable`；`spawn_argv` 必须实际 wrap；
   `run_process_async`（cwd=tmp, roots=[tmp], cancel Event,
   max_output_chars=4000, pass_fds, env 带 `GIT_OPTIONAL_LOCKS=0`）。
   linked worktree 的 gitdir 属主仓库；verify 只支持只读 git 命令。
7. `_land`：`phase="landing"`；重取 claim；`HEAD == head0`、`tip == tip0`、
   主区干净（含新 untracked/ignored 碰撞重查）；tip 变化 → `branch_moved`；
   HEAD 变化 → 重建 + 重跑 verify（最多 2 次）；`merge --ff-only candidate`。
8. `aclose`：置 closed；当前票 `phase=="landing"` 则等待它结束，不取消；
   否则设置 verify cancel event 并取消 worker；排空 queued（`cancelled`）、
   done/on_update、await。
9. 所有失败 `try/finally` 释放 claim；所有 ticket `finally` 设置 done。
10. `enqueue` 失败由 Scheduler `_fail_ticket` 结算；Queue 不留下无主 ticket。

### 5.7 `core/scheduler.py`

1. 构造参数 `merge_queue`；`self._merge_queue`。
2. `_Record` 加 `name/base_commit/commit_error`；`_begin` 填 name。
3. `_settle/on_runner_event` 可选 `branch/commit/base_commit/commit_error`。
4. `_register_result` 填 `base_commit`。
5. `recover`：`state.kind == "merge"` → `kind="merge"`，
   `status="needs_reconciliation"`，带 branch/base/commit/name；
   其它未结算 child **保持 `kind="recovered"`**（三读口仍能看到）。
6. `merge/merge_status/_merge_source/_sync_merge_view/_fail_ticket`。
7. `_sync_merge_view`：id=`merge:<branch>`，run_id=""，kind="merge"，
   **name=`merge:<branch>`**（不与 child name 冲突），origin_session=session_id，
   base/commit/verified；终态时移 inflight。
8. `aclose` 关闭 merge_queue；不改 stop/close/fence。

### 5.8 `core/dispatch.py`

- `CONTROL_REPLAY_TOOLS` 加 `"merge_agent"`。

### 5.9 `security/permission_gate.py`

- 按 §4.8 组合 bash 判定；不新增权限名。

### 5.10 `tools/merge_agent_tool.py`（新）

- 按 §4.7；`execute` 调 `scheduler.merge`；无 scheduler → `unavailable`。
- `precheck` 只做参数校验；权限由 gate 做。
- 钩子：`write_paths_for → None`；`occupies_slot=False`；`parallel_safe=False`；
  `mutates_world=True`；`max_duration_seconds=300.0`。

### 5.11 `tools/builder.py`

- 在 `include_subagent` 分支：
  ```python
  journal = TaskJournal(workspace)
  merge_queue = MergeQueue(
      workspace, worktree_manager=manager, journal=journal,
      write_coordinator=write_coordinator, policy=sandbox_policy,
      backend=sandbox_backend,
  )
  scheduler = Scheduler(..., journal=journal, merge_queue=merge_queue)
  ...
  registry.register(MergeAgentTool(scheduler=scheduler))
  ```
- 单 journal、单 queue；子代理栈 `include_subagent=False` 不拿 merge 工具。

### 5.12 `server/world.py`

- `aclose` 在 `loop.aclose` 之前：
  ```python
  scheduler = getattr(getattr(self, "registry", None), "scheduler", None)
  aclose = getattr(scheduler, "aclose", None)
  if callable(aclose): await aclose()
  ```
- 捕获异常；只关主 registry 的 scheduler，不关子 loop 共享实例。
- **不改 `AgentLoop.aclose`**。

### 5.13 `app/commands.py`

- `/tasks` 列表：`row.kind == "merge"` 显示
  `merge <branch> <status> [已验证|未验证] [commit]`；不显示“未合并”。
- `/tasks stop <merge-target>`：先 `scheduler.merge_status(given)`；命中 merge
  则显示状态并说明“merge 票不可 stop”；否则走原 `resolve_run_id/stop`。
- 普通子代理输出不变。

### 5.14 文档

- `data-contracts.md`：C3 节（base_commit、merge journal、TaskView、权限、recover）。
- `orchestration-p2c-plan.md`：§4.1 授权名改 `merge_agent`；删主区 `--no-ff`
  回退；删 notice/bridge；§8 与 v4 一致。
- `orchestration-handoff.md`：授权名、`world.aclose`、`merge_status` 由 `/tasks`
  消费、base 最小传播面。
- `subagent-handoff-contract.md`：packet v3 加 `base_commit`（additive）。

## 6. TDD / RIPR

RIPR = Red → Implement → Pass → Refactor。每个任务缺一不可。

| 任务 | Red（失败用例） | Implement | Pass | Refactor |
|---|---|---|---|---|
| T1 Worktree base/checkout | `test_create_records_head_as_base`；`test_checkout_detaches_without_branch` | `Worktree.base_commit`、`create`、`checkout` | base==HEAD；branch==""；worktree list | 现有 `test_worktree.py` 全绿 |
| T2 元数据 | `test_base_commit_reaches_result_meta_row_view`；`test_sync_run_view_has_base` | domain/handle/meta/row/view/`_register_result` | 同步/后台/resume 后 base 一致 | subagent/registry/background 全绿 |
| T3 journal | `test_merge_events_are_terminal`（三终态集合）；`test_merge_fields_fold`（base/name/commit_error）；`test_journal_append_reports_failure` | 事件/字段/append bool | 三个终态；queued/merging pending；fold 字段 | `test_task_journal` 全绿 |
| T4 MergeQueue | 干净/空 diff/已祖先/冲突/非碰撞 untracked 不阻断/碰撞 dirty/ignored/claim busy/verify 失败/不可用/不碰主区/HEAD 前进重跑 verify/tip 前进 branch_moved/cleanup 前后/FIFO/enqueue closed | `core/merge_queue.py` | 成功主区前进；失败主区不变；临时区删除；claim 释放 | 新文件全绿 |
| T5 Scheduler.merge | 五形态解析/未结算/commit_error/in-flight/重复 no-op/registry view/recover kind/enqueue 失败/跨 session/close_info 兜底 | Scheduler 方法 | 解析/拒绝/等待/重复/`/tasks`；child 恢复仍 recovered | scheduler/registry 全绿 |
| T6 工具权限 | merge_agent WARN/verify 红线/verify agent deny/verify WARN/write_paths None/control replay/取消等待/verify 省略 verified=false | tool+gate+dispatch | WARN/DENY；不取 claim；不重放；ticket 继续；未验证可见 | permission/dispatch 全绿 |
| T7 装配 | 单 journal/queue；world.aclose 关主 scheduler；子 loop 不误关；/tasks 行；merge_status 过滤 | builder/world/commands | 单实例；主关子不关；行显示；not_found 不回归 | 全量 pytest |
| T8 真机 | tmux 两可写子 + 显式 merge；冲突；verify 失败；重启需核对 | 不改代码 | 结果与 journal 一致 | 记录命令/输出 |

### 6.1 失败矩阵

- 冲突（merge-tree + 临时区两条路径）。
- verify 失败/超时/不可用；verify 改文件只影响临时区。
- tracked dirty；untracked/ignored 碰撞；文件/目录前缀；大小写；assume-unchanged。
- claim busy/释放/二次 claim busy。
- HEAD 前进；tip 前进；二者都前进。
- cleanup 失败（落地前/后）。
- 两票 FIFO，第二票基于第一票。
- commit_error 源；未结算源；无 branch；无记录；歧义 target；其他 session 源。
- 等待方取消后 ticket 继续；`/tasks` 可见。
- 重启后 queued/merging 需核对；merged 不误报。
- 权限：merge_agent WARN；verify 被 bash 红线/agent deny/WARN 拦截。
- merge_agent 不进入 write-ahead replay。
- enqueue 满/journal 失败：ticket 不再挂起，后续同分支可重试。

### 6.2 关键实现细节

- `merge-tree`：`--no-messages -z`；首字段 tree oid 丢弃；仅 exit 1 解析。
- `status`：`-z --untracked-files=all --no-renames`；ignored 用
  `ls-files -o -i --exclude-standard -z`；`normcase` + 父目录前缀。
- `diff`：`--name-only -z base..tip --`；base 用 `merge-base head0 tip0`，
  不用 ticket.base_commit 硬算（后者只审计/回退）。
- dirty：只把与 changed 前缀/normcase 碰撞的 untracked/ignored 判脏；不是所有
  untracked 都阻断。
- `merge`：临时区 `git -c ... merge --no-ff --no-verify`（`-c` 必须在 merge 前）。
- verify：必须实际 wrap；`policy.bypass` 也拒绝；输出上限 4000；cancel Event；
  `GIT_OPTIONAL_LOCKS=0`；linked worktree gitdir 只读，verify 不得写 git 状态。
- `ff-only` 后 cleanup 失败 → `merged + cleanup_error`。
- journal `merging` 失败 → 不落地；`merged` 失败 → `merged + journal_error`。
- 所有失败 `finally` 释放 claim；所有 ticket `finally` done。
- `TaskView.is_running` 包含 queued/merging，避免被 `_prune` 当 settled。

## 7. 架构图与最终形态

### 7.1 数据流图

```
                       ┌─────────────────────────────┐
用户/模型 ── merge_agent ─▶ PermissionGate(WARN)        │
                       │  verify_cmd → bash check    │
                       └────────────┬────────────────┘
                                    ▼
                       ┌─────────────────────────────┐
                       │ Scheduler.merge             │
                       │  _merge_source              │
                       │  _merge_inflight (branch)   │
                       └────────────┬────────────────┘
                                    ▼
                       ┌─────────────────────────────┐
                       │ MergeQueue (单 worker FIFO) │
                       │  merge_queued               │
                       │  is-ancestor → diff → dirty │
                       │  claim(workspace)           │
                       │  merge-tree 预检            │
                       │  temp worktree merge        │
                       │  release claim              │
                       │  verify (enforce)           │
                       │  claim + HEAD/tip guard     │
                       │  ff-only candidate          │
                       │  merged/failed/skipped      │
                       └────────────┬────────────────┘
                                    ▼
             ┌──────────────────────┴──────────────────────┐
             ▼                                             ▼
   ToolResult(data)                             TaskRegistry(kind=merge)
   + journal                                    → /tasks / merge_status
```

### 7.2 最终形态

- 两个可写子代理各自在 `ai_sub_*` 分支提交；父显式
  `merge_agent("run-xxxx")`，主区出现 merge commit，源分支保留。
- `/tasks` 可见 `merge ai_sub_xxx merged 已验证`；取消等待后仍可见。
- 冲突/verify 失败/dirty/HEAD 前进时，主区 HEAD 与工作树不变，临时区清理。
- 重启后未完成 merge 显示“需核对”，不自动重跑；已 `merged` 不误报。
- `verify_cmd` 省略时显示“未验证”。

## 8. 风险与边界

1. **死锁**：工具 `write_paths_for=None`；队列自己取整库 claim；编排工具不占槽。
2. **长锁**：verify 释放 claim；落地前二次 claim + HEAD/tip/脏检查。
3. **两本账**：merge 只写任务 journal；文件写归 `write_ahead`。
4. **恢复**：queued/merging → needs_reconciliation；不自动重试/abort；临时区路径
   写结果，人工清理。
5. **主区原子性**：只在 verify 通过且 HEAD/tip 未变时 ff-only；失败不在主区
   merge/abort/reset。
6. **git 版本**：`merge-tree --write-tree` 需 2.38+；不可用则跳过预检，临时区
   `git merge` 给最终结论。
7. **只读/非隔离子**：无 branch → nothing_to_merge。
8. **源分支保留**：不删除、不自动 rebase。
9. **不做成功缓存**：重复 merge 由 is-ancestor 变 no-op。
10. **stop 与已入队 merge**：stop 只停子任务，不撤销已入队 merge；`/tasks stop`
    对 merge 行只显示状态并说明。
11. **关闭进程**：world 先关主 Scheduler；若恰在 ff-only 中，命令可能完成但
    journal 未写 → 重启按 merging 标需核对。
12. **队列容量**：满 → MergeQueueFull → 工具 busy；不无限排队。
13. **权限残余**：merge_agent WARN；verify 走 bash 组合判定；若 agent 允许
    merge_agent 但拒绝 bash，verify 仍被 bash 层拒绝。
14. **未加 base 的消费面**：bridge/event/tool data 不承载 base；`/tasks`、journal、
    ledger、meta 有。C4 若前端要显示再加。
15. **verify 读边界**：`roots=[tmp]` 只限写；`--ro-bind /` 仍可读 workspace 的
    config/secrets，除非 sandbox 读 denylist 覆盖。沿用现有 bash 边界；C3 不扩大，
    文档声明残余。
16. **临时区 hooks/filters**：merge 用 `--no-verify` + `core.hooksPath=/dev/null`；
    smudge/clean filter 仍可能执行，属既有 git 信任边界。

## 9. 验收

- 干净合并、冲突、verify 失败/不可用、dirty、untracked/ignored 碰撞、HEAD 前进、
  tip 前进、claim 占用、cleanup 失败、FIFO 全部符合预期；失败时主区不变。
- verify 只在临时区；改文件不影响主区；失败后临时区删除。
- target 五形态解析；只读/无分支 nothing_to_merge；commit_error → commit_failed。
- `/tasks` 显示 merge 行；取消等待后仍可查；stop 对 merge 只提示。
- 重启 queued/merging 需核对；merged 不误报。
- 全量 pytest/ruff/mypy；前端 bun test 回归。
- tmux 真机：两可写子显式合并、冲突、verify 失败、重启需核对。

## 10. 实施顺序

1. T1/T2：Worktree + 元数据 + 全量回归。
2. T3：journal 事件/字段。
3. T4：MergeQueue 纯 git 失败矩阵。
4. T5/T7：Scheduler.merge + registry + `/tasks`。
5. T6/T7：工具 + 权限 + dispatch + builder/world。
6. 全量 pytest/ruff/mypy。
7. T8 tmux 真机；文档同步；提交（用户确认后）。
8. C4。

## 11. 已决策与残余

已决策：

1. 临时区 merge commit → 主区 `ff-only`。
2. `verify_cmd` 可选；省略 `verified=false`，结果/`/tasks`/journal 显示。
3. `merge_agent` 全部 WARN；verify_cmd 按 bash 组合判定（DENY/WARN 都返回）。
4. merge 进入 `/tasks`；`merge_status` 过滤 kind=merge。
5. 不加 notice/event/bridge 的 base 字段。
6. 子 loop 不关全局 Scheduler；只由 `world.aclose` 关主实例。

残余（实现中若出现再问）：

- git < 2.38 时跳过预检（临时区 merge 给最终结论）是否接受。
- `ff-only` 被 SIGKILL 导致工作树半更新：本批只记录提示，不自动 reset；
  C4 再决定恢复命令。


## 12. 实施记录（2026-11-03）

- 已按 T1-T8 落地：`Worktree.base_commit/checkout`、元数据链路、journal 事件、
  `MergeQueue`、`Scheduler.merge/merge_status`、`TaskRegistry(kind=merge)`、
  `/tasks` merge 行、`MergeAgentTool`、permission gate 组合、builder/world 装配。
- 全量 `pytest`、`ruff check .`、`mypy ai_coding` 通过。
- 同时修正 C2 的结算映射：`on_runner_event` 把 completed/error/timeout/
  max_iterations 记为 `settled`，只有 `interrupted` 保持非终态；否则重启后的
  已完成后台子会被当成未结算，C3 无法按 journal merge。
- 新增测试：`tests/test_merge_queue.py`（干净/已祖先/冲突/dirty/untracked/
  ignored/claim busy/verify 失败/不可用/成功/branch_moved/cleanup/FIFO/journal
  失败/enqueue closed）、`tests/test_merge_agent_tool.py`（契约/WARN/data/失败映射）。
- 未执行 tmux 真机（当前会话无 TUI 交互环境）；已用真实临时 git 仓库覆盖
  candidate/verify/ff-only 主路径与失败矩阵。
