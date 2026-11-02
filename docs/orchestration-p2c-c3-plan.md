# P2-C3 实施方案（评审稿）：合并队列与显式 `merge_agent`

> 前置：P2-C1（`67c53ae`/`f889b74`）、P2-C2（`1ed2331`/`ec98912`）已完成并推送。
> 总体方案 `docs/orchestration-p2c-plan.md` §4。本稿只做 C3，可独立回退。
> 用户已拍板：**显式工具触发**、不自动合并；verify 在**临时 worktree**执行；
> 失败不碰主区；恢复以 journal 为唯一结算依据；不照搬 JSON-aware merge3 / WAL /
> 收据链 / Raft。有歧义再拍板。

## 0. 目标与非目标

**做**

1. `merge_agent(target, verify_cmd?)` 显式工具；`target` 支持 request_id / run_id /
   run-id 前缀 / name / branch。
2. `MergeQueue`：单写者 FIFO；出队后**重新取主区写 claim + 重验 HEAD**才落地。
3. git 3-way：`merge-tree` 预检 + **临时 worktree**候选合并 + 沙箱 verify。
4. 失败回滚：冲突/verify 失败/dirty/HEAD 前进都不改变主区；临时区清理。
5. `Worktree.base_commit`；`SubagentResult`/`BackgroundTask`/meta/TaskView/journal
   加法字段，供 merge 定位基线与差异。
6. journal `merge_queued/merging/merged/merge_failed`；重启时 `merging` 标
   `needs_reconciliation`，不自动重试。

**不做**

- 不改 P1 wire、P2-A packet 语义、P2-B claim 键、C2 fence/停止/恢复语义。
- 不做自动合并、不做 batch/speculation、不做跨进程队列、不持久化队列。
- 不用 ACCL 的 JSON `merge3` 处理代码文件；不解析本地化 git 文本。
- 不删除源分支（保留审计与重试）；不自动 rebase 源分支。

## 1. 外部调研结论（可复用 / 不采用）

| # | 来源 | 结论 | 采用 |
|---|---|---|---|
| E1 | ACCL `harness/merge_queue.py:27-165` | 单写者 FIFO，`QUEUED→RUNNING→DONE`，容量/等待超时/仅未开始可取消；**出队→重验→提交**串成一步，关闭 verify→commit 的 TOCTOU 窗口 | 直接采用“单写者 + 出队后重验” |
| E2 | ACCL `harness/facade.py:538-545` | `apply_queued` 入队并等待；不做持久化/批量 | 采用“显式入队 + 等待” |
| E3 | ACCL `harness/facade.py:670-730` | verify 必须在隔离副本里跑；无 worktree 则 fail-closed；验证树 `finally` 清理 | 采用“临时 worktree 内 verify” |
| E4 | ACCL `harness/rebase.py:217-315` | `merge3` 是 JSON-aware（数组集合语义、modify/modify 冲突） | **不采用**于代码文件 |
| E5 | Codex 官方/社区 | 多代理用 **worktree 隔离**，不自动合并；由人挑选并合并 | 采用“隔离 + 显式合并” |
| E6 | Git 官方文档 | `merge-tree --write-tree`：无冲突退出 0，冲突退出 1，其他错误非 0/1；`--name-only -z` 给出冲突路径 | 采用预检，但以临时区真实 merge 为准 |
| E7 | GitHub merge queue | 分支级串行集成，避免“各自验证通过、合入后坏” | 采用“单一主区写者” |

外部资料不能确认/有分歧：`merge-tree --name-only -z` 的具体字段顺序随版本变化
（实测 2.43 为 `tree\0path\0\0`），因此解析只取“首个空字段前”的路径；预检失败
（非 0/1）降级为“跳过预检”，由临时区 `git merge` 给最终结论。

## 2. 本地现状（精确证据）

| # | 事实 | 证据 |
|---|---|---|
| S1 | `Worktree` 只有 name/branch/path，无 base；创建隐式基于 HEAD | `ai_coding/core/worktree.py:37-44`、`106-116` |
| S2 | 子代理隔离在 `temporary()` 中跑，退出即删目录；分支/commit 保留 | `ai_coding/core/subagent.py:517-537`、`core/worktree.py:179-189` |
| S3 | 后台子用持久 worktree；handle 记录 branch，无 base | `ai_coding/core/subagent.py:1395-1411`、`160-163` |
| S4 | `_commit_turn` 每 turn 提交，结果带 commit；无 base | `ai_coding/core/subagent.py:1994-2019` |
| S5 | meta 有 `worktreeBranch/commit`，无 base；resume 从 meta 重建 handle | `ai_coding/core/subagent.py:2063-2108`、`1041-1074` |
| S6 | `SubagentResult` 有 branch/commit/changed_paths/commit_error，无 base | `ai_coding/domain/subagent.py:267-363` |
| S7 | `BackgroundTask`/`TaskView` 同源字段齐全，无 base | `ai_coding/domain/subagent.py:587-632`、`core/task_registry.py:25-111` |
| S8 | Scheduler 无 merge；`_settle` 写 journal 的 branch/commit；`resolve_target` 只按 id/run/name | `ai_coding/core/scheduler.py:583-636`、`693-710` |
| S9 | journal 终态集合固定 4 个；fold 接受任意 event | `ai_coding/core/task_journal.py:28`、`127-153` |
| S10 | `WriteCoordinator.claim(paths=())` 即整库 claim；owner 可重入 | `ai_coding/core/write_coordinator.py:110-137`、`240-249` |
| S11 | `git()` 是唯一有界 git 调用；无 merge-tree/worktree 封装 | `ai_coding/core/gitcmd.py:17-46` |
| S12 | 沙箱执行：`spawn_argv` + `run_process_async`；roots 被 `--bind` 可写 | `ai_coding/security/process_sandbox.py:389-457`、`918-931`、`infra/subprocess_runner.py:259-320` |
| S13 | bash 的 verify/执行范式可直接复用 | `ai_coding/tools/bash_tool.py:545-573` |
| S14 | 工具面：`occupies_slot`、`write_paths_for=None` 表示不取 claim | `ai_coding/tools/base.py:223-227`、`304-315`、`core/dispatch.py:759-787` |
| S15 | 编排工具不占槽；spawn 返回 request_id+run_id | `ai_coding/tools/builder.py:470-544`、`agents_tool.py` |

**关键坑（必须先解决）**

- `merge_agent` 若按默认 `mutates_world=True` 走 dispatcher 的 30s 整库 claim，会与
  MergeQueue 自己的 claim **不同 owner 死锁**。因此该工具 `write_paths_for → None`，
  由队列在落地阶段持 claim（`dispatch.py:759-787`）。
- `merge` 事件若复用子任务 `task_id`，会把已 `settled` 的子任务重新变成“未结算”，
  重启误报“需核对”。因此 merge 用独立 `task_id="merge:<key>"`，只把子任务 id 放
  `parent_id`（`task_journal.py:97-99`、`scheduler.py:506-527`）。

## 3. 确定的数据流

```
merge_agent(target, verify_cmd?)
  → Scheduler.merge(target, verify_cmd)
      resolve target → {run_id, task_id, branch, base_commit, session}
        registry.get(id/run/name) → branch 精确匹配 → journal 折叠兜底
        record/journal 未终态 → not_settled（只允许已结算的子）
      无 branch → nothing_to_merge；完全无记录 → not_found
      同一 ticket 进行中 → 等它（不做跨请求成功缓存，见 §7.9）
  → MergeQueue.enqueue(MergeTicket)            # 单写者，worker 独立于父 turn
      journal merge_queued
      worker：
        1) branch 存在性 / tip = rev-parse <branch>
        2) base = request.base_commit or merge-base HEAD <branch>
        3) git diff --name-only base..branch          # bash 写入也可见
           空 → nothing_to_merge
           branch 已是 HEAD 祖先 → nothing_to_merge
        4) 主区 tracked 干净：status --porcelain --untracked-files=no
           untracked 与 changed 路径求交 → dirty
        5) 取主区整库 claim(owner="merge:<key>", paths=(), timeout=30) → busy
        6) head0 = rev-parse HEAD
           merge-tree --write-tree --name-only -z HEAD <branch>
             exit 1 → conflict(paths)；非 0/1 → 跳过预检
        7) 临时 worktree：worktree add --detach <tmp> head0
           在 <tmp> merge --no-ff <branch> -m "merge: <key>"
             冲突 → conflict(paths from diff --diff-filter=U) → 清理
           得到候选 merge sha
        8) 释放 claim（verify 不长期占主区写锁）
        9) verify（仅临时区，cwd=<tmp>，sandbox spawn_argv，默认 120s）
             非 0 / 超时 / sandbox 不可用 → verify_failed（输出尾部）
        10) 重新取 claim → busy
            HEAD 仍 == head0？变了 → head_moved，重试（最多 2 次）
            重验 tracked 干净 + untracked 碰撞
            git merge --ff-only <merge_sha>      # 主区只此一次写
        11) journal merged + 结果 {commit=merge_sha, main_head}
      finally：cleanup 临时 worktree；任何失败都释放 claim
  → ToolResult(data) / Scheduler.merge_status(target)
```

**顺序约束**：临时 worktree 的删除一律在 `ff-only` 之后（成功或本次尝试失败）；
候选 merge commit 未挂任何 ref，删除后即成为不可达对象，所以不能先删后落地。
`head_moved` 重试时重建临时区，不复用旧候选。

**verify 的沙箱边界**：`spawn_argv(cmd, policy, backend, roots=[tmp_path])`，
`cwd=tmp_path`；主仓库 `.git` 在 bwrap 下只读（`--ro-bind /`），因此 verify 里不要
依赖写 git 状态（需要时只用只读 git 命令）。`policy=off` 时直接
`run_process_async`，与 bash 一致；`enforce` 且无 backend 则 fail-closed 为
`verify_failed`（reason 透出）。

**串行性**：worker 是单写者；两个 merge ticket 按 FIFO 依次落地，后一个在出队后
重新读 HEAD/base，因此看到前一个的合并结果（对标 E1/E7）。

**取消**：父 turn 取消只停止“等待”，ticket 继续；`merge_status(target)` 事后可查。
worker 只随 `Scheduler.aclose()` 退出。

## 4. 接口与数据

### 4.1 `MergeQueue`（`core/merge_queue.py`，新）

```python
@dataclass
class MergeTicket:
    ticket_id: str
    key: str                  # run_id 或 request_id
    task_id: str              # 子任务 id（只作 parent_id）
    run_id: str
    branch: str
    base_commit: str | None
    verify_cmd: str | None
    session_id: str
    state: str                # queued|merging|done
    outcome: dict | None
    done: asyncio.Event

class MergeQueue:
    def __init__(self, repo_root, *, worktree_manager, journal=None,
                 write_coordinator=None, policy=None, backend=None,
                 capacity=32, verify_timeout=120.0, claim_timeout=30.0,
                 max_attempts=2): ...
    def enqueue(self, request) -> MergeTicket      # 满 → raises MergeQueueFull
    async def wait(self, ticket) -> dict
    def status(self, key) -> dict | None
    async def aclose(self) -> None
```

- 队列 `asyncio.Queue` + 懒启动一个 worker（`asyncio.create_task`），不与父 turn
  共享生命周期；单 ticket 异常只结算该 ticket，worker 继续。
- 等待用 `done` Event（不是 Future）：等待方被取消不会污染 ticket；worker 永不写
  已关闭的 Future。
- 结果 dict：`status, branch, base_commit, commit, main_head, conflicting_paths,
  verify{exit_code, output}, error, attempts, worktree_path(失败时)`。

### 4.2 `Scheduler.merge`

```python
async def merge(self, target: str, verify_cmd: str | None = None) -> dict
def merge_status(self, target: str) -> dict
```

- `_merge_source(target)`：registry（id/run 前缀/name）→ branch 精确匹配 →
  journal 折叠（task_id/run_id/branch）。返回 `_MergeSource(key, task_id, run_id,
  branch, base_commit, session_id, terminal)`。journal 兜底时用 `is_terminal`
  判断（merge 施工中不能只看 registry）。
- 未终态 → `not_settled`（含重启后的 `needs_reconciliation`）；`branch is None` →
  `nothing_to_merge`；完全无记录 → `not_found`。
- `self._merge_inflight: dict[str, MergeTicket]`：同 key **进行中** → 等同一 ticket；
  已结束则**总是新 ticket**（不做成功缓存：后续 turn 可能在同一分支追加提交，
  缓存会吞掉新工作）。已完成的分支由队列的 `merge-base --is-ancestor` 判为
  `nothing_to_merge`，语义仍幂等。
- `self._merge_outcomes: dict[str, dict]`：最近一次结果（供
  `merge_status(target)` 与等待方取消后查询）。
- `merge_queue is None` → `{"status": "unavailable"}`（测试/未装配）。

### 4.3 `Worktree`

```python
@dataclass(frozen=True)
class Worktree:
    name: str
    branch: str
    path: Path
    base_commit: str | None = None
```

- `create()`：先 `rev-parse HEAD` 得 base，再 `worktree add -b <branch> <path> <base>`
  （不再隐式用 HEAD 的二次读取）。
- 新增 `checkout(name, commit) -> Worktree`：`worktree add --detach <path> <commit>`，
  `branch=""`，`base_commit=commit`；merge 临时区专用。
- `temporary()` 保持语义；新增 `preview(commit)` 上下文（可选，merge 用显式
  `checkout`+`finally` 以便在 verify 前后分开持锁）。

### 4.4 元数据加法（全部只加不改）

| 位置 | 字段 | 用途 |
|---|---|---|
| `SubagentResult` / `to_dict` | `base_commit` | packet/ledger 可见 |
| `BackgroundTask` / `to_dict` | `base_commit` | 活句柄投影 |
| `TaskView` / `to_dict` | `base_commit` | 三读口/merge 解析 |
| `AgentHandle` / meta | `base_commit` / `"baseCommit"` | 跨 turn/重启 |
| `TaskRecordState` / fold | `base_commit` | 重启后 merge 定位 |
| `_details_projection` / `server/bridge.py` | `base_commit` / `baseCommit` | 事件可见 |

取值：同步隔离路径 `replace(result, base_commit=wt.base_commit)`；后台
`handle.base_commit=wt.base_commit`，`_commit_turn`/`_with_handle_branch`/`_record_handle_facts`
同步；resume 从 meta 恢复。

**必须补的一条链路**：后台结算走 `_finish_background → _scheduler_event →
Scheduler.on_runner_event → _settle`，当前 `_settle` 拿不到 branch/commit（只有同步
`run` 的 result 分支才写）。为使 journal 成为重启后 merge 的可用索引，给
`on_runner_event/_settle` 增加可选 `branch/commit/base_commit`，由
`_scheduler_event` 从 `result` 透传（加法参数，不改变 C2 判定）。

### 4.5 journal 事件

- 新事件：`merge_queued` / `merging` / `merged` / `merge_failed`。
- `TERMINAL_EVENTS` 增加 `merged`、`merge_failed`；`merging` 非终态 → 重启后
  `recover()` 自动登记 `needs_reconciliation`（沿用 `scheduler.py:506-527`）。
- merge 记录：`task_id="merge:<key>"`、`parent_id=<子 task_id>`、`kind="merge"`、
  `branch/commit/error`。

### 4.6 `MergeAgentTool`（`tools/merge_agent_tool.py`，新）

- name `merge_agent`；permission `subAgent`；`parallel_safe=False`（barrier）、
  `occupies_slot=False`、`mutates_world=True`（plan/readonly 拒绝）、
  `write_paths_for → None`（不取 dispatcher claim）。
- params：`target`（必填 string）、`verify_cmd`（可选 string，空串视为未提供）。
- 结果：`merged` → success=True；`nothing_to_merge` → success=True（无需合并）；
  其余 → success=False + `error=status`。
- 失败保留分支，可再次调用重试。

## 5. 逐文件改动

| 文件 | 改动 |
|---|---|
| `core/merge_queue.py`（新） | 队列/worker/预检/临时 worktree/verify/落地/清理/journal |
| `tools/merge_agent_tool.py`（新） | 显式工具 + 渲染 + 参数校验 |
| `core/worktree.py` | `base_commit`；`create` 显式基线；`checkout`（detach） |
| `domain/subagent.py` | `SubagentResult.base_commit`、`BackgroundTask.base_commit` + `to_dict` |
| `core/subagent.py` | handle/meta/base 传播；`_details_projection`；`_commit_turn` 注入 base |
| `core/task_registry.py` | `TaskView.base_commit` + `to_dict`/`from_background`/merge |
| `core/task_journal.py` | `TERMINAL_EVENTS` 增加 `merged/merge_failed`；fold 保存 base |
| `core/scheduler.py` | `merge/merge_status/_merge_source/_merge_inflight/_merge_outcomes`；构造参数 `merge_queue`；`on_runner_event/_settle` 接收 branch/commit/base；`aclose` 关队列 |
| `core/subagent.py`（续） | `_scheduler_event` 透传 branch/commit/base_commit 给 C2 结算 |
| `core/dispatch.py` | `CONTROL_REPLAY_TOOLS` 增加 `merge_agent`（不重放可能过期的合并结果） |
| `tools/builder.py` | 构造 `MergeQueue`（journal/policy/backend/coordinator）并注入 Scheduler；注册工具 |
| `server/world.py` | `aclose` 调 `registry.scheduler.aclose()` |
| `server/bridge.py` | `baseCommit` 透传（加法） |
| `tests/...` | 见 §6 |

**不改**：P1 wire、P2-A packet 形态（只加字段）、P2-B claim 语义、C2 fence/stop/
recover、`WorktreeManager.create` 的既有失败契约。

## 6. TDD / RIPR 用例

| # | 文件 | 用例 | 期望 |
|---|---|---|---|
| R1 | `tests/test_worktree.py` | `create` 后 `base_commit == HEAD`；`checkout` detach 且不建分支 | — |
| R2 | `tests/test_merge_queue.py`（新） | 干净合并 | `merged`；主区 HEAD 前进；merge commit 有两个父 |
| R3 | 同上 | 空 diff / 已是祖先 | `nothing_to_merge`；主区不变 |
| R4 | 同上 | 冲突 | `conflict` + paths；主区 HEAD 不变；临时区删除 |
| R5 | 同上 | verify 失败/超时 | `verify_failed`；主区不变；输出尾部；claim 已释放 |
| R6 | 同上 | verify 改文件 | 主区文件不被 verify 改动污染 |
| R7 | 同上 | 主区 tracked 改动 | `dirty`；不进入合并 |
| R8 | 同上 | untracked 与新增路径碰撞 | `dirty` |
| R9 | 同上 | 他人持整库 claim | `busy`（短 timeout）；释放后可合并 |
| R10 | 同上 | verify 中 HEAD 前进（verify_cmd 提交空 commit，一次） | 第二次尝试成功 `merged` |
| R11 | 同上 | 每次都 HEAD 前进 | `head_moved`；主区安全；claim 释放 |
| R12 | 同上 | 清理失败 | 失败路径 `unrecoverable`；worker 继续处理下一票 |
| R13 | 同上 | 两票并发 | FIFO 串行；两分支都合入；第二次基于第一次结果 |
| R14 | `tests/test_merge_agent_tool.py`（新） | target=request_id/run_id 前缀/branch/name | 都解析到同一分支 |
| R15 | 同上 | 只读子（无 branch）/未知 target | `nothing_to_merge` / `not_found` |
| R16 | 同上 | 参数校验、data 字段、success 映射 | — |
| R17 | 同上 | 等待方取消后 `merge_status` | ticket 继续并 `merged` |
| R18 | `tests/test_scheduler.py` | 进行中去重；已结束再调用走新 ticket；未结算拒绝 | 并发同一个 ticket / 已合并分支得 `nothing_to_merge` / `not_settled` |
| R19 | `tests/test_task_journal.py` | `merged/merge_failed` 终态；`merging` 非终态 | recover 只标 `merging` |
| R20 | `tests/test_subagent_runner.py` / `test_worktree.py` | base_commit 进 result/meta/TaskView | — |
| R21 | `tests/test_scheduler.py` | 后台 `_scheduler_event` 结算写 branch/commit/base 进 journal | 重启后可按 branch 找到 merge 源 |
| R22 | `tests/test_dispatch.py`（既有） | `merge_agent` 不进入 write-ahead 重放 | 崩溃恢复后返回新调用结果 |
| P | 全量 | `pytest`、`ruff`、`mypy`；前端不涉及 | 0 failed |

**同步修改**：`test_builder.py`（MergeQueue 注入）、`test_subagent_handles.py`
（base 恢复）、`test_server_bridge.py`（baseCommit 透传）。

**复用既有 git fixture**：`tests/test_worktree.py:30-42` 已有 `git_repo`
（init/config/初始提交）；`tests/test_crash_recovery.py:269-282` 有 `_git_repo`。
把前者提为 `tests/conftest.py` 的共享 fixture（或在新测试文件复用同一构造），
不重新造轮子；无 git 时沿用 `pytest.skip` 惯例（`test_worktree.py:19-20`）。

**突变验证**：去掉二次 claim 重验 → R10/R11 红；去掉 untracked 碰撞 → R8 红；
去掉 cleanup finally → R4/R12 红；把 merge task_id 复用子任务 → R19 红。

## 7. 风险与边界

1. **死锁**：工具不取 dispatcher claim，队列自己取；`write_paths_for=None` 是关键。
2. **长锁**：verify 期间释放 claim，落地前二次 claim + 重验 HEAD；避免 verify 120s
   阻塞其它写。
3. **两本账**：merge 只写任务 journal，文件写仍归 `write_ahead`。
4. **恢复**：`merging` 一律 `needs_reconciliation`；不自动重试、不自动 abort。临时区
   可能残留，结果里给出路径，由人清理。
5. **主区原子性**：只在 verify 通过且 HEAD 未变时 `merge --ff-only`；失败路径从不
   在主区执行 merge/abort/reset。
6. **git 版本**：`merge-tree --write-tree` 需要 2.38+；不可用则跳过预检，最终由
   临时区 `git merge` 判定。
7. **只读子/非隔离子**：无 branch → `nothing_to_merge`，不误报失败。
8. **源分支保留**：不删除，便于重试/审计；不做自动 rebase。
9. **不做跨请求成功缓存**：子代理同一分支可能在后续 turn 继续提交；缓存会吞掉
   新工作。已完成分支再合并由 `is-ancestor` 给出 `nothing_to_merge`。
10. **stop 与已入队 merge**：`Scheduler.stop` 只停子任务，不撤销已入队的 merge；
    ticket 已捕获 branch/base，仍会落地。需要撤销时只能人工处理；本批不做取消票
    （ACCL 也只允许取消未开始的票）。
11. **关闭进程**：`Scheduler.aclose()` 取消 worker；若恰在 `git merge --ff-only`
    执行中，命令可能已不可中断地完成（`run_process_async` 的线程语义）。因此
    关闭后主区可能已合并但 journal 未写 → 重启按 `merging` 标需核对。
12. **队列容量**：满时返回 `busy`（不排队无限增长）；显式合并调用很少，32 足够。

## 8. 验收

- 干净合并、冲突、verify 失败、dirty、untracked 碰撞、HEAD 前进、claim 占用、
  清理失败、串行两票全部符合预期；失败时主区 HEAD 与工作树不变。
- verify 只在临时区执行，改文件不影响主区；失败后临时区被删。
- target 四种形态都能解析；只读子正确返回 `nothing_to_merge`。
- 等待方取消后 merge 仍完成并可查询。
- 重启后 `merging` 显示“需核对”，不自动重跑；`merged` 不误报。
- 全量 `pytest` 0 failed；`ruff`/`mypy` 通过。

## 9. 实施顺序

1. 元数据与 `Worktree.base_commit/checkout`（R1/R20）+ 全量回归。
2. `MergeQueue` 纯 git/失败矩阵（R2-R13），先不接工具。
3. Scheduler 解析/缓存/journal（R18/R19），再接 `merge_agent`（R14-R17）。
4. builder/world/bridge 装配；全量校验；tmux 真机（两可写子 + 显式 merge，
   冲突/verify 失败各一次）。
5. 独立评审（2 轮）→ 修复 → 提交推送。

## 10. 已决策 / 待拍板

已决策（沿用总体方案与既有实践）：显式合并、临时 worktree verify、git 3-way、
失败保留分支、单写者、恢复不自动重试、verify 沙箱 fail-closed。

待拍板（评审后若仍存在才问）：

1. 合并落地方式：临时区产生 merge commit 后主区 `merge --ff-only`（本稿）vs 主区
   `merge --no-ff`（会在主区留 `MERGE_HEAD` 窗口）。本稿选前者。
2. `verify_cmd` 缺省时是否允许“无验证直接合并”（本稿：允许，工具可选）。
