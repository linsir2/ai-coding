# C-5b 设计（v2，已吸收评审）：send_message / interrupt_agent / resume_agent

> 目标：子智能体可寻址、可**turn 级打断**、可**复原续跑**；复用 `SubAgentRunner`、
> `AgentLoop`、`SessionService`、权限与沙箱。基准是 Codex 的 multi-agent 语义，
> 差异集中在第 9 节。
>
> v1 评审发现 3 个 P0（每轮 `aclose` 与续跑冲突、mailbox 挂错队列、
> interrupted 仍会发完成通知）和若干 P1（持久化根目录、句柄/行表双源、并发竞态）。
> 本文是修正后的权威版本。

## 1. 结论与范围

- 新增 3 个工具：`send_message` / `interrupt_agent` / `resume_agent`，与 C-5a 的
  `spawn_agent` / `wait_agent` / `list_agents` / `close_agent` 组成 7 件套。
- 打断是 **turn 级**：取消当前 turn，保留 handle、子会话与工作区；历史落盘。
- 复原分两层：进程内（handle 还在）；跨进程（`SessionService` + sidecar）。
- 不改压缩、父循环、权限门语义；只扩展 `SubAgentRunner`、`AgentLoop` 的一个入站队列
  与工具层。

## 2. Codex 的语义（事实）

| 环节 | Codex | 位置 |
|---|---|---|
| `send_message` | `{target, message}`；target 是相对/canonical task name 或 thread id；`QueueOnly`（`trigger_turn=false`）投递；活跃 turn 在下一采样边界消费，空闲则等到下一次 turn；成功返回空文本 | `multi_agents_v2/send_message.rs:41-47`、`message_tool.rs:100`、`input_queue.rs:346-366` |
| 空闲唤醒 | `send_message` **不**唤醒；`followup_task`（`TriggerTurn`）才起新 turn | `followup_task.rs:41-47`、`api.rs:154-159` |
| `interrupt_agent` | `{target}`；`Op::Interrupt` 中止当前 turn，线程与会话保留；返回 `{previous_status}`；未加载/已死也成功 | `interrupt_agent.rs:44-76`、`session/mod.rs:5669-5676`、`control/interrupt.rs:35-60` |
| `resume_agent` | `{id}`；依赖 thread 元数据 + rollout，`InitialHistory::Resumed`；不恢复 mailbox/pending；恢复后 idle，需 send/followup 才起 turn；**不续跑**被中断 turn；已加载时 no-op | `resume_agent.rs:43-47`、`spawn.rs:1003-1033,1074-1088`、`control/resume.rs:17-35` |
| `close_agent` | 关闭整棵子树、flush rollout、移除 registry；rollout 保留，可 resume | `multi_agents_spec.rs:336-359`、`legacy.rs:9-48` |
| 完成回灌 | 仅 Completed/Errored/Shutdown/NotFound 发完成消息（`trigger_turn=false`）；**Interrupted 不发** | `completion.rs:112-128`、`session_prefix.rs:25-35` |
| 持久化 | 子线程写 rollout（parent_thread_id/agent_path/role/source）；mailbox、pending、registry 不持久化 | `recorder.rs:965-999`、`mailbox.rs:15-21` |
| 并发 | 4 线程/会话（含 root） | `config/mod.rs:261,1716-1729` |

## 3. 我们的现状与缺口

| 缺口 | 证据 | 影响 |
|---|---|---|
| 每轮建会话、每轮 aclose | `core/subagent.py:733-744,799-803` | 无法续跑；engine 被释放 |
| 保存是空实现 | `core/subagent.py:90-108`（`_NullSessions`） | 子会话不落盘 |
| 子 loop 没有入站队列 | `AgentLoop(...)` 构造未传 notices（`:745-770`）；`_take_notices` 在无队列时直接返回（`agent_loop.py:1761-1763`） | 无法向运行中的子会话投递 |
| `_notices` 是父→runner 的出站队列 | `agent_loop.py:1731-1752` | 共享会抢走父通知，且标签固定 `[notice]` |
| 取消是 run 级 | `cancel()` 只 `task.cancel()`（`subagent.py:403-407`）；`_supervised` 捕获后按 cancelled 结算（`:510-514`） | 打断即终止，无法保留会话 |
| `run_with_deadline` 取消后立即 re-raise | `infra/deadline.py:182-193` | 内层 `_close_turn` 可能还没落盘就重建 loop，产生读写竞态 |
| 行表是寻址唯一来源 | `resolve_run_id` 查 `_background`（`:448-462`）；`_prune_background` 删行（`:471-478`） | 句柄还在但行被删 → 寻址丢失 |
| 通知对所有 status 都发 | `_finish_background:551-566`；`_reap:594-631` | interrupted 也会发；且可能二次通知 |
| 后台 spawn 硬传 readonly=False | `_supervised` → `_run_isolated(..., False)`（`:505-509`） | resume 无法保持只读 |
| 取消收尾可用 | `AgentLoop._run_turn` 捕获 `CancelledError` → `_close_turn(aborted)` → re-raise（`agent_loop.py:1560-1571`） | turn 级取消可复用，但必须等它收完 |

## 4. 设计目标与不变量

1. `_handles` 是**唯一真源**；`_background` 只是 UI/观察投影。
   `resolve_run_id`、`wait`、`cancel`、`task_list`、`/tasks stop`（`app/commands.py:1244-1258`）
   都先查 `_handles`；`_prune_background` 不得删除仍存在于 `_handles` 的行；
   resume 重建 handle 时把对应行也重建（status=idle）。
2. 打断只取消当前 turn；turn 收尾（`_close_turn`）完成后才把状态置 `interrupted`。
3. 续跑使用同一个 `SessionData`；压缩、checkpoint、task list 全部照常。
4. 只有 `close_agent` 丢弃 handle；磁盘状态保留，`resume_agent` 可重开。
5. mailbox 不落盘；恢复后只恢复会话，不恢复未消费消息。
6. 每个 handle 同时最多一个 turn；并发 send 一律进 mailbox，绝不覆盖 turn_task。
7. 新工具 `permission_name="subAgent"`；plan/readonly 由既有 surface 规则隐藏。

## 5. 运行时结构

### 5.1 `AgentHandle`（`core/subagent.py` 内部）

```python
@dataclass
class AgentHandle:
    run_id: str
    name: str
    task: str
    session: SessionData
    session_store: SessionService      # 独立 root，见第 7 节
    loop: AgentLoop | None             # None = 需重建（interrupt 后/跨进程）
    workdir: str | None
    readonly: bool
    agent: str | None
    context_mode: str
    parent_permissions: tuple[Rule, ...]
    parent_span: str
    emit: Any
    origin_session: str
    output_schema: dict[str, Any] | None
    status: str                        # running | idle | interrupted | closed
    last_status: str                   # completed | error | timeout | interrupted
    mailbox: deque[str]
    turn_task: asyncio.Task[Any] | None
    turn_lock: asyncio.Lock            # handle 级：一个 turn
    announced: bool                    # 每个 turn 只 announce 一次
```

- 重建 loop 所需的全部输入都从 handle + runner 取得（`engine_factory`、`build_tools(workdir)`、
  `gate`、`approval`、`guards`、`skills`、`project`、`settings`、`agents`、
  `session_permissions`）；v1 遗漏的 `parent_span/emit/origin_session/output_schema`
  已入 handle。
- `session_store` 是**注入**的独立 `SessionService` root（不是 `_NullSessions`）。

### 5.2 生命周期

```
spawn → _open_handle（会话 + loop + sidecar）；status=running
   ├─ turn 完成 → idle（last_status=completed/error/timeout），announced 后发 notice
   ├─ interrupt → interrupted（等 _close_turn 落盘），**不发** notice；loop=None
   └─ close → closed（取消在跑的 turn、aclose loop、删 handle、保留磁盘）

send_message(target, text)
   ├─ status=="running" 或已有 turn_task → mailbox.append（不新建 turn）
   ├─ status 为 idle/interrupted → _schedule_turn(handle, text)
   └─ closed/unknown → error

每个 turn 结束（正常或中断）时复检 mailbox：
   ├─ 还有消息且 handle 未 closed → 合并成一次 follow-up turn（一次投递全部）
   └─ handle 已 closed → 丢弃（与 close 语义一致）
```

### 5.3 同步路径（`run()` / 研究 fan-out）不建可续跑句柄

`SubAgentRunner.run()` 仍走一次性的同步路径：`_run_isolated` 内部创建一个
**ephemeral handle**（`ephemeral=True`），跑一个 turn 后 `loop.aclose()` 并丢弃
handle/session 文件。只有 `spawn_background` 创建**可续跑** handle。
这样拆 `_open_handle/_run_turn` 不会让同步路径泄漏 engine，也不改变现有
`subAgent`/`parallel_research` 的行为。

## 6. 数据流

### DF-1 `send_message`（运行中 / 已有 turn）

```
SendMessageTool.execute
  → runner.send_message(target, text)
     → target 解析：_handles 里完整 id → 唯一前缀 → 唯一 name（closed 不算）
     → handle.turn_task 活跃或 status=="running" → handle.mailbox.append(text)
     → 否则 _schedule_turn(handle, text)
  → 返回 {"target": id, "status": "queued"|"running"}
```

子会话投递点（新，不复用 `_notices`）：

- `AgentLoop` 新增构造参数 `agent_inbox: Callable[[], list[str]] | None = None`。
- `_run_turn` 的 iteration 边界（与 `_take_notices` 并列）调用 `_take_agent_inbox`：
  把每条消息写成 `user_message("[agent-message] {text}", synthetic=True)` 并
  `closer.checkpoint(session, index)`。
- 数量/长度上限由 `SendMessageTool` 在入队时校验（非空、≤2000 字），循环内不静默丢。

### DF-2 `send_message`（空闲/被中断）→ `_schedule_turn`

```
runner._schedule_turn(handle, text)
  → handle.turn_lock 已被占用：return "queued"（消息进 mailbox，不覆盖）
  → 先按 running 行数检查池（满 → SubAgentPoolFull，工具报 agents_exhausted）
  → 行 status=running（重置 ended/摘要），handle.status=running，announced=False
  → create_task(_run_turn(handle, text))；handle.turn_task=task
_run_turn:
  → loop 为 None 时按 session 重建
  → async with semaphore: task = create_task(loop.process_input(session, text))
  → await asyncio.wait({task}, timeout=timeout)
       • 正常完成：session_store.save；写 sidecar；status=idle；announce（按 last_status）
       • 超时/打断：task.cancel() 后 **await gather(task, return_exceptions=True)**
         等 `_close_turn` 完成；session_store.save；status=interrupted；不发 notice
```

- **禁止 `run_with_deadline` 直接包 `process_input`**：它取消内层后立刻 re-raise，
  `_close_turn` 可能未完成（`infra/deadline.py:182-193`）。改为"取消 → 等待收尾 → 再切状态"。
- 续跑 turn 同样占 semaphore；池检查在调度前用 running 行数完成，避免父 turn 阻塞在取槽上。
- 竞态规则：`_schedule_turn` 中"检查 `handle.turn_task` 是否活跃"与"赋值为新 task"
  之间**不得有 await**；`turn_lock` 只用于跨协程互斥，不作为"是否已有 turn"的判据。
  `_schedule_turn` 创建的 task 同时注册 `_reap`（与 spawn 同一 done-callback）。
- turn 结束时 mailbox 非空 → 先保存 session，再调度一次合并的 follow-up turn
  （避免消息卡在最后一个 iteration 边界之后）。

### DF-3 `interrupt_agent`

```
InterruptAgentTool.execute
  → runner.interrupt(target)
     → 未知/已死：成功返回 {"previous_status": "not_found"}（Codex 同）
     → 没有 turn_task：成功返回 {"previous_status": handle.status}（no-op）
     → 有 turn_task：previous=handle.status
          task.cancel()
          _run_turn 的取消链：
            except CancelledError:
                inner.cancel()                       # 取消 loop.process_input
                await gather(inner, return_exceptions=True)   # 等 _close_turn 落盘
                session_store.save(handle.session)
                handle.loop=None；旧 loop 在安全点 aclose
                handle.status=interrupted；行 status=interrupted；不 announce
          （不能只取消 turn_task 就返回：内层 process_input 需要被一起取消并等待收尾）
  → 返回 {"previous_status": previous, "status": "interrupted"}
```

### DF-4 `resume_agent`

```
ResumeAgentTool.execute
  → runner.resume(target)
     ├─ handle 存在且 status=="running" → no-op，返回 {"status":"running"}（Codex 已加载 no-op）
     ├─ handle 存在（idle/interrupted/completed）→ 确保 loop 可用（必要时重建），status=idle
     └─ handle 不存在：
          → 读 .ai-coding/agents/<run_id>.json
          → session_store.load(sidecar["sessionId"])；清 pending_approval/pending_posture/
            write_ahead/open_turn
          → 重建 handle（permissions 用 rules_from_dicts，readonly/workdir 来自 sidecar）
          → status=idle
     文件缺失/损坏 → error="not_found"
```

- resume **不**自动起 turn（Codex 语义）；`send_message` 后才跑。
- 不续跑"被中断的那个 turn"：`_close_turn` 已把 `open_turn` 清掉；新 turn 从历史继续。

### DF-5 `close_agent`

```
CloseAgentTool.execute
  → runner.close(target)
     → 有 turn：cancel → await gather → 保存
     → loop.aclose()（释放 engine）
     → handle 从 _handles 删除；行 status=cancelled；sidecar/session 保留
     → 不发 notice
  → 返回 {"closed": true}
```

`resume_agent` 可在 close 后从磁盘重开。

### DF-6 完成回灌

| 结局 | 行 status | notice |
|---|---|---|
| completed / error / timeout | 对应值 | 发一次（`announced` 门） |
| interrupted | interrupted | **不发** |
| closed | cancelled | **不发** |

- `interrupt` 路径**直接构造** `SubagentResult(status="interrupted")`，不走 `_packet`
  的 `aborted→error` 映射；`_STATUS_MAP` 不需要新增键（`TurnStatus` 里没有
  "interrupted"，只有 `ABORTED`）。
- `_finish_background` 只有在 `status in {completed,error,timeout}` 且 `not announced` 时回灌。
- `_reap` 若 handle 已 interrupted/closed 则不二次通知（以 handle 状态为准，不看行）。

## 7. 持久化格式

根目录：`<workspace>/.ai-coding/agents/`（**不是** ledger_root=workspace 本身，
避免写进仓库根）。该 root 注入一个独立 `SessionService`，与 `/resume` 的列表隔离。

- `agents/sessions/`：`SessionService(root=agents/sessions)` 的会话文件
  （`<session_id>.json`，messages/checkpoint/task/compaction 全在）。
- `agents/meta/<run_id>.json`：sidecar（不与会话文件同目录，避免被
  `SessionService` 的 `*.json` 扫描当成幻影会话）。
  ```json
  {
    "version": 1, "runId": "run_x", "name": "helper", "task": "…",
    "sessionId": "sub-x", "parentSessionId": "sid", "workdir": "/…",
    "readonly": false, "agent": "build", "contextMode": "task_only",
    "permissions": [/* rule_to_dict */], "status": "interrupted",
    "lastStatus": "interrupted", "createdAt": "…", "updatedAt": "…"
  }
  ```
- 写入时机：open、turn 结束、interrupt、close、resume。
- 权限用 `permissions.rule_to_dict` / `rules_from_dicts`（**没有** `Rule.to_dict`）。
- `SessionService.load` 会带回 `pending_approval`、`pending_posture`、`write_ahead`、
  `pending_reminders`、`inbox`、`recovered_interrupted`：resume 时必须全部清空
  或重置，否则旧审批/旧消息/旧恢复标记会带进新 turn。
- 子会话 id 以 `sub-` 开头，现有 `_check_id` 接受；独立 root 保证不出现在 `/resume` 列表。

## 8. 工具 schema 与权限

| 工具 | schema | mutates_world | write-ahead | permission_name |
|---|---|---|---|---|
| `send_message` | `{target, message}` 均必填 | True | **排除** | `subAgent` |
| `interrupt_agent` | `{target}` | True | **排除** | `subAgent` |
| `resume_agent` | `{agent_id}` | True | **排除** | `subAgent` |

- `target` 解析只在 `_handles` 里做（closed 不算）；歧义 → `ambiguous_target` + 候选。
- plan/readonly：surface 已按 `permission_name` 判断（`agent_loop.py:3484-3492`），
  `subagent` 规则会隐藏三者；`_PLAN_SOFT_DENY` 不必再加名字（C-5a 加的
  `spawn_agent`/`close_agent` 可保留作可读性，也可删除）。
- **write-ahead**：控制类工具不能在崩溃后被"重放"（会重复投递/重复打断）。
  最小实现点：`core/dispatch.py:617-622` 的 `log.claim(call, allow_replay=...)`；
  对 `send_message`/`interrupt_agent`/`resume_agent` 跳过 claim（没有记录时
  `settle` 自己也是 no-op）。可选做法：给 `BaseTool` 加 `write_ahead_replay=False`，
  dispatch 处读取；不改 replay 的其余语义。
- `interrupt` 对未知/已死也返回 success（Codex 同）；`resume` 对 running no-op。
- 描述里写明：send 到空闲 agent 会起新 turn（把 Codex 的 followup 合并进来）。

## 9. 与 Codex 的显式差异

| # | 差异 | 原因 |
|---|---|---|
| 1 | run id / name 寻址，不用 `/root/<task_name>` 路径 | 无 agent path 树；spawn 时对重名自动加后缀，解析歧义报错 |
| 2 | `send_message` 对空闲 agent 起新 turn | 合并 Codex 的 `followup_task`，满足"复原续跑"；发送即唯一唤醒路径 |
| 3 | `wait_agent` 等完成，不等调用者输入活动 | 现有语义更有用，改造成本高 |
| 4 | resume 用 SessionService+sidecar，不用 rollout | 复用现有会话存储 |
| 5 | mailbox 不持久化 | 与 Codex 一致 |
| 6 | `spawn_agent` 参数仍是 `task/name` | 已有 C-5a 接口；改名放后续 |
| 7 | interrupt 保 handle、close 丢 handle | 与 Codex 的 interrupt/close 分层一致 |

## 10. 分期实施

| 阶段 | 内容 | 验收 |
|---|---|---|
| C-5b-1 | `AgentHandle` + 注入 `SessionService`（`agents/sessions`）+ sidecar（`agents/meta`）+ `_run_in` 拆 `_open_handle`/`_run_turn`；同步 `run()` 走 ephemeral handle；`resolve_run_id`/`wait`/`cancel`/`task_list`/`/tasks stop` 改 handles-first；不新增工具 | 现有 7 件套不变；`sub-*.json` 生成且不进 `/resume`；全量绿 |
| C-5b-2 | `send_message`：每 handle `agent_inbox` + 运行中投递 + 空闲 `_schedule_turn` | T8/T9 |
| C-5b-3 | `interrupt_agent`：turn 级取消、等收尾、handle 保留、无 notice | T10 |
| C-5b-4 | `resume_agent`：running no-op、内存重开、磁盘重开 | T11/T12 |
| C-5b-5 | 权限/write-ahead 排除、并发、plan 隐藏、文档、真机脚本 | T13/T14 |

每阶段：`pytest` 全绿 + `ruff` + `mypy` + 脚本化真机；有 API key 后再补一次真实 provider。

## 11. 验收用例

- **T8** 运行中 send：子代理脚本 2 步，第 1 步后收到 `[agent-message]`；turn 数不增。
- **T9** 空闲 send：新 turn 运行；父收到一次 notice；子会话历史含上一条任务与回复。
- **T10** interrupt：turn 中途取消；行 `interrupted`；无 notice；`open_turn` 已清；
  resume → send → 新 turn 正常，历史保留。
- **T11** 跨进程 resume：新 runner 用 sidecar + session 重开；send 起 turn。
- **T12** close 后 resume：handle 已删、磁盘仍在、可重开。
- **T13** plan/readonly：三个工具被隐藏；`subagent deny` 用户规则也隐藏。
- **T14** 并发：同一 idle handle 两次 send，第二次进 mailbox/排队，只起一个 turn；
  两个不同 handle 的续跑不超过 4。

## 12. 风险

| 风险 | 说明 | 处置 |
|---|---|---|
| 取消竞态 | 内层 turn 未收尾就重建 loop | cancel 后 `await gather(..., return_exceptions=True)`；T10 断言 open_turn 清空 |
| engine 不可复用 | 流式请求取消后状态坏 | interrupt 后 `loop=None`，下次按 session 重建；旧 loop 在安全点 aclose |
| 二次通知 | `_finish` 与 `_reap` 都 announce | `announced` 门 + interrupted/closed 不发 |
| 权限丢失 | 续跑的 child 必须仍受父规则约束 | sidecar 持久化 permissions；重建时注入 |
| workspace 污染 | 子会话/元数据写进仓库 | 放 `.ai-coding/agents/`，加 ignore 说明 |
| 名字冲突 | name 不唯一 | spawn 自动去重；解析歧义返回候选 |
| write-ahead 重放 | 崩溃后重复 send/interrupt | 三个控制工具排除 replay（`dispatch.py:620-622`） |
| 同步路径泄漏 | `run()` 拆开后无人 aclose | 5.3 的 ephemeral handle：turn 后 aclose 并丢弃 |
| 副作用文件被当会话 | sidecar 与 session 同目录 | `agents/meta` 与 `agents/sessions` 分开 |
| mailbox 卡在末尾 | 最后一个 iteration 边界后入队 | turn 结束复检 mailbox，合并一次 follow-up |
