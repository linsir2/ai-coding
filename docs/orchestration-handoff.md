# 编排层交接文档（P0 → P2-C3 完成）

> 目的：把当前仓库"做到哪了、还剩什么、怎么在本机接着干"一次说清。
> 截至 2026-11-05，基线 `f8461c6`（已推送，工作区干净）。
> C3 已实现；之后又合入 EffectLedger/完成门禁、计划身份、读账本与运行时修复。

## 1. 一分钟结论

- P0a/P0b、P1、P2-A、P2-B、P2-C1、P2-C2、**P2-C3** 全部完成并推送。
- C3 之后还有一大批运行时/体验修复（见 §3.2），其中三条有**语义变化**：
  后台 job 不再持整库写锁、`todo_write`/`update_plan` 完成必须有回执或 verify、
  `update_plan` 步骤身份改为 id 优先。
- **没做**：P2-C4 配置收口、tmux 全矩阵的剩余用例、audit 文档列的 4 项后续。
- 工作区干净；最近提交 `f8461c6 fix(ui): 轮询回收失配的 working 状态`。

## 2. 仓库状态

| 项 | 值 |
|---|---|
| 路径 | `/workspace/ai-coding` |
| 分支 / HEAD | `main` / `f8461c6` |
| 工作区 | 干净 |
| Python | 3.14.8（要求 >=3.10；mypy 按 3.10 检查） |
| 虚拟环境 | `.venv/`（依赖已装；系统 python 没有 pydantic） |
| 提交链 | `c7832a7`(P0a) → `703186e..0ffd1e4`(P0b) → `23b8b32`(P1) → `ef81377`(P2-A) → `5a1a61c`(P2-B) → `67c53ae`+`f889b74`(C1) → `1ed2331`+`ec98912`(C2) → `ec0522a`+`fe73a8d`(C3) → `6fb18a9..f8461c6`(运行时/UI 修复) |

## 3. 已完成

### 3.1 编排主线

| 期 | 交付 | 关键文件 |
|---|---|---|
| **P0a/P0b** | 删掉旧计划执行器链路（`PlanExecutor`/`PlanRunner`/`present_plan`/session+legacy/旧状态与前端评审卡）；`PlanReport`、`workspace_guard` 先迁出再删 | `core/workspace_guard.py`、`docs/orchestration-p0b-plan.md` |
| **P1** | 模式投影 `plan/build/readonly`；**plan ⇒ readonly**；plan 下 `/readonly off` 拒绝；`spawn_agent(readonly=true)`；`mutates_world_for` 参数感知写边界 | `core/agent_loop.py:682`、`core/dispatch.py:159,451`、`tools/base.py` |
| **P2-A** | 父子信息包 v1：`SubagentBrief`/`SubagentTask`/`AgentPacket`/`SubagentResult v3`；`changed_paths`/`commit_error`/`degraded`；ledger；六通道同一投影；每轮 `_commit_turn` | `domain/subagent.py`、`core/subagent.py`、`server/bridge.py` |
| **P2-B** | `WriteCoordinator`（`(root, rel)`、`*` 整库、同 owner 重入）；bash 后台 hold（**后被 C3 后批次取消**）；`conflicts` 重叠告警；`NoticeStore` 持久通知 | `core/write_coordinator.py`、`core/notice_store.py` |
| **P2-C1** | `TaskRequest`；`Scheduler` 门面；`TaskRegistry` 三读口同源；`ExecutionSlotPool`（`ai.max_execution_slots`）；编排/控制工具不占槽；`slot_held` 防嵌套 | `domain/orchestration.py`、`core/scheduler.py`、`core/task_registry.py`、`core/execution_slots.py` |
| **P2-C2** | `TaskJournal`（JSONL/线程锁/半行容错/`pending`）；claim/lease/fence；`TaskReaper`；`cancel_tree`/`close_tree`；`stop_run` 终态闩锁；`recover()` → `needs_reconciliation`；幂等键 | `core/task_journal.py`、`core/scheduler.py`、`core/subagent.py` |
| **P2-C3** | `merge_agent` 显式工具 + `MergeQueue` 单写者；`Worktree.base_commit/checkout`；journal `merge_*`；`TaskRegistry(kind=merge)`；`verify_cmd` 按 bash 组合判定；失败矩阵测试 | `core/merge_queue.py`、`tools/merge_agent_tool.py`、`tests/test_merge_queue.py`、`docs/orchestration-p2c-c3-plan.md` §12 |

### 3.2 C3 之后合入的运行时批次

| 主题 | 变更 | 关键文件 |
|---|---|---|
| **EffectLedger / 完成门禁** | 会话级效果账本：原始 read 证据（版本 + call_id）+ 文件回执（`exact`/`observed`/`unknown`）；`todo_write`/`update_plan` 完成必须有当前回执或 verify，否则拒绝，`override` 需人工批准；会话持久化 `readCalls`/`fileReceipts`/`effectSeq`/`effectsVersion`（0=旧会话） | `domain/effects.py`、`core/completion.py`、`domain/session.py`、`tools/todo_write_tool.py` |
| **计划身份** | `update_plan` 按 **id 优先 → 内容精确匹配 → 绝不用位置**；`started_seq` 作为完成回执下界；允许真实回执下调边界 | `tools/update_plan_tool.py`、`tests/test_update_plan_identity.py` |
| **读账本 / 重读** | read 同一 fd 读+stat，检测读期间变化（`read_raced`）；允许必要重读；版本变化不再合并成“完整”；重复调用守卫改为建议式，但 `mutates_world` 仍硬拒 | `tools/read_tool.py`、`domain/read_ledger.py`、`core/guard.py` |
| **bash / jobs** | 包管理器缓存指向 `<ws>/.ai-coding/cache`；job 日志绝对路径 + `job_output` 提示；日志上限按管道截断（SIGPIPE 不误判）；**所有后台/晋级 job 不再持整库写锁**（仅启动时由 dispatcher claim） | `tools/bash_tool.py`、`core/jobs.py`、`docs/agent-runtime-audit-2026-11-05.md` |
| **沙箱网络** | 新 `inherit` 模式（保留宿主网络与代理环境）；按调用升级 `network_permissions="inherit"`，默认 deny/proxy 时走 WARN + justification；`off` 仍按历史行为清理代理环境 | `security/process_sandbox.py`、`config/models.py`、`security/permission_gate.py` |
| **运行时 / TUI** | steer ack 带 live turn id；Esc 清孤儿 busy；`session.get` 增 `running`/`turnId`；运行中拒绝中途发图片、文本走 steer；Ctrl+C 有选区复制/无选区退出；merge/完成门禁状态标签 | `server/handlers.py`、`frontend/src/util/{selection,toolmeta}.ts`、`frontend/src/routes/session.tsx` |

**测试覆盖新增**

- C3：`test_merge_queue.py`、`test_merge_agent_tool.py`、`test_merge_effects.py`、`test_worktree.py`（base/checkout）
- 效果账本：`test_effects.py`、`test_completion_gate.py`、`test_completion_diagnostics.py`、`test_bash_effects.py`、`test_effect_agent_loop.py`、`test_effect_callbacks.py`、`test_todo_completion.py`、`test_read_coverage.py`
- 计划身份：`test_update_plan_identity.py`、`test_update_plan_tool.py`
- 前端：`selection.test.ts`、`toolmeta.test.ts`、`reducer.test.ts`、`turn-ownership.test.ts`

## 4. 剩余（全部）

### 4.1 P2-C4 收口

- `max_concurrent_tools` / `max_concurrent_subagents` → `max_execution_slots` 兼容别名。
- 三读口最终形态：`subagent_list` / `parallel_research` 是否保留为兼容别名。
- `docs/data-contracts.md`、`config.example.yaml` 注释、tmux 全矩阵。

### 4.2 真机矩阵缺口（`docs/tmux-p2-matrix.md`）

- 未单独跑：P0-03（readonly spawn 对照）、P0-05（NoticeStore）、P0-06（conflicts）、
  P0-08（重启恢复）、P0-09（stop/fence）。
- C3 未跑：冲突、ignored 碰撞、重启时 `merging` 的需核对展示。

### 4.3 audit 文档列的后续（`docs/agent-runtime-audit-2026-11-05.md`）

> 另有本轮长任务审计：`docs/agent-runtime-audit-longtask-2026-11-05.md`。
> P0-1/P0-2 与 P1-1..P1-6、P2-1..P2-3 均已修复（含低预算压缩滞回、沙箱启动
> 诊断、`security.auto_approve_tools`、缓存路径提示、stale-read 恢复提示）；
> 仍未验证的是 bwrap 下的网络墙与交互式 TUI 审批面板（本机无 bwrap/tmux）。

1. 用真实超大上下文压压缩，记录 `CompactionFailed`/breaker 行为。
2. `job_kill` 不可用时给冲突一个 UI 出路（子代理栈没有 job 工具）。
3. 包管理器缓存跨工作区迁移；job ledger 日志路径按 rescan 重定位。
4. 可选的“严格写模式”：需要服务写入串行化的用户可开启。

### 4.4 已知边界（明确不做）

| 边界 | 影响 |
|---|---|
| fanout 只能整次取消 | `ResearchFanOut` 不暴露子 task |
| 无跨进程租约/恢复 | 单进程契约；重启只标 `needs_reconciliation` |
| 后台子不自动恢复、不重跑 | 崩溃后必须人工核对 |
| `changed_paths` 不含 bash 写入 | 合并用 `git diff base..branch`；bash 效果路径只作为 `observed` 回执 |
| 不做自动合并 | 必须显式 `merge_agent` |
| 无 WAL/收据链/Raft | 有意不照搬 ACCL |
| 后台 job 启动后不持写锁 | 写入不与其它工具串行化；这是 2026-11-05 的有意权衡 |
| 模型步不占槽 | 有意偏离 `unified-design` §4 |

## 5. 转本地开发：环境与命令

### 5.1 拉取与安装

```bash
git clone <repo> ai-coding && cd ai-coding
git checkout main && git log --oneline -3     # 应看到 f8461c6
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                        # pytest/pytest-asyncio/ruff/mypy
bun --version                                  # 前端需要 >=1.2
```

首次启动 TUI 会复制前端并 `bun install`（需网络，上限 600s）；开发热改用
`AI_CODING_FRONTEND=$PWD/ai_coding/frontend`。

### 5.2 校验命令

```bash
.venv/bin/python -m pytest            # 全量（addopts: -q -rs --strict-markers）
.venv/bin/python -m pytest tests/test_merge_queue.py tests/test_effects.py -q
ruff check ai_coding tests
mypy ai_coding                        # strict，按 py3.10
cd ai_coding/frontend && bun test && bun run typecheck
```

说明：

- 无 `bwrap` 的环境会有约 28 个 skip（沙箱用例），不是失败。
- 涉及真实 git 的测试：无 `git` 时按 `test_worktree.py:19-20` 的惯例 `skip`。
- 系统 `python` 没有 pydantic；用 `.venv/bin/python`。

### 5.3 运行期数据在哪（排查用）

| 位置 | 内容 |
|---|---|
| `<ws>/.ai-coding/tasks/<session>.jsonl` | 任务 journal（queued/claimed/running/settled/stopped/closed/pending/merge_*） |
| `<ws>/.ai-coding/agents/<run_id>.json` | 后台子代理 meta（branch/commit/baseCommit/status/lastStatus） |
| `<ws>/.ai-coding/agents/…` + ledger | 结算包（`SubagentResult` 全文） |
| `<ws>/.ai-coding/cache/` | npm/pnpm/yarn/bun/pip/uv 缓存与 tmp |
| `<ws>/.ai-coding/jobs/` | 后台 job 日志（绝对路径回给模型） |
| `<ws>/worktrees/<name>_<token>/` | 子代理/合并候选 worktree（临时区退出即删） |
| `ai_sub_<name>_<token>` 分支 | 子代理提交所在分支，合并前一直保留 |

排查顺序：journal 最后事件 → meta 的 `lastStatus` → 分支 `git log`；工具被拒时先看
session 文件里的 `tool` 消息（deny 会带 `decision`）与 `write_conflict` 的 blocker。

## 6. 怎么交接（给下一位开发者 / 下一个 AI 会话）

### 6.1 必读顺序（约 30 分钟）

1. `docs/orchestration-p2c-plan.md` —— P2 总方案与分期。
2. `docs/orchestration-p2c-c3-plan.md` §12 —— C3 实现记录与失败矩阵。
3. `docs/agent-runtime-audit-2026-11-05.md` + `docs/tmux-p2-matrix.md` —— 已知问题与真机缺口。
4. 代码：`core/scheduler.py` → `core/task_journal.py` → `core/merge_queue.py`
   → `core/write_coordinator.py` → `domain/effects.py` + `core/completion.py`
   → `tools/builder.py`（组装）。
5. 测试：`tests/test_merge_queue.py`、`tests/test_effects.py`、`tests/test_scheduler.py`。

### 6.2 可以直接复制给下一位 AI 的开场提示

```text
仓库 /workspace/ai-coding，基线 f8461c6（main）。P0-P2-C3 已完成；
C3 之后合入了 EffectLedger/完成门禁、计划身份、读账本与运行时修复。
请先读：docs/orchestration-handoff.md、docs/agent-runtime-audit-2026-11-05.md、
docs/tmux-p2-matrix.md、core/merge_queue.py、domain/effects.py、core/completion.py。

任务：做真机长任务审计，重点复现并定位：
1) 工具调用频繁被拒（write/edit/bash/下载/缓存写入）；
2) 压缩后上下文/回执失效（EffectLedger 与 read 版本）；
3) 前端状态与错误标签问题。
发现即记录到 docs/ 下的审计文档（含复现命令、证据路径、根因、建议），
不要只报现象；修一个记一个，每步跑全量 pytest/ruff/mypy。
约束：不改 P1 wire/P2-A packet 语义/P2-B claim 键/C2 fence-stop-recover；
后台 job 不持写锁是有意设计，除非有证据否则不要回退。
```

## 7. 接下来该干啥（按顺序）

| 步骤 | 动作 | 完成标准 |
|---|---|---|
| 1 | 真机长任务审计（工具被拒/缓存/下载/压缩/前端） | 审计文档：每条问题有复现、证据、根因、建议 |
| 2 | 修复审计中的 P0（拒绝类、上下文失效类） | 对应用例绿；全量不回归 |
| 3 | 补跑 tmux 矩阵缺口（P0-03/05/06/08/09 + C3 冲突/ignored/重启） | 矩阵文档更新为 PASS/FAIL + 证据 |
| 4 | P2-C4 配置别名与三读口收口 | 全量 pytest/ruff/mypy；前端绿 |
| 5 | 提交 | `fix(orchestration): …` / `feat(orchestration): …` |

## 8. 不要做的事

- 不要自动合并、不要自动重跑已写过的子代理、不要恢复后台运行中的子代理。
- 不要为代码文件引入 ACCL 的 JSON `merge3`；不要解析本地化 git 文本。
- 不要新增第三张持久任务表；`TaskRegistry` 是只读视图。
- 不要改 P1 wire（`normal/plan` + `readonly`）、P2-A packet 字段语义、P2-B
  claim 键、C2 的 fence/stop/recover 语义。
- 不要在 `merge_agent` 上打开 dispatcher 的写 claim（会与队列互锁）。
- 不要删除子代理源分支（保留重试与审计）。
- 不要把后台 job 的“不持写锁”当成 bug 回退；要串行化请走严格模式设计。

## 9. 相关文档索引

- 总方案：`docs/orchestration-p2c-plan.md`
- 已实现分期：`docs/orchestration-p2a-plan.md`、`-p2b-plan.md`、`-p2c-c1-plan.md`、
  `-p2c-c2-plan.md`、`-p2c-c3-plan.md`
- 审计与真机：`docs/agent-runtime-audit-2026-11-05.md`、`docs/tmux-p2-matrix.md`
- 更早背景：`docs/orchestration-unified-design.md`、`docs/orchestration-unified-research.md`、
  `docs/orchestration-p0b-plan.md`、`docs/orchestration-p1-plan.md`
- 运行手册：`docs/how-to-run.md`
