# 18 · N1 研究出口（finish_research）

> 状态：**✅ 已实施（2026-10-21）**。本文是实施蓝图 + 实施记录。
> 依据：`17-plan模式架构对比与目标形态.md` §4.4（N1）、`15-plan模式实测解剖.md` §1/D1/D2、`08-三方对比与内化设计.md` §T4/S-14/R08。
> 边界：本批**只加出口**，不动执行层契约（批准→arm→执行→验收→收据）。

---

## 1. 目标与最终模样

### 1.1 今天的样子

计划模式只有一个出口：`present_plan`（`ai_coding/tools/present_plan_tool.py`）。守卫 `PlanSubmissionGuard`（`ai_coding/core/turn_policy.py:206-220`）在回合末尾检查「有没有交计划」，没有就 nudge 一次，再没有就以 `PLAN_UNSUBMITTED_NOTE` 结束。

于是**研究型请求没有合法出路**：实测（`15` 号稿 §1）里，模型先把活干完（read/grep/glob 的 `mutates_world=False`，只读闸拦不住），守卫再追讨计划，模型只好补交一份「已经做过的事」的计划，`verify` 是编出来的。

### 1.2 本批做完的样子

计划模式有**两个出口**：

| 出口 | 工具 | 结果 |
|---|---|---|
| 交计划 | `present_plan` | 评审卡 → 批准即 arm → 执行层接管 |
| 交结论 | `finish_research(summary)` | 结论即交付物；回合结束；**不 arm、不执行、不弹卡** |

两个出口**互斥**：一个回合只有一个出口，先成功者赢，后到者被拒（`already_concluded`）。

### 1.3 三个不变量

1. **执行层不醒**：研究出口不写 task、不 arm、不落执行计划 → 收据/验收/boot 续跑全不触发。
2. **人要批准才执行**：研究出口不批准任何东西；它只是「不执行」的另一条路。
3. **回合内单出口**：批准（plan）与收尾（research）不能同时成立，否则会出现「结论已给、评审卡又弹出来」。

---

## 2. 契约与复用

### 2.1 复用什么（**不造轮子**）

| 复用 | 位置 | 用法 |
|---|---|---|
| `present_plan` 的姿态门 | `present_plan_tool.py:133-134` | `ctx.mode != "plan"` → `not_in_plan` |
| `present_plan` 的忙/已批准门 | `present_plan_tool.py:138-162` | `armed` → `task_busy` / `plan_already_approved` |
| 评审搁置的「回合在批次末结束」手法 | `agent_loop.py:943-967` | 研究出口用同一形状：批次后 `break` |
| 门卫的白名单档 | `permission_gate.py:402-468` | `finish_research` 进 `_allow_tools`（不ask谁） |
| 守卫的 nudge 文本位 | `turn_policy.py:56-70` | nudge 改成「两个出口」 |

### 2.2 新增什么（**每个都要说得出作用**）

| 新增 | 位置 | 作用 |
|---|---|---|
| `TurnConclusion` | `tools/base.py` | 一个回合的出口持有者（谁赢了）；**回合的，不是 task 的** |
| `FinishResearchTool` | `tools/finish_research_tool.py` | 研究出口本体 |
| `ToolContext.conclusion` | `tools/base.py` | 让两个出口互相看见 |
| `PLAN_RESEARCH_NOTE` | `turn_policy.py` | 回合末的终局说明（与 `PLAN_DISMISSED_NOTE` 同形） |

### 2.3 砍掉什么（**说不清作用就砍**）

- **不给 `finish_research` 加「必须先探索过」的门槛**：没有可靠的「探索过」事实，猜出来的门槛只会误伤。
- **不把它做成 build 模式可用**：它属于计划姿态的门（`not_in_plan`），与 `present_plan` 同源。
- **不改守卫的判定口径**：研究出口在批次末就结束了回合，守卫根本不会被咨询；给 `TurnSnapshot` 加字段是给不存在的问题写代码。
- **不做前端专属渲染**：结论就是 tool result 的文本，走既有渲染。

---

## 3. 逐文件详设

### 3.1 `ai_coding/tools/base.py` — 出口持有者

```python
@dataclass
class TurnConclusion:
    exit: str = ""
```

`ToolContext.conclusion: TurnConclusion | None = None`。回合级共享对象：loop 每回合建一个（`agent_loop.py:616`），`tool_context()` 传下去（`request_builder.py`），两个出口读它。

### 3.2 `ai_coding/tools/finish_research_tool.py` — 出口本体

门序（先门后解析）：

1. `ctx.mode != "plan"` → `not_in_plan`（含 `ctx is None` 失败关闭）。
2. `ctx.conclusion.exit` 非空 → `already_concluded`（另一个出口赢了）。
3. `ctx.task.armed`：`armed_profile == "plan"` → `plan_already_approved`；否则 → `task_busy`。
4. `summary` 空 → `missing_summary`。
5. 成功：`conclusion.exit = "finish_research"`，返回 `ToolResult(success=True, output=summary, data={"research_finished": True})`。

**输出即结论**：tool result 的文本就是给用户看的那段话（也进模型历史）。

### 3.3 `ai_coding/tools/present_plan_tool.py` — 互斥的另一半

- 在 `armed` 两道门之后加 `already_concluded` 门（顺序重要：已批准时仍报 `plan_already_approved`，今天的语义不变）。
- 批准成功（`:284` 附近）与评审搁置（`:255` 附近）各置 `conclusion.exit = "present_plan"`。
- `keep` 仍是 tool error，**不置**出口——模型可以在同一回合改了再交。

### 3.4 `ai_coding/core/agent_loop.py` — 回合在批次末结束

在评审搁置分支（`:943-967`）之后加同形分支：

```python
if any(
    item.call.name == "finish_research" and item.result.success
    for item in outcomes
):
    outcome = TurnOutcome(..., reason="research finished", note=PLAN_RESEARCH_NOTE)
    break
```

### 3.5 `ai_coding/core/turn_policy.py` — 守卫的话术

`PLAN_NUDGE` 从「必须交计划」改成「必须给一个出口」（交计划 / 交结论二选一）；`PLAN_UNSUBMITTED_NOTE` 补上 `finish_research`。

### 3.6 prompt — 两个出口要看得见

- `ai/ai/prompt.py` 的 `<plan-mode>` 段：加一句「请求不需要实现时用 `finish_research(summary)` 收尾」。
- `ai_coding/core/plan_runner.py` 的 `PLAN_PROMPT`：同样加一句（规划轮用）。

---

## 4. 数据流

```
规划回合
  model ──► finish_research(summary)
              │  tool: 门 → conclusion.exit = "finish_research" → output = summary
              ▼
           agent_loop._run_tools → outcomes
              ▼  （批次后 break）
           TurnOutcome(COMPLETED, reason="research finished", note=PLAN_RESEARCH_NOTE)
              ▼
           转录里多一条 tool result（结论）——  没有评审卡、没有 task 变更、没有 run
```

对照（有计划的那条路）：`present_plan` → 评审卡 → 批准 → `task.armed=True` → `note_plan_approved` → 回合末由 `plan_runner` 派发执行。**研究出口一条都不碰。**

---

## 5. RIPR 用例总表

### 5.1 后端

| # | 用例 | 断言 |
|---|---|---|
| R1 | `tests/test_finish_research_tool.py::test_it_refuses_outside_plan_mode` | `not_in_plan`（含 `ctx=None`） |
| R2 | `::test_it_refuses_an_empty_summary` | `missing_summary` |
| R3 | `::test_it_closes_the_turn_with_the_conclusion_itself` | `success`；`output == summary`；`data={"research_finished": True}`；**task 未 arm、无 items** |
| R4 | `::test_it_refuses_when_the_turn_already_concluded` | `already_concluded` |
| R5 | `::test_it_refuses_while_a_plan_is_approved` | `plan_already_approved` |
| R6 | `::test_it_refuses_while_a_goal_is_armed` | `task_busy` |
| R7 | `::test_a_successful_conclusion_marks_the_turn_concluded` | `conclusion.exit == "finish_research"` |
| R8 | `tests/test_finish_research_turn.py::test_a_conclusion_ends_the_turn_at_the_batch` | 引擎只被问一次；`reason == "research finished"`；`note == PLAN_RESEARCH_NOTE`；**守卫没说过话** |
| R9 | `::test_a_conclusion_arms_nothing` | `task.armed is False`；`task.items == []` |
| R10 | `::test_a_refused_conclusion_leaves_the_turn_to_the_guard` | 失败的出口不算出口：守卫仍 nudge 一次，末了仍 `未提交计划` |

### 5.2 前端

| # | 用例 | 断言 |
|---|---|---|
| F1 | `frontend/src/util/toolmeta.ts` | `already_concluded` / `missing_summary` / `plan_already_approved` 有中文标签，不再显示「未知原因」 |

### 5.3 真机

| # | 场景 | 期望 | 结果（2026-10-21，qwen3.7-flash） |
|---|---|---|---|
| L-1 | plan 模式下一个**研究型**请求（不改代码） | 模型走 `finish_research`，回合结束，**无评审卡、无 plan.receipt、task 未 arm** | ✅ PASS（`scripts/n1_live_check.py`：`finish_research=1 present_plan=0 reviews=0 items=0 armed=False`） |
| L-2 | plan 模式下一个**实现型**请求 | 模型仍走 `present_plan`（出口二选一没把老路堵死） | ✅ PASS（`scripts/r19_acceptance.py --only plan_mode`：`items=1 after_run=('normal', False) receipts=1`） |

---

## 6. 实施顺序与提交切片

| 切片 | 内容 | 提交 |
|---|---|---|
| 1 | `TurnConclusion` + `FinishResearchTool` + 注册 + 互斥门 + 回合末结束 + 话术 + prompt | 一提交（feat） |
| 2 | 状态看板回填（`00`） | 随切片 1 或单独 docs 提交 |

---

## 7. 自核

- [x] 执行层不醒：研究出口**不写 task、不 arm、不落执行计划**（R3、R9）。
- [x] 人要批准才执行：研究出口不批准任何东西。
- [x] 回合内单出口：两个出口通过 `TurnConclusion` 互斥（R4、R7）。
- [x] 门禁：`python -m pytest -q` 全量 EXIT=0（含 `tests/test_mcp_lifecycle.py::test_the_pause_grows_and_stays_under_the_cap` 在改动前的干净树上同样失败的那条，本次全量跑为绿）；`ruff check ai_coding tests` 通过；`mypy ai_coding` 144 文件通过；前端 `bun test` 409 pass / 0 fail。
- [x] 真机：L-1 / L-2 均 PASS（见 §5.3）。
- [x] 迁移：`tests/test_prompt.py` 的 `PLAN_SEGMENT` 常量随 `<plan-mode>` 段改写（sanctioned migration：行为按设计改变，钉住的是「逐字 + 位置」）。

---

## 8. 实施记录（2026-10-21）

| 文件 | 改动 |
|---|---|
| `ai_coding/tools/base.py` | `TurnConclusion` + `ToolContext.conclusion` |
| `ai_coding/tools/finish_research_tool.py` | 新文件：研究出口 |
| `ai_coding/tools/present_plan_tool.py` | `already_concluded` 门 + 两处置出口 |
| `ai_coding/tools/builder.py` | 注册（主会话；子代理不注册） |
| `ai_coding/core/request_builder.py` | `tool_context()` 透传 `conclusion` |
| `ai_coding/core/agent_loop.py` | 每回合建持有者 → 传 ctx；批次末 `break`（研究出口） |
| `ai_coding/core/turn_policy.py` | `PLAN_NUDGE` / `PLAN_UNSUBMITTED_NOTE` / `PLAN_RESEARCH_NOTE` |
| `ai_coding/ai/prompt.py`、`ai_coding/core/plan_runner.py` | 两处 prompt 加第二出口 |
| `ai_coding/security/permission_gate.py` | `finish_research` 进白名单（不ask谁） |
| `frontend/src/util/toolmeta.ts` | 三个新错误码的中文标签 |
| `tests/test_finish_research_tool.py`、`tests/test_finish_research_turn.py` | 新增 10 个用例 |
| `tests/test_prompt.py` | `PLAN_SEGMENT` 常量随设计更新 |
| `scripts/n1_live_check.py` | 新增：N1 真机脚本（研究出口） |

**真机结果（2026-10-21，qwen3.7-flash）**：

- L-1 研究型请求 → 模型走 `finish_research`（1 次），`present_plan` 0 次，评审卡 0 次，task 无条目、未 arm。
- L-2 实现型请求（`scripts/r19_acceptance.py --only plan_mode`）→ 仍走 `present_plan`：1 个条目、批准后执行、1 张收据。

**未做（留给下一批）**：N2（契约归位）、N3（只读变成权限规则，依赖 M1a）、M1a/M1b。

---

## 9. 评审落点的修复（2026-10-21，第二轮）

> 对抗性评审指出原型的三个落点缺陷：**占位时机**（dismiss 也占位，把另一个出口封死）、**`plan_approved` 不在 snapshot**（守卫看不见「已批准」）、**`finish_research` 无条件注册**（build 模式的工具表里有一个永远调不通的工具）。
> 另按用户拍板补上「出口②防滥用」：**prompt 判据 + 工具内兜底**（两者都做）。

### 9.1 占位 = 成功

| 事实 | 落点 |
|---|---|
| `present_plan` 的占位 = **人批准**；dismiss **不占位**（只关掉评审本身） | `present_plan_tool.py`：dismiss 分支改置 `conclusion.dismissed`，批准分支仍置 `conclusion.exit` |
| dismiss 后同一回合的 `finish_research` **仍能成功** | 新用例 `test_a_dismissed_review_does_not_eat_the_research_conclusion`（Red → Green） |
| `finish_research` 成功后同批的 `present_plan` 返回 `already_concluded`，**不 arm、不改 task** | 新用例 `test_the_plan_after_a_conclusion_is_refused_and_arms_nothing` |
| 评审不被问第二次（dismiss 只关评审，不关另一个出口） | `TurnConclusion.dismissed` + `present_plan` 的第二道门（错误码仍用 `already_concluded`） |
| 成功的出口拥有回合的终局说明 | `agent_loop.py`：研究出口的分支挪到 dismissal 分支**之前**（同一批里既有 dismissal 又有结论时，note 说「结论已给出」） |

**两个终局 note 不矛盾**（评审要求核对）：

- 已批准 → 批准即 arm，回合照常收尾（note 为空），执行层接管；研究出口此时已被 `already_concluded` 挡住。
- 研究已完成 → `PLAN_RESEARCH_NOTE`（「结论已给出，未提交计划、未执行」）；此时 task 未 arm、无条目。
- 两者能同时成立的前提是「同批里结论先成功、计划随后被批准」——**互斥门在提问之前就拒了它**（`present_plan` 的 `already_concluded` 门在评审之前），所以状态与 note 不可能矛盾。

### 9.2 补齐的落点

| 缺陷 | 落点 |
|---|---|
| `TurnSnapshot` 里没有 `plan_approved`，守卫看不见「已批准」 | `turn_policy.py` 加字段 + `PlanSubmissionGuard` 改读它；`turn_books.py` 的 `snapshot()` 带上 `self.plan_approved` |
| build 模式的工具表里有永远调不通的 `finish_research` | `BaseTool.plan_only` + `FinishResearchTool.plan_only = True`；`agent_loop.py` 的工具面按姿态取（`_surface_tools` / `_surface_schemas`，按姿态缓存以保请求逐字稳定） |
| 三处 prompt 各说一套 | `ai/prompt.py` 的 `<plan-mode>` 段、`core/plan_runner.py` 的 `PLAN_PROMPT`、`finish_research_tool.py` 的工具描述：同一句判据（"A request that asks for changes (implement, modify, add, fix, refactor) needs a plan, not a conclusion."） |

> **与设计稿的一处偏差（如实记录）**：设计稿写「本批先只在 plan 模式**注册**」。工具注册表是**进程级**的（CLI / server 各建一次，所有会话共用），而姿态是**会话级**且运行时会翻（`/plan`、`/plan off`）——注册时根本不知道姿态。所以落点改成**工具面**（模型看到的工具表）而不是注册表：plan 姿态看得见 `finish_research`，别的姿态看不见；执行期还有工具自己的 mode 门兜底（`not_in_plan`）。真效果与设计稿一致，位置不同。

### 9.3 出口②防滥用（prompt + 工具内兜底）

| 层 | 落点 |
|---|---|
| (b) prompt 判据 | `<plan-mode>` 段与 `PLAN_PROMPT` 各加一句："A request that asks for changes (implement, modify, add, fix, refactor) needs a plan, not a conclusion." |
| (a) 工具内兜底 | `finish_research_tool.py`：读**本回合人类的话** → 命中实现类动词就拒（`implementation_request`），并说清「改走 present_plan」 |

**「本回合人类的话」从哪来**：复用 `core/request_assembly.py` 的 `_extract_user_input` 判据，抽成模块级 `human_utterance(messages)`（工具与装配器共用一份，不另写一套）。两处排除：**synthetic 消息**（harness 自己的话）与**规划指令**（`[plan] …`，B3：它写在用户的语气里，且在规划轮里是**最后**一条 user 消息——不排除它，守卫就会拿 harness 的提示词当用户的话）。

**词表（小、只挡明显的实现类动词）**：

- 中文：`实现 新增 加上 加一个 加个 修改 改成 改一下 改代码 修复 重构 写一个 删除`
- 英文：`implement add fix refactor delete remove`（按**词边界**匹配，所以 `implementation` / `address` / `prefix` 不算）
- **否定不算**：「不要改代码，只要调研」「不要提出实现改动」→ 不算实现意图（否则守卫会把 N1 存在的意义——研究请求出结论——顶回计划）
- **名词不算**：「这个实现」「该实现」→ 实现**方式/复杂度**是名词，不是动词（英文靠词边界天然做到，中文靠代词排除）

**已知误判（词表兜底的本质，08 号稿已认：「判定粗糙（同义/反讽会误判）」）**：

- 假阳：「评估一下给 X **加一个**函数要动哪些地方」——研究请求被当成实现请求（模型会改交一份计划）。
- 假阴：「调研**并**实现 X」——一个从句里既有调研词又有实现词，按词表算实现（拦下）✓，但若将来加「调研词优先」规则就会漏。

真正的解法是 M1a 的规则语言（按 agent 规则集剪裁工具面），不是词表。

### 9.4 真机（2026-10-21，qwen3.7-flash，`scripts/n1_live_check.py`）

两个场景都跑在新场景脚本里（研究 + 实现），产物在 `/tmp/opencode/n1-live-final`（首跑 `/tmp/opencode/n1-live-fix`）：

```
[research] posture=('plan', True) items=0 armed=False reviews=0 finish_research=1 present_plan=0 elapsed=50.6s
PASS [research] the research exit was taken and nothing was planned

[implement] posture=('plan', True) items=1 armed=True reviews=1 finish_research=0 present_plan=1 elapsed=70.5s
PASS [implement] the implementation request got a plan, not a conclusion
```

- **研究型**（`/plan 调研这个仓库的会话是怎么落盘的…不要提出实现改动，也不要写任何文件。`）→ 模型走 `finish_research`（1 次，`data={"research_finished": true}`），`present_plan` 0 次，评审卡 0 张，task 无条目、未 arm。★守卫没有误伤：这句话里有「实现改动」，但它在否定从句里。
- **实现型**（`/plan 给 src/mod1.py 加一个函数，计算两个数的和。`）→ `finish_research` **0 次**，`present_plan` 1 次，评审卡 1 张，批准后 task 2 个条目并 arm。★这次模型**根本没试**研究出口——是 prompt 判据 (b) 起的作用（工具内兜底 (a) 是第二道，单元测试两侧都钉住了）。

### 9.5 本轮的门禁

- `python -m pytest -q` 全量 **EXIT=0**（新增 15 个用例函数，其中两个是参数化：7 + 6 个 case，合计 25 个 case；另有 `tests/test_prompt.py` 的 1 处常量迁移）。
- `ruff check ai_coding tests`：**All checks passed**。
- `mypy ai_coding`：**Success: no issues found in 144 source files**。
- 前端 `bun test`：**409 pass / 0 fail**（只加了 `implementation_request` 的中文标签）。
