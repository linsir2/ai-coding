# P2-B 实施方案（评审修订稿）：写协调、容量统一与持久通知

> 前置：P2-A 已完成（`ef81377`）。行号以 HEAD `ef81377` 为准。
> 依据：`docs/orchestration-p2-research.md` + 三轮子智能体调研（本地写路径 / ACCL /
> 业界共享写与通知）。本稿已吸收一轮独立方案评审的 P0/P1（容量泄漏、通知重复与
> 丢失、claim 生命周期、bash 后台写、键设计、消费缺口、测试影响面）。

## 0. 目标与非目标

**做**
1. `WriteCoordinator`：会写工作区的工具在执行前拿 claim，执行后释放；同路径互斥、
   未知范围按工作区独占；有界等待后 fail-closed（`write_conflict`）。
2. 子代理完成时把 `changed_paths` 与近期子代理比对，重叠写进
   `SubagentResult.conflicts`，进入 render/notice/details/ledger/bridge。
3. 容量统一：`max_concurrent_subagents` 同时约束同步、后台、follow-up；池满
   fail-loud；槽位释放幂等，覆盖中断/关闭/启动失败/reap。
4. 持久通知：结算写 `notices/<session_id>.jsonl`；loop 合并内存+磁盘、按文本去重、
   只清已投递项；结算 `wake=True`（server/IPC 可唤醒 idle；裸 CLI 等下一次输入）。
5. 深度：子代理工具栈 `include_subagent=False`（`tools/builder.py:114`），结构性
   恒为 1；B 只补断言与文档，不新增配置。

**不做**
- 不做跨调用租约/单调 fence：前台 claim 在 `finally` 释放，取消也释放；后台 bash
  是唯一的“长持有”，用 job 结算回调释放，单进程内不需要 fencing。
- 不做 merge queue / 3-way merge / 自动合并。
- 不做 WAL/收据/崩溃恢复、Raft、TLA+、跨进程 flock。
- 不改 plan/gate/worktree 语义；P2-A packet 只加 `conflicts`。

## 1. 现状事实

| # | 事实 | 证据 |
|---|---|---|
| W1 | `write`/`edit` 有 `path`；`bash` 只有 `command`（cwd=ctx.workspace）；`mcp_call` input 不透明 | `tools/write_tool.py:68`、`edit_tool.py:50`、`bash_tool.py:180-214`、`mcp_call.py:26-33` |
| W2 | write/edit/bash/mcp 默认 `parallel_safe=False`（barrier） | `tools/base.py`、`core/dispatch.py:388-411` |
| W3 | `write_ahead` 只是崩溃重放，不是锁 | `core/write_ahead.py:77-145` |
| W4 | 父/子 `FileStateTracker` 各一套 | `tools/builder.py:272,301-306` |
| W5 | `changed_paths` 只来自 write/edit artifacts；bash 不在内 | `core/subagent.py:1953-1961` |
| W6 | 容量两套口径；sync 不占 running 行；resume 不计数 | `core/subagent.py:250,354,552-560,707-715,849-934`、`models.py:374` |
| W7 | notice 只有内存队列，结算无 wake；写盘不存在 | `core/steering.py:44-63`、`core/subagent.py:1317-1334`、`core/jobs.py:480` |
| W8 | `session.inbox` 是操作者输入，已持久化 | `domain/session.py:260,683-684,759-765` |
| W9 | bash 后台命令立即返回，超时 promotion 后仍在跑 | `tools/bash_tool.py:406-407,269-352` |
| W10 | `JobRegistry._settle` 幂等，可挂结算回调 | `core/jobs.py:399-448` |

## 2. 设计

### 2.1 写分区与 claim

| 工具 | `write_paths_for(params, ctx)` | claim |
|---|---|---|
| `write` / `edit` | `(规范路径,)`，用 `self._sandbox.resolve`（吞 `SandboxViolation`→`None`） | 路径级 |
| `bash`（前台/后台/超时 promotion） | `()` = 未知 | dispatcher 前台 claim `*`；后台/晋升 job 不拿 lifetime hold |
| `mcp_call` | `()` | 工作区级 `*` |
| `subAgent` / `spawn_agent` / `send_message` / `todo_write` / plan / memory / job_kill | `None` | 不 claim（显式返回 None） |

- **bash 不再用“只读命令”豁免**：`read_only_problem` 允许 `git status/log`，但 git
  alias/fsmonitor/index 刷新仍可能写；一律 `*`，保守正确。
- 键：`root = resolve(ctx.workspace)`；路径 claim = `(root, rel)`，其中 `rel` 是
  `os.path.relpath(abs_path, root)`（越界则用绝对路径）。`*` claim = `(root, "*")`。
- 冲突：同 root 的 `*` ↔ 任意路径；同 root 同 rel 的两路径；不同 root 不冲突
  （隔离子代理的 worktree 与主区天然并行）。
- 获取点：`core/dispatch.py::_decide_inner` 中 **write-ahead replay 返回之后、
  `_execute` 之前**；params 已是 guard/hook rewrite 后的最终值。
- 等待：`min(30s, 剩余 turn deadline)`；超时返回 `write_conflict`，data
  `{paths, owner, wait_ms}`；不执行。`WriteAheadLog` 新增 `abandon(call)`，冲突时
  删除已落下的 `running` intent（`core/write_ahead.py:113-118`），不留残影。
- 释放：`try/finally`；异常/取消都释放。
- **2026-11-05 修订：后台 job 不持有 workspace 锁。** 原方案让
  `_start_background`/`_run_as_job` 在 `registry.start` 前 `acquire_hold(owner, root)`，
  由 `on_settle` 释放。实测一个长期 dev server（`npx vite`）会让整个 workspace
  永久不可写，跨 session 连 `job_kill` 前的诊断 bash 都被 `write_conflict` 挡住。
  现在后台/晋升 job 只受 dispatcher 启动 claim 保护，不再持 lifetime hold；
  需要写串行时把 install/build 放前台。`job_kill` 本身不取 claim。

- `acquire_hold` 是同步的（可能从 job watcher 线程释放）；异步 `claim` 用
  `threading.Lock` 保护的同一份占用表 + 50ms 轮询等待，跨线程安全。
- 不做 lease/fence：前台 claim 不跨调用；后台 job 不持 lifetime hold。

### 2.2 完成时重叠（子代理之间）

- `WriteCoordinator.register_run(key, paths, origin_session)`：key = `run_id`（同步
  子用其唯一 session_id），另存 `origin_session`；最近 20 个 run、10 分钟窗口。
- `_run_turn` 结果生成后：register + `overlap(key, paths)`（按 key 排除自己，
  不按 origin_session 排除）→ `result.conflicts`。
- **如实语义：只有后完成者能看到重叠**（先完成者的 notice 已发出；B 不做回溯通知）。
  文案写“与 X 改动重叠（未合并）”，不承诺“双方都收到”。
- 消费：`_render`、notice、`details`、ledger、bridge、前端行。
- 父自身写与子的冲突由 §2.1 claim 串行；重叠检测只覆盖两个隔离子/共享子改同一
  相对路径的合并风险。

### 2.3 容量统一（`_active` 集合 + 幂等释放）

- `self._active: set[str]` 是唯一准入集合；`max_concurrent_subagents` 是上限。
- `async def _admit(key)`：`while len(_active) >= max: await self._slot_freed.wait()`;
  然后加入。同步 `run()` 用 `await _admit(sync_key)`（排队，不拒绝）；
  `try_admit(key)` 供同步上下文（`spawn_background`/`_schedule_turn`）使用，
  满则 `SubAgentPoolFull`。
- `_release_slot(key)`：`set.discard` + `set()` 事件；**幂等**，可在多个终态路径调用。
- 调用点（每条路径都钉测试）：
  - `run()`：admit → `finally release`。
  - `spawn_background`：try_admit → 两处启动失败回滚 → 成功结算
    `_finish_background` 最顶部 release（不依赖 row 状态）。
  - `_supervised`：不重复 admit（spawn 已占）。
  - `_schedule_turn`：try_admit；`create_task` 失败回滚；结算/中断 release。
  - `_reap`：**stale-task 身份检查之后、row 状态检查之前** release（否则旧回调
    会误释新 follow-up 的槽，`core/subagent.py:1349-1351`）。
  - `interrupt`/`close`：若 key 在集合中则 release（幂等）。
  - `resume`：不占槽（不执行）。
- 信号量仍保留为**执行**硬上限；准入集合保证“已接纳总数 ≤ max”。
- 深度：结构性 1；加一条 `build_tools` 断言测试，无新配置。

### 2.4 持久通知与唤醒

- 新模块 `core/notice_store.py`：
  - `clip_notice(text)` 与 `SteeringQueue` 完全一致（2000 字符、超出加 `…`）；
    写盘与入队都用它，保证文本可逐字比对。
  - `<workspace>/.ai-coding/notices/<session_id>.jsonl`，每行
    `{"id": uuid, "session":..., "text": <clipped>, "created_at":...}`；`O_APPEND`。
  - `pending(session_id) -> list[dict]`；`deliver(session_id, ids)` 只删这些 id
    （tmp+replace），未投递行保留。
- `SubAgentRunner._announce_to_model(..., origin_session)`：`origin_session` 非空才写盘；
  然后 `notices.put(line, wake=True)`。写盘失败只记日志。
- `AgentLoop._take_notices`（新增可选 `notice_store`，`agent_loop.py:1550-1571`）：
  1. `memory = self.notices.drain()`（≤5 条、按队列规则裁剪）；
  2. `disk = store.pending(session_id)`；
  3. 顺序合并 `memory + disk.text`，按**文本**去重，取前 5 条写消息；
  4. `closer.checkpoint(session, index)` 之后 `store.deliver(session_id, ids)`：
     对每条已投递文本，删除磁盘中**同文本的全部 id**（内存行与磁盘行同文本即同一条）。
  - 只清已投递项：未投递的磁盘行留下；内存已消费但未投递不会被挤出（memory 在前）。
  - 语义 **at-least-once**：投递后、deliver 前崩溃会重复一条，绝不丢。
  - `notice_store=None` 时保持旧行为（只 drain 内存）。
- `_build_loop` 注入 `NoticeStore.for_workspace(ws)`；子代理 loop 传 `None`。
- 唤醒：`put(..., wake=True)` 走既有 `SteeringQueue.on_put`（server 在
  `handlers.py:1895-1903` 安装）；裸 CLI 没有 on_put，等下一次人类输入。
- 两个 loop 同 session 的 `deliver` 竞态不在 B 范围；文件操作在事件循环线程内同步执行，
  单 loop 原子。

### 2.5 冲突通知

- `SubagentResult.conflicts: tuple[dict[str,Any], ...] = ()`（v3 加法）。
- `_render`/`_notice_text`：`⚠ 与 <sid[:12]> 改动重叠: p1,p2（未合并）`。
- `_details_projection` 加 `"conflicts"`；bridge 白名单加 `conflicts`。
- 前端：`background.done` 行提示“改动重叠”；`toolmeta.ts` 加 `write_conflict`
  标签“等待其它写者超时”。
- 工具 data：`changed_paths/commit/commit_error/degraded` 已存在，再加 `conflicts`
  （小字段，不塞整包）。

## 3. 逐文件改动

| 文件 | 改动 |
|---|---|
| `tools/base.py` | `write_paths_for(params, ctx) -> tuple[str,...] \| None`，默认 `None` |
| `tools/write_tool.py`/`edit_tool.py` | 覆写：`self._sandbox.resolve` → `(str(target),)`；`SandboxViolation`→`None` |
| `tools/bash_tool.py` | 覆写：一律 `()`；后台/晋升 job **不** `acquire_hold`（2026-11-05 修订） |
| `tools/mcp_call.py` | 覆写：`()` |
| `core/write_coordinator.py`（新） | 异步 `claim`（路径/`*`、等待/超时/取消）、同步 `acquire_hold`、`release_hold`、`register_run/overlap` |
| `core/dispatch.py` | 构造收 `write_coordinator`；replay 后 claim、finally release；`_write_conflict` |
| `core/jobs.py` | `start(..., on_settle=None)`；`_settle` 幂等调用回调（`jobs.py:399-448`） |
| `core/write_ahead.py` | `abandon(call)`：冲突时删除 running intent |
| `core/agent_loop.py` | 传 coordinator 给 dispatcher；`notice_store` 合并/去重/只清已投递 |
| `core/subagent.py` | `_run_turn` 注册/重叠→`conflicts`；`_active` admit/release；notice 写盘+wake（`_reap` 也传 origin_session）；details 加 conflicts |
| `tools/builder.py` | 创建 `WriteCoordinator`，挂 `registry.write_coordinator` 并传给 runner；子 registry 由 runner 挂同一实例 |
| `cli.py` | `_build_loop` 注入 `NoticeStore` |
| `domain/subagent.py` | `SubagentResult.conflicts`；`to_dict` 加法 |
| `tools/subagent_tool.py` | `_render` 冲突告警；data 加 conflicts |
| `server/bridge.py` + 前端 | details → `conflicts`；通知行/`toolmeta.ts` |
| 文档 | `docs/data-contracts.md`、`docs/orchestration-unified-design.md` B 状态 |

**不改**：gate/plan/worktree/`session.inbox`/`jobs` 事件形状/P2-A 字段语义。

## 4. 数据流

```
Dispatcher._decide_inner
  ... approval / guard / hook / precheck / write-ahead replay ...
  paths = tool.write_paths_for(call.params, ctx)
  if paths is not None:
      claim = await coordinator.claim(owner=ctx.session_id, root=ctx.workspace,
                                      paths=paths, timeout=min(30, deadline))
      超时 → write_conflict（工具未执行、无 write-ahead）
  try: _execute
  finally: claim.release()
bash(run_in_background)
  dispatcher launch claim only；job.start() 不取 lifetime hold
子代理结算
  _run_turn → changed_paths
  coordinator.register_run(session_id, changed_paths)
  conflicts = coordinator.overlap(session_id, changed_paths)
  result.conflicts → ledger/tool/notice/details
  notice → store.append(origin_session) + queue.put(wake=True)
AgentLoop._take_notices
  memory + disk → dedupe → first 5 → messages → checkpoint → store.deliver(delivered)
```

## 5. TDD 测试用例（先写失败，再实现）

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/test_write_coordinator.py`（新） | 同路径串行 | 第二个在第一个释放后拿到 |
| | 不同路径并行 | 同时持有 |
| | `*` 与同 root 路径互斥 | 路径等待 |
| | 不同 root 不互斥 | worktree/主区并行 |
| | 超时/取消释放 | `write_conflict`；等待者取消后锁不泄漏 |
| | `acquire_hold` 与异步 claim 互斥 | hold 期间 claim 等待；release 后通过 |
| | overlap 只报别人/10 分钟外不报 | — |
| `tests/test_tool_dispatch.py`（既有） | write 路径 claim | fake coordinator 收到 resolve 后的路径 |
| | bash 前台 claim `*`；后台不持 lifetime hold | 后台运行期间另一个 writer 可 claim |
| | mcp `*` | — |
| | 超时 → `write_conflict` | 错误码/data；工具未执行；`write_ahead` intent 已 abandon |
| | 异常/取消释放 | finally 生效 |
| `tests/test_write_ahead.py`（既有） | `abandon` 删除 running 记录 | settle 不再写回；重放不命中 |
| `tests/test_jobs.py` | `on_settle` 只回调一次 | kill/正常结算各不重复 |
| | 超时 promotion 不持 hold | 后台运行期间另一个 writer 可 claim |
| `tests/test_subagent_runner.py` | sync 占槽、spawn 被拒 | `_active`；`SubAgentPoolFull` |
| | 启动失败回滚 | 两处失败路径后 `_active` 归零 |
| | follow-up 占槽/中断释放 | +1/-1；不泄漏 |
| | 完成 overlap 进 packet | `conflicts` 非空 + ledger 有 |
| | 深度结构性 1 | 子 registry 无 spawn/subAgent |
| `tests/test_background_tasks.py` | notice/details 含冲突 | 文本/投影 |
| | 中断/关闭/reap 各只减一次 | `_active` 不泄漏（三条用例） |
| `tests/test_notice_store.py`（新） | 追加→pending→deliver | 部分投递只清已投递；裁剪 2000 |
| `tests/test_loop_notices.py`（扩展现有 8 例） | loop 合并/去重/只清已投递 | 内存+磁盘同文本一条消息；剩余磁盘行保留 |
| | `notice_store=None` 旧行为 | 只 drain 内存 |
| | 投递后 checkpoint 再 deliver | 崩溃窗口重复而非丢失（模拟） |
| `tests/test_agent_messages.py` | 结算 `wake=True` | fake queue 记录 |
| `tests/test_tool_subagent.py` | data/render 含 conflicts | 小字段 |
| `tests/test_server_bridge.py` + 前端 | details conflicts | bridge/行文本 |
| `frontend/test/reducer.test.ts` | 重叠提示 | 行含“改动重叠” |

**需同步修改的既有断言**
- `tests/test_background_tasks.py:72-83`（用真实 spawn/`_active` 触发池满）、`:87-105`
  （启动失败回滚）。
- `tests/test_agent_edges.py:101-117`、`tests/test_subagent_runner.py:437-442`、
  `tests/test_agent_messages.py:200-227`（容量/中断断言）。
- `tests/test_mode_gate.py`（claim 不得改变 readonly 拒绝码）。
- P2-A 的 `test_agent_packet.py` / `test_subagent_handoff_v2.py` 不得改弱。

## 6. 风险与边界

1. bash 一律 `*` 会降低并发；只读 shell 用 `shell_read`（它不 claim）。
2. 前台 claim 30s 超时 fail-closed；长命令会占 `*`，文案明确。
3. 后台 bash 不持 `*`；`job_kill` 也不取 claim。
4. 完成重叠只有后完成者能看到；不回溯通知（如实文案）。
5. 通知 at-least-once；`deliver` 在 checkpoint 之后，崩溃只会重复不会丢。
6. `_active` 的释放必须幂等，且覆盖 sync/spawn/follow-up/interrupt/close/reap/
   启动失败；测试逐条钉住。
7. 路径键按 root 隔离：父区 `*` 不会误伤 worktree 子；共享（非 git）子与父同 root
   会被串行化。

## 7. 验收

- 全量 `pytest` 0 failed；`ruff`/`mypy`；前端 `bun test` + `tsc`。
- 并发写：同路径两子 → 一个 `write_conflict`；不同路径并行；隔离/主区不互斥。
- 后台 bash 在跑时，另一个写者被 `*` 挡住；job 结束后放行。
- 容量：sync+background+follow-up ≤ `max_concurrent_subagents`；中断/关闭/reap
  各只减一次。
- 通知：新 loop 能读到未投递 notice；同一条不重复；未投递的磁盘行保留。
- 重叠：两个隔离子改同一相对路径 → 后完成者 packet/notice/details 有 conflicts。
- **tmux 真机**（qwen3.7-flash）：两个后台可写子并行改同一文件 → 一成一败且
  `write_conflict` 可见；改不同文件 → 两成且 notice 带 branch/commit；安装
  `on_put` 的 loop 在 idle 收到 wake（裸 CLI 无 on_put 的差异在报告中注明）。

## 8. 实施顺序

1. **B1 写协调**：`WriteCoordinator` + `write_paths_for` + dispatcher claim +
   前台 dispatcher claim + `write_conflict`；后台 job 不持 lifetime hold。
2. **B2 重叠与容量**：`conflicts`、`_active` admit/release、所有终态路径。
3. **B3 持久通知**：`NoticeStore`、loop 合并/去重/只清已投递、`wake=True`。
4. **B4 复核**：独立子智能体评审 → 修 → 全量校验 → tmux 真机。

## 9. 实施记录（完成）

- **B1 写协调**：`core/write_coordinator.py`（`(root, rel)` 占用表、`*`/路径冲突、
  同 owner 重入、异步 claim 20ms/50ms 轮询、同步 hold）、`write_paths_for`、
  dispatcher claim（replay 后、execute 前，超时 `write_conflict` + `write_ahead.abandon`）、
  前台 claim；后台/晋升 job 不持 lifetime hold（2026-11-05 修订）。
- **B2 重叠与容量**：`SubagentResult.conflicts`（ledger/data/render/notice/details/
  bridge/前端）；`_run_turn` 注册 + 比对；`_active` 集合准入与幂等释放，覆盖
  run/spawn 两处失败/`_schedule_turn` 失败/中断/close/reap。
- **B3 持久通知**：`core/notice_store.py`（`clip_notice`、JSONL append/pending/deliver）；
  `_announce_to_model` 写盘 + `wake=True`；`_take_notices` 内存+磁盘去重、取前 5、
  checkpoint 后只清已投递；`NoticeStore` 由 `_build_loop` 注入，子 loop 为 None。
- **独立评审**：无 P0；P1（`shell_read` 继承 bash 的 `*` claim）已修（dispatcher 只对
  `mutates_world_for` 的调用 claim + `ShellReadTool` 显式 None）；P2（abandon 只删
  running、overlap 并集、doc 去 meta、clip strip、stale-reap/取消/回滚用例）已修。
- **全量校验**：`pytest` exit 0（28 skipped 为 bwrap 不可用）；`ruff` 通过；`mypy`
  175 文件；前端 `bun test` 546 pass、`tsc` 干净。
- **tmux 真机（qwen3.7-flash）**：
  | 场景 | 结果 |
  |---|---|
  | 非 git 共享区两子并发（各 `bash sleep 35` 后写同一 `a.txt`） | 一个子会话记录 `write_conflict`（1 次），后被拒后自行恢复；后完成者 ledger `conflicts` 指向先完成者的 `a.txt` |
  | git 仓库两子写不同文件 | 两子均 completed，分支 `ai_sub_create_a_*` / `ai_sub_create_b_*` 与各自 commit，`conflicts=[]` |
  | idle 子代理完成 | notice 落盘（313B），`wake=True` 回调 1 次；下一回合读入 1 条 `[notice]`，磁盘行清空 |
- **已知边界**：bash 一律 `*`（只读命令也占），长命令会串行化其它写者；重叠只有
  后完成者能看到（先完成者不回溯通知）；同 session 的后台 bash 与其后续写可重入并发。
