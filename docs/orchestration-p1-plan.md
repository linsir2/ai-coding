# P1 实施计划：mode 投影、plan⇒readonly 不变量、readonly spawn（评审修订稿）

> 前置：P0b 全部完成（`c7832a7..1fe8a58`），另有真机回归修复 `03208c5`
> （规划回合无步数上限/无预算压力）。本计划只做 P1，不碰 P3 的写协调与 P4 的
> Scheduler/TaskRegistry。
>
> 依据：`docs/orchestration-unified-design.md` §2、§7；代码复核 + 真机
> （qwen3.7-flash）在 build/readonly/plan/plan→implement/子代理读/子代理写/
> 后台子代理七种链路上的实测；方案评审（子智能体）修订。

## 0. 现状（已核，含行号）

- **单一真源已经存在**：`SessionData.agent` + `SessionData.readonly`；
  `SessionData.mode` 返回 `"normal" | "plan"`（`session.py:466-475`），
  `security/permissions.py::posture_of` 投影出 `plan | readonly | build`。
  **不新增 TurnMode 类型、不改 wire**，直接复用这两个投影。
- **写路径没有强制不变量**：`commit_posture`（`agent_loop.py:735-742`）逐字段写入，
  允许 `agent="plan"` + `readonly=False`；`request_posture` 按目标逐字段比较；
  **读路径更没管**：`SessionService.load → SessionData.from_dict`（`session.py:827-830`）
  把旧文件里的失配原样读回，`_verify_plan_item` 等直接读 `session.readonly`
  绕过 gate（`agent_loop.py:2393`）。
- **`/readonly off` 在 plan 下被接受**：`commands.py::_handle_readonly` 无条件
  `request_posture(readonly=False)`，结果是 plan 规则仍在（`ctx.mode=="plan"`）
  但 `ctx.readonly` 变 False，两个边界说法不一致。
- **plan 下编排工具已被规则层禁止**：`domain/agents.py` 的 plan 规则
  （`_PLAN_SOFT_DENY`，`agents/registry.py:70-77`）对
  `subAgent/spawn_agent/send_message/interrupt_agent/resume_agent/close_agent`
  返回 deny（错误码仍是 `denied`）；`wait_agent/list_agents/subagent_list` 是
  刻意保留的只读观察口。`mutates_world=True` 只是第二道。**P1 不改错误码、
  不改规则表。**
- **spawn 的只读能力在 runner 里已有、工具面缺失**：`core/subagent.py` 的
  `_open_handle` 同时写 `session.readonly` 与 `handle.readonly`（`:1334/:1344`），
  resume 读 meta（`:769`）；但 `SubAgentTool` / `SpawnAgentTool` 不暴露 `readonly`，
  `spawn_background` 还把 `readonly=False` 写死（`:459`）——后台子代理永远可写。
- **gate 只看类属性**：`dispatch.py:496-502` 用 `item.tool.mutates_world` 拒绝只读
  调用，无法区分“只读 spawn / 可写 spawn”。
- **工具面过滤**：`_surface_tools`（`agent_loop.py:3089-3170`）只读按
  `mutates_world` 掉工具；`subAgent` 是 deferred（`builder.py:482-497`），
  `spawn_agent` 非 deferred。plan（readonly=True）下若裸加 `readonly_capable`
  会把 `spawn_agent` 泄漏进表面。

## 1. 决策与契约

1. **plan ⇒ readonly（coerce，不是拒绝）**：共用一个
   `_normalize_posture(agent, readonly)`；`request_posture` 用它算 target 再比较，
   `_pending_target` 也用它；`commit_posture` 兜底。**不新增 `PostureResult` 值**
   （四值契约保持）。
2. **读路径同样归一化**：`SessionData.from_dict` 读到 `agent` 为 plan 时
   `readonly=True`（旧文件自愈），与 P0b-1 对 armed 的处理同一原则。
3. **`/readonly off` 在 plan 下拒绝**（命令层直接报错，不入队）：
   条件同时看 `session.mode=="plan"` 与 `pending_posture` 的目标 agent 是 plan
   ——否则它会走到 `CANCELLED` 把排队中的 `/plan` 撤掉。提示「先 `/plan off`」。
   `/readonly on` 在 plan 下照常（若已有 pending，返回 `CANCELLED`，不改文案）。
4. **参数感知的写边界**：`BaseTool.mutates_world_for(params) -> bool` 默认
   `self.mutates_world`。只读拒绝（`dispatch.py:496-502`）改用它。
   **严格真值**：只有 `params.get("readonly") is True` 才算只读；其余（False、
   `"false"`、缺省）一律视为可写（fail-closed）。`execute` 对非 bool 的
   `readonly` 返回 `invalid_readonly`（工具边界），两处共用同一个解析函数。
   **不改** `dispatch.py:629-658` 的 write-ahead（保持类属性：只读 spawn 仍要记
   claim，resume 不得重复起子代理），**不改** `plan_exception`。
5. **guard/hook rewrite 后重算姿态**：`dispatch.py:510-541` rewrite 后除重跑
   `_check`，还要用新 params 重算 `mutates_world_for` 并重跑只读/plan 判定（fail-closed）。
6. **spawn 参数**：`subAgent` 与 `spawn_agent` 都接受 `readonly: bool`（默认 False）；
   `spawn_agent` 另加 `agent: str`（与 `subAgent` 对齐，`_resolve_target` 已有
   primary/unknown 拒绝）。`mutates_world_for` 按第 4 条。
   `spawn_background` 增加 `readonly` 并传给 `_open_handle`（session+handle 都写，
   见现状）；resume 沿用 meta。
   **只读 spawn 不隔离**：`isolate = isolate and not readonly` —— 只读子代理要读
   活工作区，不能读 worktree 的 HEAD 快照（fan-out 已实测过这个坑）。
7. **只读工具面**：新增 `BaseTool.readonly_capable: bool = False`，
   `subAgent`/`spawn_agent` 置 True。只读过滤条件必须是
   `not tool.mutates_world or (tool.readonly_capable and not plan)`；
   `subAgent` 仍是 deferred（只读下需 `tool_search` 载入），`spawn_agent` 直接可见。
8. **plan 下编排工具**：沿用既有 plan 规则（`denied`），不新增 `plan_forbidden`
   属性/错误码/前端标签；`wait/list/subagent_list` 的观察口保持。
9. **写策略不做**：`write=none|serial|isolated` 属 P3；P1 只保证 plan/readonly 的
   工具面与 gate 不产生写。

## 2. 数据流

```
/plan <topic>        → request/commit 归一化 (plan, readonly=True)
                     → 表面：既有 plan 规则拒绝 6 个编排工具；可 tool_search 载入 parallel_research
/readonly off (plan) → 拒绝（看 mode 与 pending 目标；不改字段）
/readonly on  (build)→ commit(readonly=True)；表面掉写工具 + readonly_capable(spawn_agent)
spawn_agent(readonly=False) 在 build    → gate: mutates_world_for=True → 允许
spawn_agent(readonly=False) 在 readonly → gate: True → readonly_denied
spawn_agent(readonly=True)  在 readonly → gate: False → 允许；子会话+handle readonly=True，不隔离
rewrite hook 把 readonly=True 改成 False → 重算后按可写判定（fail-closed）
plan 批准 → commit(agent=build, readonly=False) → build 主闭环（既有）
```

## 3. 分步实施

### P1a：不变量（写路径 + 读路径）+ `/readonly off` 拒绝

- `agent_loop.py`：`_normalize_posture`；`request_posture`/`_pending_target`/
  `commit_posture` 全部使用。
- `domain/session.py::SessionData.from_dict`：plan agent ⇒ readonly 归一化。
- `commands.py::_handle_readonly`：plan（含 pending plan）拒绝 `off`。
- 测试：
  - `tests/test_posture_result.py`：plan 目标强制 readonly、NOOP/QUEUED/CANCELLED 语义。
  - `tests/test_session_mode.py` 或 `test_domain_session.py`：旧文件
    `agent="plan", readonly=False` 读入后 readonly=True、to_dict 只写 True。
  - `tests/test_plan_command.py`：`/readonly off` 在 plan 被拒（含排队中的 plan）；
    `/plan off` 恢复 writable。

#### P1a 实施记录（完成）

- `AgentLoop._normalize_posture` 用于 `request_posture` / `_pending_target` /
  `commit_posture`；排队 sidecar 本身存归一化后的 pair。
- `SessionData.from_dict` 读入归一化；`to_dict` 兜底只写 `readonly: True`；
  `SessionService.create` 的构造默认值同样归一化（评审 P2）。
- `/readonly off` 在 plan（含排队中的 plan）拒绝，文案区分「当前已是 plan」与
  「进入 plan 已排队」；不调用 `request_posture`。
- 评审结论：可提交（无 P0/P1），P2 已修：create 入口、to_dict 兜底、排队文案、
  文档措辞、旧 sidecar 落地用例、legacy `mode:"plan"` 用例、build+readonly 保持。
- 校验：全量 `pytest` 0 failed；`ruff`/`mypy` 绿。

### P1b：参数感知写边界 + readonly spawn

- `tools/base.py`：`mutates_world_for`、`readonly_capable`、共享严格解析。
- `core/dispatch.py`：只读拒绝改用 `mutates_world_for(call.params)`；rewrite 后重算。
- `agent_loop.py::_surface_tools`：`readonly and not plan` 条件。
- `tools/subagent_tool.py`：schema `readonly`；execute 与 `_spawn_background` 透传；
  `readonly_capable=True`。
- `tools/agents_tool.py`：`SpawnAgentTool` schema `readonly`/`agent`；
  `readonly_capable=True`；`mutates_world_for` 覆写。
- `core/subagent.py::spawn_background`：`readonly` 透传；只读不隔离
  （由工具算好 `isolate` 传入也可，二选一，必须只有一处定义）。
- 测试：
  - `tests/test_mode_gate.py`：readonly 允许只读 spawn、拒绝可写 spawn；
    点名调用绕过表面（直接 dispatch `spawn_agent(readonly=false)`）；
    `"readonly": "false"` 视为可写；rewrite 翻转 readonly 被拒。
  - `tests/test_tool_subagent.py`、`tests/test_agents_tools.py`：schema/透传/非法类型。
  - `tests/test_subagent_handles.py`：后台 handle 的 `session.readonly` 与
    `handle.readonly` 一致、resume 后仍只读。
  - `tests/test_builder.py`（或表面测试）：readonly 下 `spawn_agent` 可见、
    `subAgent` 需 tool_search；plan 下二者都不可见。
  - plan 规则不放宽：`tests/test_plan_codex_path.py` 或 `test_mode_gate.py`
    断言 plan 下 spawn 仍是 `denied`（不是新码）。

#### P1b 实施记录（完成）

- `tools/base.py`：`parse_readonly` 是全仓唯一的严格解析（`True` 只读、`False`/缺省可写、
  present 非 bool 返回 `None`）；`readonly_capable` 与 `mutates_world_for` 就位。
- `core/dispatch.py`：`_readonly_refusal` 用 `mutates_world_for(call.params)`，首次判定、
  guard rewrite、hook rewrite 三处共用并在 rewrite 后用新参数重算；write-ahead 仍用
  类属性（只读 spawn 仍记 claim）。
- `agent_loop._surface_tools`：`not mutates_world or (readonly_capable and not plan)`；
  plan 下不泄漏，readonly 下 `spawn_agent` 直接可见、`subAgent` 仍需 `tool_search`。
- `tools/subagent_tool.py` / `tools/agents_tool.py`：`readonly` schema、严格校验
  （`invalid_readonly`）、同步/后台透传；`spawn_agent` 另有 `agent` 透传。
- `core/subagent.py`：“只读不隔离”收口到 `_should_isolate`，由 `_run_isolated`（同步）
  与 `_supervised`（后台，读 `handle.readonly`）调用；工具层只传 `isolate`/`readonly`。
  `spawn_background` 的 `readonly` 进 `_open_handle`（session 与 handle 同写）。
- 评审结论：无 P0/P1 逻辑缺陷；P1 测试缺口（`SubAgentTool` gate 覆盖）已补，且验证
  删除覆写时用例会红；P2 已修（隔离收口、死分支、`__all__`、plan+`include_deferred`、
  只读后台不建 worktree 用例）。
- 校验：全量 `pytest` exit 0（28 skipped 均为 bwrap 不可用）；`ruff`/`mypy` 全绿；
  前端 `bun test` 544 pass、`tsc` 干净。

### P1c：矩阵与真机

- 单测矩阵：`{build, readonly, plan} × {spawn 可写, spawn 只读}` + `/readonly on|off`、
  `/plan on|off`；断言 gate 错误码与表面差异。
- 真机（qwen3.7-flash）：readonly 里 `spawn_agent(readonly=true)` 起得来且子代理只读；
  `readonly=false` → `readonly_denied`；plan 里 spawn → 既有 `denied`；三模式主链路
  不回归。

#### P1c 实施记录（完成）

单测矩阵落在既有用例里，不新增重复矩阵：`test_mode_gate.py` 已覆盖
`{build, readonly, plan} × {spawn 可写, spawn 只读}`（含点名绕过表面、字符串
`"readonly"`、缺省、rewrite 翻转）、`SubAgentTool` 与 `SpawnAgentTool` 两套；
`/plan`、`/readonly` 的开关语义在 `test_plan_command.py` / `test_posture_result.py`。

真机（qwen3.7-flash，DashScope 兼容模式）：

| 场景 | 结果 |
|---|---|
| build 短任务（写一个文件） | COMPLETED，`write` 一次，文件存在 |
| readonly 主链路 | COMPLETED，未写文件；表面出现 `spawn_agent` |
| readonly 起只读子代理 | 成功，run `413f38aca292`；meta `readonly=true`、`workdir=null`（不隔离）；wait 3.3s completed |
| readonly 起可写子代理 | `readonly_denied`（`decision=deny, reason=read-only session`）；模型重试 3 次仍被拒 |
| plan 里起子代理 | 表面无 `spawn_agent`；`tool_search "spawn_agent"` 返回“no deferred tool matched”，只列出 `memory_search/parallel_research/subagent_list`；计划块产出 |
| build 子代理改代码 | `spawn_agent(readonly=false)` 成功，子代理创建 `sub_written.py`，父会话读回 `print('sub-ok')` |
| build 长任务（模块+测试+跑 pytest） | COMPLETED，4 步，`calc.py`/`test_calc.py` 存在，pytest 通过 |
| plan 步数上限=12 | 8 步、无 `[budget]`、COMPLETED、`<proposed_plan>` 存在（预算修复未回归） |

## 4. 影响面清单

| 文件 | 改动 |
|---|---|
| `core/agent_loop.py` | `_normalize_posture`；`request_posture`/`_pending_target`/`commit_posture`；`_surface_tools` |
| `domain/session.py` | `from_dict` 的 plan⇒readonly 归一化 |
| `app/commands.py` | `/readonly off` 在 plan（含 pending）拒绝 |
| `tools/base.py` | `mutates_world_for` / `readonly_capable` / 严格 readonly 解析 |
| `core/dispatch.py` | 只读拒绝参数化；rewrite 后重算 |
| `tools/subagent_tool.py` | `readonly` 参数（同步/后台） |
| `tools/agents_tool.py` | `spawn_agent` 的 `readonly`/`agent`；`mutates_world_for` |
| `core/subagent.py` | `spawn_background(readonly=...)`；只读不隔离 |
| `tests/*` | P1a/P1b 用例 + 矩阵 |

**不改**：wire 事件形状/错误码、`posture_of` 返回值、plan 规则表、write-ahead、
`plan_exception`、计划面板、`subagent_worktree_isolation` 配置语义。

## 5. 验收

- `pytest` 全量 0 failed；`ruff`/`mypy` 全绿；前端 `bun test` + `tsc`。
- 不变量：新写入的 `agent=="plan"` 必带 `readonly=True`；旧文件读入即自愈；
  `/readonly off` 在 plan 下不改任何字段。
- gate：readonly 会话可写 spawn → `readonly_denied`；只读 spawn 通过；
  `readonly` 传字符串/缺省 → 视为可写并拒绝；rewrite 翻转后拒绝。
- plan：编排工具仍按既有规则 `denied`；`spawn_agent/subAgent` 不在表面，
  `wait/list/subagent_list` 观察口保留。
- 只读 spawn 的子会话与 handle 都只读，且读活工作区（无 worktree）。

## 6. 风险

- `readonly_capable` 让只读会话“看得见”可写类工具，安全完全依赖 gate 的参数裁决；
  必须有“点名调用绕过表面”的用例。
- readonly 的真值解析只能有一处；两套 `bool()` 会让 gate 与 execute 漂移。
- rewrite 后不重算姿态会开一个绕过口；实现时先写失败用例。
- 只读 spawn 若仍隔离会读 HEAD 快照，子代理看到的是旧代码；`isolate` 必须由
  同一个地方决定。
