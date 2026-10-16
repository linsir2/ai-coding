# 17 · 阶段二设计（模式 + `/plan` 重定义）——影响审核与实施蓝图

> 状态：**已拍板（2026-10-15）**：Q1=B（白名单 + `memory_search`）/ Q2=A（`/plan run`）/ Q3=B（后端载荷 + 前端状态栏标记）/ Q4=A（纯模式批）。本稿 = `12` §2 的修订版；依 12 第 3 行"阶段二以修订版为准"。
> **重头复核（2026-10-15）**：`18-阶段二重头分析-模式与plan-对照dsh与opencode.md` 从第一性原理独立重推并与 dsh/opencode 源码对照——与本文冲突处以 18 为准（修正：验收命令绕过洞、翻转时点语义；其余设计经对照维持）。
> 依据：`12` §2/§13、`16` F5、以及 **2026-10-15 HEAD 逐文件核对**（不引用旧行号，锚点以函数/内容为准）。
> 范围纪律：只做模式 + `/plan` 重定义；11-任务生命周期排后；F1（触发面）另批；一切"说不清作用"的改动本稿已砍（§11）。
> 前情要点：**12 写于 Textual 时代，其 §2.5 的 UI/y-n 方案已与现实不符**（Textual UI 已删，现前端为 `frontend/` OpenTUI + IPC；`IpcConsole` 无 `ask` 通道）——本稿按现实重写并列出差异（§9）。

---

## 1. 现有设计 vs 现状（核对表）

| 面 | 12 的设想 | 现状（HEAD 实测） | 本稿处置 |
|---|---|---|---|
| 模式字段 | `SessionData.mode`，非 normal 才写 | 不存在；条件持久化规则已有先例（`task` 仅带 items 写、`interventionState` 非空才写，`session.py` to_dict 区） | P0 按同一规则加 |
| plan 提示段 | 接上 `plan_mode` 死接线 | `PromptAssembler.__init__(plan_mode=False)` 构造期死布尔（`ai/prompt.py:45,61`），3 个构造点无人传 True | P2：构造期参数**删除**，改 `build(mode=...)` 参数 |
| 工具闸 | `BaseTool.mutates_world` 默认 True；`ToolContext.mode`；`_decide` unknown 后、gate 前插入 | `BaseTool` 只有 `parallel_safe`（默认 False 保守样式，`tools/base.py:80`）；`ToolContext` 无 mode；唯一真实构造点 `request_builder.tool_context()`（:95，由 `_run_tools` 使用，`agent_loop.py:1254`） | P1：沿 three 个现有接缝加，零新机制 |
| 拒绝的可见性 | "模型看得见拒绝（可审计、可学习）" | 已有先例通道：`dispatch._denied`（`error:"denied"`）/`_guard_denied`（`ToolFinished(success=false)` 自然可见） | P1 新增 `_mode_denied`（专属 error=`mode_denied`，文案自带退出指引） |
| 命令面 | `/plan [topic]` 入模式；`/plan off` **问 y/n** 再执行 | 单入口 `plan_and_run`（规划轮+PlanRunner 顺序拼接，`plan_runner.py:103-147`）；**`IpcConsole` 故意没有 `ask`**（`handlers.py:96-98`）→ 交互问在 IPC 不可行；裸 `/plan` 在 IPC 会静默空转 | P4：拆 `plan_only`；执行入口改为**显式 `/plan run`**（无交互依赖）；裸 `/plan` 只进模式（顺带修掉 IPC 静默空转） |
| UI | "header 标记（小改；超预期降级 notice）" | 前端为 OpenTUI/TS（Textual 已删）：命令终态走 `turn.finished`、状态走 `session.stats`、加载走 `session.load`（`handlers.py:246-253,731-741`）；`console.info` 即 dim notice 行 | P5 后端载荷 + notice 为底；前端标记列为可选（§10-Q3） |
| 决策面（执行类门） | plan 关 task/acceptance 门、保留 strategy/fatigue | 阶段一已建成：policy 两决策点 + `interventions.select`；`TurnSnapshot` 由 `books.snapshot` 建、决策点用 `replace(...)` 搭 `intervention_ledger`（`agent_loop.py:527,740`） | P3：`TurnSnapshot.mode` + `decide_after_step` 早退；**`interventions.py` 零改动** |
| 子代理 | ctx.mode 恒 normal | 子代理自建 `SessionData`（`subagent.py:499+`）→ 默认即 normal；且父在 plan 时 `subAgent` 工具本身会被闸拒绝（默认 mutates_world=True） | P1 默认值即保证；补一条断言测试（R8） |

## 2. 执行范围（做/不做）

**做**：P0 模式字段与持久化 → P1 只读工具闸（`mutates_world`+`ctx.mode`+dispatch 闸）→ P2 提示词 mode 化 → P3 决策面（POST 门抑制 + F5 豁免）→ P4 命令面（`plan_only` 拆分、`/plan|off|run`、`/plan-run` 墓碑）→ P5 载荷（stats/load 带 mode）+ 可选前端标记。
**不做**（§11）：其它模式 / 自动切换 / schema 隐藏工具 / 子代理只读化 / 交互 y/n / 前端大改 / 11 / F1。

## 3. 复用的方法契约（不造轮子，逐条声明）

| 复用件 | 位置 | 本稿用法 |
|---|---|---|
| 条件持久化白名单 | `session.py` to_dict/from_dict（task/interventionState 同规则） | `mode` 非 normal 才写；from_dict 未知值**兜底 normal**（fail-safe） |
| 保守布尔属性样式 | `BaseTool.parallel_safe`（`tools/base.py:80`） | `mutates_world: bool = True` 同为类属性、同默认失关闭 |
| 可见拒绝通道 | `dispatch._denied/_guard_denied`（:527,543） | `_mode_denied` 同形（`ToolResult(success=False)`，事件零新增） |
| ToolContext 单构造点 | `request_builder.tool_context()`（:95） | `mode` 从 `session` 派生，一行接入，全部路径（含 plan 步会话/子代理）自动正确 |
| 规划轮夹具 | `plan_runner.continuations_disabled`（:150） | `plan_only` 原样复用（C3 关闭） |
| 执行引擎 | `PlanRunner`（:224-296）+ 步会话（ephemeral, mode 恒 normal） | **零改动**；`/plan run` 直接构造调用 |
| 命令通道 | `sessions.save(session)`（commands.py:161 先例）、`console.info`（REPL/IPC 均有） | 模式进入/退出落盘与提示 |
| 载荷通道 | `session.load`/`session.stats` 响应（handlers.py） | 各 +1 键 `mode` |
| 决策快照 | `TurnSnapshot` + 决策点 `replace(...)`（agent_loop.py:527,740） | +1 字段 `mode`，两处 replace 各 +1 参 |
| 事件/前端 | `plan.updated`（todo_write 后自动发）、`turn.finished`（命令终态） | 规划期计划面板**自动**工作，零新增事件 |

## 4. 新增功能与逐文件实现（P0–P5）

### P0 · `mode` 字段与持久化（3 文件行级改动）

- `domain/session.py`：
  - 字段区（`git_branch` 一带，runtime 字段旁）：`mode: str = "normal"`，注释：17 §1——plan 为只读姿态，随会话文件存活。
  - 模块常量：`KNOWN_MODES: tuple[str, ...] = ("normal", "plan")`（from_dict 兜底的唯一词表；P4 命令解析复用）。
  - `to_dict`：`if self.mode and self.mode != "normal": data["mode"] = self.mode`（"same rule" 注释）。
  - `from_dict`：读 `mode`，不在 `KNOWN_MODES` → `"normal"`（fail-safe，不落 plan）。
- 证明面：往返、未知值、非 normal 才写键、崩溃读回仍是 plan（R1）。

### P1 · 只读工具闸（1 新助手 + 1 插入点 + 白名单）

- `tools/base.py`：`mutates_world: bool = True`（docstring：默认失关闭；与 `parallel_safe` **正交**——`subAgent` 是 parallel_safe 但执行任意工作，两者不可互推）。
- `tools/base.py` `ToolContext`：`mode: str = "normal"`（docstring：闸的输入）。
- `core/request_builder.py` `tool_context()`：`mode=getattr(session, "mode", "normal") or "normal"`。
- `core/dispatch.py` `_decide`：在 unknown 检查（`item.tool is None` 分支）之后、`verdict = _verdict(...)` 之前插入：
  ```python
  if ctx.mode == "plan" and item.tool.mutates_world:
      return self._mode_denied(call), True, False
  ```
  选此位置的理由：被禁工具不花审批（审批留给真实用户问题）、不被 guard/钩子改写（模式是姿态不是建议）。
- 新助手 `_mode_denied`（放 `_denied` 旁）：
  ```python
  @staticmethod
  def _mode_denied(call: ToolCall) -> ToolResult:
      return ToolResult(
          success=False,
          output=("error: plan 模式只读——该工具会改变工作区或状态，已拒绝。"
                  "读/搜/写计划仍可用；要执行先 /plan off 退出，或 /plan run 直接执行。"),
          error="mode_denied",
          data={"decision": "deny", "reason": "plan mode is read-only", "mode": "plan"},
      )
  ```
- 白名单（**已拍板 Q1=B**；每个工具类各 +1 行 `mutates_world = False`）：`read / glob / grep / todo_write / checkpoint_write / ask_user / skill / memory_search`（8 个；`memory_search` 的持久写仅 touch/stake 元数据，禁它伤规划召回）。明确拒绝（默认 True）：`write/edit/bash/subAgent/mcp_call/memory_write`。
- 关键性质（测试钉死）：失关闭——未来任何新工具/未知 MCP 工具**默认被拒**。

### P2 · 提示词 mode 化（删死参数，加 build 参数）

- `ai/prompt.py`：`__init__(self, base_instructions)`（删 `plan_mode`）；`build(project_instructions="", skills_catalog="", mode="normal")`；`<plan-mode>` 段**原文照旧**（其文本即闸的行为描述），仅触发条件从构造期布尔改为 `mode == "plan"`。
- `core/agent_loop.py` `_build_instructions`（:1892，`session` 参数已在手）：`self.prompt.build(..., mode=getattr(session, "mode", "normal") or "normal")`。
- `cli.py:161`、`subagent.py:490,497`：**零改动**（从未传 `plan_mode`；参数删除对它们是透明的）。
- normal 模式输出与今日**逐字节相同** → golden/loop_events 零重录。

### P3 · 决策面（POST 门抑制 + F5 豁免）

- `core/turn_policy.py` `TurnSnapshot`：+ 字段 `mode: str = "normal"`。
- `core/turn_policy.py` `decide_after_step` **函数首行**：
  ```python
  if snapshot.mode == "plan":
      # 17 §4-P3: in plan mode the execution gates describe *running* the plan,
      # not writing it -- a task nudge here pushes the model to execute in a
      # mode that structurally refuses execution.  Planning ends when the model
      # stops writing items; strategy/fatigue stay armed (they describe thinking).
      return TurnDecision(TurnAction.FINISH, "model finished")
  ```
  理由：抑制放 policy（"姿态"是决策面的事），`interventions.py` 保持模式无关（阶梯只管"卡住"不管"姿态"）。reason 复用现成 `"model finished"`，零新词表。
- `core/agent_loop.py` 两个决策点（`replace(` 于 :527/:740）：各 +1 参 `mode=getattr(session, "mode", "normal") or "normal"`。
- F5 豁免（`_begin_turn` 裸 goal 刷新分支）：条件加一项 `and getattr(session, "mode", "normal") != "plan"`——规划期 goal 归 `begin_plan` 的主题管，闲聊补充不夺锚。normal 行为零回退（16-F5 两测继续绿）。

### P4 · 命令面（拆 `plan_and_run`，立 `/plan off|run`）

- `core/plan_runner.py`：
  - `plan_only(loop, session, *, topic: str, on_delta=None, observer=None) -> PlanReport`：现 `:126-145` 逻辑原样搬（工作区闸#1 → `begin_plan(topic)` → `continuations_disabled` 规划轮 → 闸#2）。**`topic` 必填**（裸 `/plan` 不再经此，`ask_topic` 参数一并砍——无调用者）；`console` 参数不需要（规划半边不打印，命令层负责提示）。
  - **`plan_and_run` 删除**（组合语义由命令层两步显式表达；调用点仅 commands.py 与两个测试，一并迁移）。
  - `PlanRunner`、`continuations_disabled` 零改动；`__all__` 同步。
- `app/commands.py` `/plan` 分支重写（现 :235-254）：
  ```python
  if cmd.name == "plan":
      from ai_coding.core.plan_runner import PlanRunner, plan_only
      rest = cmd.raw[len("/plan"):].strip()
      if rest == "off":
          if session.mode != "plan":
              console.info("[plan] 当前不在计划模式。")
          else:
              session.mode = "normal"
              sessions.save(session)
              open_n = len(session.task.open_items()) if getattr(session, "task", None) else 0
              console.info(f"[plan] 已退出计划模式；{open_n} 项待执行——/plan run 执行。"
                           if open_n else "[plan] 已退出计划模式（无计划项）。")
      elif rest == "run":
          if session.mode == "plan":
              session.mode = "normal"
              sessions.save(session)
          await PlanRunner(loop).run(session, console, on_delta=console.delta, observer=observer)
      else:
          if session.mode != "plan":
              session.mode = "plan"
              sessions.save(session)
              console.info("[plan] 已进入计划模式（只读）：可阅读与写计划；/plan off 退出，/plan run 执行。")
          if rest:
              report = await plan_only(loop, session, topic=rest, on_delta=console.delta, observer=observer)
              if report is not None and report.stopped:
                  console.error(report.stopped)
              else:
                  open_n = len(session.task.open_items()) if getattr(session, "task", None) else 0
                  console.info(f"[plan] 计划就绪：{open_n} 项。可继续对话补充（如“把第 3 步拆细”）；"
                               "/plan run 执行，/plan off 退出。")
      return REPLAction.CONTINUE, session
  ```
  - `_ask_topic`（:56-61）删除——**没有任何路径再需要交互问**（裸 `/plan` 进模式、带题即规划）。
  - 解析保留：主题恰为单字 `off`/`run` 时按子命令解释（文档注明规避法：写完整句子）。
  - 语义补记：已在 plan 模式时再收 `/plan <topic>` = **显式重规划**（`begin_plan` 会清 items/收据/账本——这是既有 begin_plan 契约，非新行为）。
- `app/slash.py` `_DEPRECATED`（:99）：`/plan-run` 墓碑提示改指 **`/plan run`**（实施期按 `suggest_command` 返回格式微调；若该函数只能回单命令名，则保持指 `/plan` 并在文档注明）。
- `server/handlers.py` `IpcConsole` 注释（:96-98）迁移：改述为"裸 /plan 现在只进入计划模式，不需要问主题"。

### P5 · 载荷与可见性

- `server/handlers.py`：`session_stats`（:731-741）+ `"mode"`；`session_load`（:246-253）+ `"mode"`。（后端契约先落，前端消费可后随。）
- notice：P4 已用 `console.info`（REPL 控制台/IPC `console.line`）——进入/退出/就绪三处可见。
- 前端状态栏标记（**已拍板 Q3=B，本批做**）：`frontend/src/routes/session.tsx` Stats 接口 +1 字段、`status-bar.tsx` 渲染一段 `PLAN·只读`（实施期核实前端构建/测试基建并纳入门禁）。

### 迁移清单（sanctioned，一处收）

| 测试 | 现况 | 迁移 |
|---|---|---|
| `tests/test_plan_command.py` | 2 例（begin_plan 行为、规划轮 C3 关闭） | 按新语义重写：裸=进模式、topic=规划、off、run、桩验证 |
| `tests/test_plan_workspace_gate.py` | 直调 `plan_and_run` | 改调 `plan_only(topic=...)`（闸#1/#2 语义不变） |
| `tests/test_repl.py:309-327` | 裸 `/plan` 经 console.ask 取题 | 裸 `/plan`=进模式（ask 语义亡）；保留 topic 路径断言 |
| `tests/test_repl.py:841-866` | monkeypatch `plan_and_run` 验 observer 穿线 | 改 patch `plan_only`（规划半边）；`/plan run` 的 observer 走 PlanRunner（既有测试覆盖） |
| `tests/test_prompt.py:6-7,30-33,66-67` | `PromptAssembler(plan_mode=…)` | `build(mode=…)` |
| `tests/test_plan_runner*.py`（runner/m1/budget/proof/attestation） | 直调 `PlanRunner` | **零改**（作为"零改动"证明保留） |

## 5. 数据流（改前 vs 改后）

```
改前： /plan topic → begin_plan → 规划轮(全工具开放，凭提示词自觉) → 立即执行整个计划（一步到底）
       裸 /plan（IPC）→ ask_topic=None → 静默返回（空转 bug）

改后：
/plan [topic] ──▶ mode=plan（save）──▶ 提示词带 <plan-mode> 段
     │                                  工具闸：mutates_world 工具一律可见拒绝(mode_denied)
     │                                  决策面：POST task/acceptance 门静默；strategy/fatigue 保留
     │ 带 topic: plan_only = begin_plan → 规划轮(continuations 关) → todo 计划成形
     ▼
  「聊商划」循环：read/grep/memory_search/todo_write 自由；write/bash/subAgent 被拒；plan.updated 实时上屏
     ▼
/plan off ──▶ mode=normal（save）──▶ notice(N 项待执行)   ／  /plan run ──▶ 退出+PlanRunner 执行（步会话 mode=normal）
     ▼
 崩溃/重启：mode 随会话文件存活，重启仍是只读姿态（R1）
```

## 6. 影响面矩阵（逐文件）

| 文件 | 动作 | 大小 |
|---|---|---|
| `domain/session.py` | +字段、+常量、to_dict/from_dict 各一处 | ~10 行 |
| `tools/base.py` | +`mutates_world`、+`ToolContext.mode` | ~6 行 |
| `tools/{read,glob,grep,todo_write,checkpoint_tool,ask_user_tool,skill_tool[,memory_search_tool]}.py` | 各 +1 行 `mutates_world = False` | 1 行/文件 |
| `core/request_builder.py` | `tool_context()` +1 参 | 1 行 |
| `core/dispatch.py` | +闸（4 行）+ `_mode_denied`（12 行） | ~16 行 |
| `ai/prompt.py` | 删构造参、build +mode | ~5 行 |
| `core/agent_loop.py` | `_build_instructions` +1 参；两决策点 replace +1 参；F5 豁免 +1 条件 | ~4 行 |
| `core/turn_policy.py` | `TurnSnapshot.mode` + `decide_after_step` 早退 | ~8 行 |
| `core/plan_runner.py` | 拆 `plan_only`、删 `plan_and_run` | 净 -5 行 |
| `app/commands.py` | `/plan` 分支重写、删 `_ask_topic` | ~+35/-15 行 |
| `app/slash.py` | 墓碑文案（小） | 1-3 行 |
| `server/handlers.py` | stats/load 各 +1 键、注释迁移 | ~4 行 |
| `frontend/*`（Q3=B） | Stats 接口 + 状态栏段（+刷新链路核对） | 2-3 文件 |
| 测试 | §4 迁移表 + 新 `test_session_mode.py`/`test_mode_gate.py` | — |

**明确零改动**：`interventions.py`、`PlanRunner`、`continuations_disabled`、`closer`/审批/快照/压缩/subagent 主体、golden 事件。

## 7. RIPR 用例表（红→实现→证明，缺一不可）

| # | Red（先写、先红） | Implement | Prove |
|---|---|---|---|
| R1 | `test_session_mode.py`：默认无 `mode` 键；`plan` 写键；未知值读回 `normal`；往返等价；save/load 后仍是 plan | P0 | 5 绿 + 全量 |
| R2 | `test_mode_gate.py`：①plan 下 write/edit/bash/subAgent/memory_write/mcp_call 逐个被拒且 `error="mode_denied"`、文案含指引 ②白名单工具逐个放行 ③**陌生工具对象（默认属性）被拒**（失关闭证明）④拒绝先于审批（无 `ToolApprovalRequested`）⑤normal 全放行 | P1 | 全绿 |
| R3 | `test_prompt.py` 迁移 + 新增：`build()` 默认**不含** plan 段（逐字节=旧 normal）；`build(mode="plan")` 含段且序在 methods 后 | P2 | 绿 + golden 零动（对照既有 golden 跑） |
| R4 | `test_turn_policy.py` 增：plan+open_items → FINISH `"model finished"`（零 nudge）；plan+failed_streak=2 → 仍 NUDGE strategy L1；plan+acceptance 失败 → 不触发 acceptance 门 | P3 | 绿 |
| R5 | plan 模式新增用户回合不刷 goal（主题保留）；normal 下 F5 两测原样绿 | P3 | 绿 |
| R6 | `test_plan_command.py` 重写（5-6 例）：裸=进模式+save+零模型调用；topic=`begin_plan`+规划轮+`max_auto_continuations==0` 且还原；off=退出+提示；run=先退再执行（PlanRunner 桩）；重复 `/plan <topic>`=重规划；`plan_only` 闸#1/#2（迁移 workspace_gate） | P4 | 全绿 |
| R7 | `session_stats`/`session_load` 响应含 `mode` | P5 | 绿 |
| R8 | 子代理不继承：plan 模式父会话下发 `subAgent` 被拒（并入 R2 清单）；子循环 ctx.mode=="normal"（现有 subagent 夹具补一条断言） | P1（默认值） | 绿 |
| R9 | 收口：全量 pytest + mypy + ruff + r7e/r8/r18 + 前端构建（Q3=B）+ **r19 新场景（真机）**：进模式→模型试写被拒（可见）→写计划→`/plan run` 执行→报告正常 | 全部 | 全绿 + 验收报告（16 式） |

## 8. 成功后的模样（真机剧本 = 细节正确性的总检查）

```
> /plan 给编排层的工具闸补文档
  [plan] 已进入计划模式（只读）：可阅读与写计划；/plan off 退出，/plan run 执行。
  （规划轮：read/grep 自由 → todo_write 写出 6 步含 verify → 收尾；POST 门全程静默）
  [plan] 计划就绪：6 项。可继续对话补充（如“把第 3 步拆细”）；/plan run 执行，/plan off 退出。
> 把第 3 步拆细
  （只读闸放行 read/todo_write；模型 edit 计划；plan.updated 实时刷新）
> 你直接改代码吧
  （模型调 write → ToolFinished(success=false)：plan 模式只读——…已拒绝；模型改道）
> /plan run
  [plan] 完成 5 步 · 用了 5 回合…（原执行引擎，步收据照旧）
```
会话文件对照：规划期 `"mode": "plan"` 落盘一次；退出后 save 使键消失（条件写）；`stepDeliveries`/`task` 行为零变。

## 9. 自核（逐条对照）

- **每个新东西的存在理由/消费者**：`mode`（持久化→闸→prompt→快照→载荷，五消费者）；`mutates_world`（闸输入）；`ToolContext.mode`（闸输入）；`TurnSnapshot.mode`（POST 抑制输入）；`_mode_denied`（可见拒绝）；`plan_only`（规划半边）；`KNOWN_MODES`（from_dict 兜底 + 命令词表）。无"写了没人读"项。
- **砍掉的**（说不清/冗余即砍）：`run_plan` 包装函数（commands 直接构造 `PlanRunner`——单入口）；`ask_topic` 参数与 `_ask_topic` 助手（无交互路径）；`plan_and_run` 组合器（无调用者）；`interventions.py` 改动（抑制属决策面）；TUI prefill 调整（无害不动）。
- **与 12 的差异**（全部显式）：① UI 段按 OpenTUI 重写（Textual 已删）② y/n → `/plan run`（IpcConsole 无 ask，12 写于旧前端时代）③ "arm"一词落地为"退出即执行"（arm 概念属旧账本设计，阶段一未建）④ §2.4 的 build 参数落地为 `mode` 字符串。
- **与 11 交叉**：均触 `_begin_turn`（11 的 save、本稿 F5 豁免行）——先后执行以先到者行号为准（12 §13.5 同纪律）。
- **行号纪律**：本稿行号为 2026-10-15 HEAD 快照，实施以函数/内容锚点定位。
- **推演核对**（§8 剧本倒推）：POST 抑制若放 interventions 会引入模式耦合→改放 policy ✓；闸若放 schema 层工具会"消失"（不可学习）→放 dispatch 可见拒绝 ✓；裸 `/plan` 即刻 begin_plan 会毁掉"聊商划"（主题未定）→只进模式 ✓；`plan_only` 不给 console 参数→提示由命令层统一 ✓。

## 10. 拍板回执（2026-10-15）

- **Q1 = B**：白名单 = `read/glob/grep/todo_write/checkpoint_write/ask_user/skill/memory_search`（网络工具不入）。
- **Q2 = A**：执行入口 = `/plan run` 子命令；`/plan-run` 墓碑文案指到它。
- **Q3 = B**：后端载荷 + 前端状态栏常驻 `PLAN·只读` 标记。
- **Q4 = A**：纯模式批；F1 独立另批，F2/B 各自单列。

## 11. 不做清单

模式自动切换 / plan 以外的模式 / schema 级工具隐藏 / 子代理只读继承（v1 整只 subAgent 拒绝）/ 交互 y/n / `/plan off` 自动执行 / 前端大改 / 11-任务生命周期 / F1 触发面 / 干预系统改动（零改）。

## 12. 风险与实施期验证点

1. **规划期模型反复试写被拒**：拒绝文案自带三条出路；strategy 阶梯兜底；r19 真机场景验证"被拒后改道"（R9）。
2. **裸 `/plan` 后直接聊计划**：不设 goal（begin_plan 未跑）——已记录为已知边界；功能无影响（`/plan run` 只看 items）。
3. **测试迁移面**：4 个文件（§4 表）+ 新增 2 个测试文件；`test_plan_runner*` 零改作为"执行引擎未动"证明。
4. **前端（Q3=B 已定）**：TS 构建/测试纳入门禁；若前端基建缺测试则以构建通过 + r19 人工核对为准。
