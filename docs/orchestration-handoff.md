# 编排层交接文档（P0 → P2-C3 前）

> 目的：把当前仓库"做到哪了、还剩什么、怎么在本机接着干"一次说清。
> 截至 2026-11-02，基线 `ec98912`（已推送）。C3 只出了方案，**没有代码**。

## 1. 一分钟结论

- P0a/P0b、P1、P2-A、P2-B、P2-C1、P2-C2 已完成并推送；全量 `pytest` 通过，
  `ruff`/`mypy` 通过，前端 `bun test` + `tsc` 通过，关键路径做过 tmux 真机。
- **没做**：P2-C3 合并队列（方案已写：`docs/orchestration-p2c-c3-plan.md`）、
  P2-C4 配置收口/文档/真机矩阵，以及若干已知边界（见 §4）。
- 工作区只有**一个未跟踪文件**：`docs/orchestration-p2c-c3-plan.md`。先提交它，
  再开始 C3 实现，避免交接丢失。

## 2. 仓库状态

| 项 | 值 |
|---|---|
| 路径 | `/workspace/ai-coding` |
| 分支 / HEAD | `main` / `ec98912 docs(orchestration): 记录 P2-C2 tmux 恢复结果` |
| 工作区 | 干净（仅 `docs/orchestration-p2c-c3-plan.md` 未跟踪） |
| Python | 3.14.8（要求 >=3.10；mypy 按 3.10 检查） |
| 提交链 | `c7832a7`(P0a) → `703186e..0ffd1e4`(P0b) → `23b8b32`(P1) → `ef81377`(P2-A) → `5a1a61c`(P2-B) → `67c53ae`+`f889b74`(C1) → `1ed2331`+`ec98912`(C2) |

## 3. 已完成（做什么、代码在哪）

| 期 | 交付 | 关键文件 |
|---|---|---|
| **P0a/P0b** | 删掉旧计划执行器链路：`PlanExecutor`/`PlanRunner`/`present_plan`/`plan_orchestrator` 的 session+legacy、旧状态字段/事件/前端评审卡；`PlanReport`、`workspace_guard` 先迁出再删 | `docs/orchestration-p0b-plan.md`；`ai_coding/core/workspace_guard.py` |
| **P1** | 模式投影 `plan/build/readonly`；**plan ⇒ readonly** 不变量；plan 下 `/readonly off` 拒绝；`spawn_agent(readonly=true)`；`BaseTool.mutates_world_for` 参数感知写边界 | `core/agent_loop.py:682`、`core/dispatch.py:159,451`、`tools/base.py`、`server/handlers.py`、`docs/orchestration-p1-plan.md` |
| **P2-A** | 父子信息包 v1：`SubagentBrief`/`SubagentTask`/`AgentPacket`/`SubagentResult v3`；`changed_paths`/`commit_error`/`degraded`；ledger；tool data / notice / `/tasks` / 事件 / 前端六通道同一投影；每轮 `_commit_turn` | `ai_coding/domain/subagent.py`、`core/subagent.py`、`server/bridge.py`、`docs/orchestration-p2a-plan.md` |
| **P2-B** | `WriteCoordinator`（`(root, rel)` 占用表、`*` 整库、同 owner 重入、异步 claim / 同步 hold）；bash 后台 job 生命周期 hold；`SubagentResult.conflicts` 重叠告警；容量统一（`_active` → 槽池）；`NoticeStore` 持久通知 + `wake=True` | `core/write_coordinator.py`、`core/notice_store.py`、`tools/bash_tool.py`、`docs/orchestration-p2b-plan.md` |
| **P2-C1** | `TaskRequest`；`Scheduler` 门面（run/spawn/fanout/send/interrupt/resume/close/wait/stop/task_list）；`TaskRegistry` 三读口同源；`ExecutionSlotPool`（`ai.max_execution_slots`，0=回退 `max_concurrent_subagents`）；编排/控制工具 `occupies_slot=False`；`slot_held` 防嵌套 | `domain/orchestration.py`、`core/scheduler.py`、`core/task_registry.py`、`core/execution_slots.py`、`tools/builder.py`、`docs/orchestration-p2c-c1-plan.md` |
| **P2-C2** | `TaskJournal`（JSONL、线程锁、补换行、坏行跳过、`pending` 语义）；claim/lease/fence；`TaskReaper` 续租；`cancel_tree`/`close_tree`；`stop_run` 终态闩锁（清 mailbox、拒绝 send/resume）；`recover()` → `needs_reconciliation`（不重跑）；幂等键（spawn/send/fanout） | `core/task_journal.py`、`core/scheduler.py`、`core/subagent.py`、`docs/orchestration-p2c-c2-plan.md` |

**测试覆盖（C1/C2 新增）**

- `tests/test_task_request.py`、`test_execution_slots.py`、`test_task_registry.py`、
  `test_scheduler.py`、`test_task_journal.py`
- C2 还改了 `test_subagent_handles.py`、`test_background_tasks.py`、
  `test_agents_tools.py`、`test_tool_subagent.py`

**已核验的行为**

- `slots=1` 时父等子不死锁；编排/控制工具不占槽。
- 取消后晚到结果被 fence 丢弃；`stop`（终态）≠ `interrupt`（可续）；Esc 不级联。
- 重启后未结算任务显示"需核对"，不重跑；已 `settled` 不误报。
- 三读口（`/tasks`、`subagent_list`、`list_agents`）同源。
- tmux 真机：`slots=1` 长任务、C2 新进程恢复。

> 注意：`docs/orchestration-unified-design.md` §8.1 写的"ExecutionSlotPool 未单独实现"
> 已过时，C1/C2 已落地。以本文和 C1/C2 方案为准。

## 4. 未完成（全部）

### 4.1 P2-C3 合并队列（下一步的主任务）

方案：`docs/orchestration-p2c-c3-plan.md`（已写完，待评审/实现）。要点：

- `merge_agent(target, verify_cmd?)` 显式工具，授权名 `subAgent`；`target` 支持
  request_id / run_id 前缀 / name / branch。
- `MergeQueue` 单写者 FIFO，worker 独立于父 turn；`merge_queued → merging →
  merged | merge_failed` 写 journal（**独立 `task_id="merge:<key>"`**，否则会把
  已结算的子任务重新变成未结算）。
- 流程：解析 branch/base → `git diff base..branch` 判空 → 主区 tracked 干净 +
  untracked 碰撞 → 整库写 claim → `merge-tree` 预检 → 临时 worktree 候选
  `merge --no-ff` → 释放 claim → **仅临时区**跑 `verify_cmd`（沙箱，默认 120s）→
  重取 claim + HEAD 守卫 → 主区 `merge --ff-only <候选 sha>` → `merged`。
- 新增：`Worktree.base_commit` + `checkout()`（detach）；`SubagentResult`/
  `BackgroundTask`/`TaskView`/meta/journal 增加 `base_commit`；后台结算把
  branch/commit/base 透传给 `Scheduler._settle`；`CONTROL_REPLAY_TOOLS` 加
  `merge_agent`；builder 装配 `MergeQueue`；`world.aclose` 关 scheduler。
- 工具必须 `write_paths_for → None`，否则 dispatcher 的 30s 整库 claim 会和队列
  自己的 claim 互锁。

尚未写的文件（实现时新建）：`ai_coding/core/merge_queue.py`、
`ai_coding/tools/merge_agent_tool.py`、`tests/test_merge_queue.py`、
`tests/test_merge_agent_tool.py`。

### 4.2 P2-C4 收口（C3 之后）

- `max_concurrent_tools` / `max_concurrent_subagents` → `max_execution_slots` 的
  兼容别名与配置迁移提示。
- 三读口最终形态：`subagent_list` / `parallel_research` 是否保留为兼容别名。
- `docs/data-contracts.md`、`config.example.yaml` 注释、tmux 真机全矩阵
  （两可写子 + 显式 merge、冲突、verify 失败、重启需核对）。

### 4.3 已知边界 / 技术债（不是 bug，是明确不做）

| 边界 | 影响 |
|---|---|
| fanout 只能整次取消 | `ResearchFanOut` 不暴露子 task；`cancel_tree(token)` 取消整次 |
| 无跨进程租约/恢复 | 单进程契约；重启只标 `needs_reconciliation` |
| 后台子不自动恢复、不重跑 | 崩溃后必须人工核对 |
| `changed_paths` 不含 bash 写入 | P2-C3 必须用 `git diff base..branch` 判定 |
| 不做自动合并 | 必须显式 `merge_agent` |
| 无 WAL/收据链/Raft | 有意不照搬 ACCL |
| `bash` 一律按整库 `*` claim | 长命令会串行化其它写者；只读命令也占 |
| 槽池与模型步 | 模型步不占槽（有意偏离 unified-design §4） |

## 5. 转本地开发：环境与命令

### 5.1 拉取与安装

```bash
git clone <repo> ai-coding && cd ai-coding
git checkout main && git log --oneline -3     # 应看到 ec98912
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                        # pytest/pytest-asyncio/ruff/mypy
bun --version                                  # 前端需要 >=1.2
```

首次启动 TUI 会复制前端并 `bun install`（需网络，上限 600s）；开发热改用
`AI_CODING_FRONTEND=$PWD/ai_coding/frontend`。

### 5.2 校验命令

```bash
python -m pytest                      # 全量（addopts: -q -rs --strict-markers）
python -m pytest tests/test_scheduler.py tests/test_task_journal.py -q
ruff check ai_coding tests
mypy ai_coding                        # strict，按 py3.10
cd ai_coding/frontend && bun test && bun run typecheck
```

说明：

- 无 `bwrap` 的环境会有约 28 个 skip（沙箱用例），不是失败。
- 涉及真实 git 的测试：无 `git` 时按 `test_worktree.py:19-20` 的惯例 `skip`。
- 不要在仓库根跑 `git worktree list` 之外的危险操作；子代理 worktree 落在
  `worktrees/`（已在 `.gitignore`）。

### 5.3 运行期数据在哪（排查用）

| 位置 | 内容 |
|---|---|
| `<ws>/.ai-coding/tasks/<session>.jsonl` | 任务 journal（queued/claimed/running/settled/stopped/closed/pending…） |
| `<ws>/.ai-coding/agents/<run_id>.json` | 后台子代理 meta（branch/commit/status/lastStatus） |
| `<ws>/.ai-coding/agents/…` + ledger | 结算包（`SubagentResult` 全文） |
| `<ws>/worktrees/<name>_<token>/` | 子代理 worktree（临时区退出即删；后台子 close 时删） |
| `ai_sub_<name>_<token>` 分支 | 子代理提交所在分支，合并前一直保留 |

排查顺序：先看 journal 最后事件 → 再看 meta 的 `lastStatus` → 最后看分支
`git log --oneline ai_sub_<...>`。

## 6. 怎么交接（给下一位开发者 / 下一个 AI 会话）

### 6.1 交接前动作

```bash
git add docs/orchestration-p2c-c3-plan.md
git commit -m "docs(orchestration): P2-C3 合并队列方案"
git push
```

### 6.2 必读顺序（约 30 分钟）

1. `docs/orchestration-p2c-plan.md` —— P2 总方案与分期。
2. `docs/orchestration-p2c-c1-plan.md` → `-c2-plan.md` —— 已落地的契约与偏离。
3. `docs/orchestration-p2c-c3-plan.md` —— 待做方案（含外部调研结论与坑）。
4. 代码：`core/scheduler.py` → `core/task_journal.py` → `core/write_coordinator.py`
   → `core/worktree.py` → `core/subagent.py`（只看 `_run_isolated`/`_commit_turn`/
   `_settle` 相关段）→ `tools/builder.py`（组装）。
5. 测试：`tests/test_scheduler.py`、`tests/test_task_journal.py`、`tests/test_worktree.py`。

### 6.3 可以直接复制给下一位 AI 的开场提示

```text
仓库 /workspace/ai-coding，基线 ec98912（main）。已完成 P0-P2-C2，C3 只有方案。
请先读：docs/orchestration-p2c-c3-plan.md、docs/orchestration-p2c-c2-plan.md、
core/scheduler.py、core/worktree.py、core/write_coordinator.py、tools/builder.py。

任务：按 C3 方案先做独立评审（找 P0/P1，别改风格），修订方案后再 TDD 实现：
1) Worktree.base_commit + checkout；元数据 base_commit 全链路；
2) core/merge_queue.py + 失败矩阵测试（冲突/verify/dirty/untracked/HEAD 前进/claim 忙/串行）；
3) Scheduler.merge/merge_status + tools/merge_agent_tool.py；
4) builder/world/bridge 装配；全量 pytest + ruff + mypy；tmux 真机两可写子显式合并。
约束：不改 P1 wire/P2-A packet 语义/P2-B claim 键/C2 fence-stop-recover；
merge 用独立 task_id；工具 write_paths_for=None；不自动合并；不删源分支。
每完成一步跑全量校验并汇报，不要一次做完再报。
```

## 7. 接下来该干啥（按顺序）

| 步骤 | 动作 | 完成标准 |
|---|---|---|
| 1 | 提交 C3 方案文档 | `git status` 干净；C3 方案在 main |
| 2 | 独立评审 C3 方案（1–2 轮）+ 修订 | 无 P0；P1 全部落入方案（含 §10 两个待拍板项） |
| 3 | 元数据 + `Worktree.base_commit/checkout` | 新用例绿；全量不回归 |
| 4 | `MergeQueue`（纯 git，不接工具） | 冲突/verify/dirty/untracked/HEAD 前进/claim/串行/清理失败全绿 |
| 5 | `Scheduler.merge` + `merge_agent` 工具 | target 四形态、只读子、取消后 `merge_status` 全绿 |
| 6 | builder/world/bridge 装配 | 全量 pytest/ruff/mypy 绿 |
| 7 | tmux 真机 | 两可写子显式合并成功；冲突失败；verify 失败回退；主区不变 |
| 8 | 提交 C3；再做 C4 | 提交信息按 `feat(orchestration): P2-C3 …` |

### 两个待拍板项（做步骤 2 时确认）

1. 落地方式：临时区生成 merge commit 后主区 `merge --ff-only`（方案默认，主区无
   `MERGE_HEAD` 窗口）vs 主区直接 `merge --no-ff`。
2. `verify_cmd` 不传时是否允许直接合并（方案默认：允许；传了就必须通过）。

## 8. 不要做的事

- 不要自动合并、不要自动重跑已写过的子代理、不要恢复后台运行中的子代理。
- 不要为代码文件引入 ACCL 的 JSON `merge3`；不要解析本地化 git 文本。
- 不要新增第三张持久任务表；`TaskRegistry` 是只读视图。
- 不要改 P1 wire（`normal/plan` + `readonly`）、P2-A packet 字段语义、P2-B
  claim 键、C2 的 fence/stop/recover 语义。
- 不要在 `merge_agent` 上打开 dispatcher 的写 claim（会与队列互锁）。
- 不要删除子代理源分支（保留重试与审计）。

## 9. 相关文档索引

- 总方案：`docs/orchestration-p2c-plan.md`
- 已完成分期：`docs/orchestration-p2a-plan.md`、`-p2b-plan.md`、`-p2c-c1-plan.md`、
  `-p2c-c2-plan.md`
- 待做：`docs/orchestration-p2c-c3-plan.md`
- 更早背景：`docs/orchestration-unified-design.md`、`docs/orchestration-unified-research.md`、
  `docs/orchestration-p0b-plan.md`、`docs/orchestration-p1-plan.md`
- 运行手册：`docs/how-to-run.md`
