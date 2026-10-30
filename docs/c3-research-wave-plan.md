# C3 方案：计划内只读调研波（**复研中，暂缓实现**）

> 状态：**复研后本方案的核心前提被推翻，暂缓**。见 §13：计划执行回合里的模型**已经**能
> 自己调用 `parallel_research` 并当场读全文，所以「执行器先跑调研波、再把结果塞进下一波」
> 是冗余且必然有损的。方向待你拍板（§13 末）。
>
> 已确认决策：调研子代理 token **计入计划账本**（不进回合 `TurnBooks`）；单条失败记
> **stalled**（下次可重跑）；调研条目**只由 `present_plan` 声明**；`files` 可选；波分组
> **保持声明顺序**。

## 1. 要解决什么

C2 的 wave 执行器把条目交给模型逐条做回合。一个计划里常有 2–4 个互不依赖的**只读调研**
步骤（入口在哪、测试怎么跑、接口谁在用）。今天每条都要开一个完整回合：模型读文件、发
grep，然后停下验收——成本高、还把这些中间过程写进 wave 会话。

B0 已经有 `ResearchFanOut`：2–4 个只读 `explore` 子代理并发、`task_only`、`readonly`、
结构化 packet、部分成功。C3 把它接到计划执行器里：**一波全是 `kind=research` 的条目时，
执行器直接用 fan-out 跑完，不开模型回合**，把每条 packet 结算成该条目的完成证据，并把
摘要写进 `task.wave_note` 交给下一波。

**非目标**：不做写并行；不做 wave_report / replan；不做前端波次进度；不改 `todo_write`
（模型不能在运行中把新增写任务标成 research 绕开回合验收）。

## 2. 网络调研结论（为什么这样切）

| 来源 | 结论 | 对本方案的落点 |
|---|---|---|
| Claude Code 内置 subagent 文档：`Explore` / `Plan` | 两个都是 read-only（Write/Edit denied）；Explore 为了快和便宜**不加载** CLAUDE.md/git 快照；Plan 在 plan 模式下专门负责把调研输出留在**独立上下文**，主会话保持只读 | 与我们的 `explore` + `task_only` + `readonly` 同构；调研波就是「执行器的 Plan 子代理批次」 |
| 同一文档「Run parallel research / Concurrent subagent limit」 | 官方把「并行调研」列为 subagent 的典型用法，并给并发上限 | 复用 B0 的 `max_concurrent_subagents`，执行器不再开并发通道 |
| opencode Agents：`explore`（read-only，不能改文件）、`task` 权限 | 只读探索子代理 + 按 agent 限制可调用的子代理 | 我们的 `explore` 白名单 + `readonly` 硬开关已经是同一形状 |
| Modern AI Guide / AgentPatterns：orchestrator-worker、fan-out/fan-in、LLM map-reduce | 子代理返回 ~1–2k token 的压缩摘要；**orchestrator 是唯一写者**（single-threaded writes） | 调研波只读、不发写；合并只做确定性的摘要拼接 |
| Claude 多智能体编排 / dynamic workflows | 把「多子代理并发 + 合并」写成编排脚本，而不是让主模型逐轮调度 | 执行器（`WaveRunner`）就是那个编排脚本：research 波由代码调度，不用模型回合 |

## 3. 现有设计与复用（必须复用，不另造）

| 现有件 | 位置 | 复用什么 | 不改什么 |
|---|---|---|---|
| 调研编排层 | `core/research_fanout.py:92-145`（`ResearchFanOut.run`） | 直接调用；`task_only` / `readonly` / `isolate=False` / schema / usage / 包络 | 不写第二套 fan-out |
| 调研包解析 | `core/research_fanout.py:160-204`（`_brief` / `_packet` / 截断标注） | 条目 → `ResearchQuestion`，packet → 摘要/usage | 不改包形状 |
| 子代理运行时 | `core/subagent.py:180-290` | 信号量、超时、usage；执行器只用 fan-out | 不新开 semaphore |
| 工具入口 | `tools/parallel_research_tool.py:51-53` | 它持有 `ResearchFanOut`；执行器通过一个只读访问器复用**同一个**实例 | 不重复构造 runner |
| 波分组 | `core/wave_planner.py:70-99` | 在 `group_waves` 里加「kind 变化即切波」 | 分组仍是提示，不是正确性约束 |
| 波执行 | `core/wave_runner.py:330-395`（`_run_wave`） | 在同一入口分支：整波 research → 调研路径 | 不改写路径的循环 |
| 结算/收据 | `core/wave_runner.py:754-814`（`_record` / `_delivery`） | 复用 `StepDelivery` + 父会话气泡 + `PlanStepReceipt` | 不造新事件 |
| 计划记账 | `core/wave_runner.py:899-934` | `_wave_tokens` / `plan_tokens_used` / 波预算 | 不改回合 `TurnBooks` |
| 计划声明 | `tools/present_plan_tool.py:100-125`、`:387-418` | items 增加 `kind` / `files`；审批写入 `TaskState.add`（已支持这两个参数） | 不改审批流程 |
| 字段载体 | `domain/task.py:62-69`（`files` / `kind`），`:236-260`（`add`） | 已有字段，第一次有生产者 | 不加新字段 |

**发现（必须一起修）**：`config/models.py:476-478` 的注释已经声称「research/verify 条目
总是提前切波」，但 `wave_planner.group_waves` 只看 `is_heavy_verify`。也就是说 C2 的
`kind` 至今没有生产者、也没有消费者——本方案补上生产和消费，注释与代码才一致。

## 4. 数据流

```text
present_plan(items=[{content, verify, depends_on, kind, files}])
  └─ 校验：kind ∈ {write, research}；research 不允许 verify
      └─ 审批 → TaskState.add(..., kind=..., files=...)   （待执行清单）

/plan run → WaveRunner
  └─ group_waves：kind 变化处切波；research 波整波只含 research 条目
      └─ next_item → WaveItem(wave)
          └─ _run_wave：整波 research 且开关打开且 fan-out 可用？
              ├─ 否 → 现有回合路径（C2 行为，不变）
              └─ 是 → _run_research_wave
                    ├─ 取本波可运行条目（依赖满足、未 terminal、本次 run 未尝试）
                    ├─ 每 4 条一组：ResearchQuestion(id=item.id, question=item.content,
                    │                                  scope=item.files)
                    │     ResearchFanOut.run(questions,
                    │         shared_context=goal + 上一波 wave_note（≤1000 字）,
                    │         parent=ParentContext(permissions=父会话规则),
                    │         observer=父 observer, parent_span="plan-wave-<n>")
                    ├─ 每条 packet：
                    │     completed/max_iterations → task.completed + evidence=summary
                    │                                   + StepDelivery(attested=True)
                    │     timeout/error            → 条目保持 open，StepDelivery(stalled)
                    ├─ usage 合并进 report.tokens_used 与 _wave_tokens（波/计划预算）
                    └─ task.wave_note = "#id 调研完成：<summary>"…（≤3000 字）
```

没有 wave 会话、没有模型回合、没有 acceptance 合同：调研的验收就是「子代理返回了
可读结论」，由执行器结算，与「模型自己声称完成」明确区分（`attested=True` 表示执行器
结算，不是模型自述）。

## 5. 契约

### 5.1 `present_plan.items` 新增字段

```json
{
  "content": "调研：登录入口在哪",
  "kind": "research",            // 可省略，默认 "write"；另一取值 "write"
  "files": ["src/auth/"],        // 可省略；调研范围（仓库相对路径），≤10 项
  "verify": "",                  // research 必须为空
  "depends_on": [0]
}
```

- `kind` 不是 `write`/`research` → `error="invalid_items"`，`item N has a bad 'kind'`。
- `kind=research` 且 `verify` 非空 → `invalid_items`，`item N is research and cannot have a verify`。
  调研是只读的，给不出 shell 证明；让它在审批后再失败是不诚实的。
- `files` 必须是字符串数组、≤10 项、每项非空；否则 `invalid_items`。
- 审批落库：`TaskState.add(content, verify=..., depends_on=..., kind=..., files=...)`
  （`add` 已支持，不新增参数）。
- 审批卡 `review_items` 增加 `kind`，前端把该项的副标题显示为「调研（只读）」而不是
  「verify：无证明命令」。

### 5.2 波分组（`group_waves`）

- 新增规则：`current[-1].kind != item.kind` 时先 `close()` 再 `append`。
- 现有规则（size 上限、重验证切波、同文件回退一格）全部保留。
- 调研波整波只含 `kind=="research"`；写波只含 `write`。交错声明会产生更多波，这是声明
  顺序的代价，不是执行器的错误。

### 5.3 执行器（`WaveRunner._run_research_wave`）

触发条件（全部成立）：
1. `wave.items` 的 `kind` 全是 `research`；
2. `settings.plan_research_wave` 为真（新配置，默认 `True`；用 `getattr(..., True)` 读，
   现有测试的 `SimpleNamespace` 没有这个键）；
3. `loop.tools.get("parallel_research")` 存在且暴露 `fanout`（主栈注册；子栈没有）。

任一条不成立 → 走现有回合路径，行为与 C2 完全一致（研究条目就是没有 verify 的普通条目）。
legacy 执行器（`plan_orchestrator=legacy`）看不到 kind，research 条目同样按普通回合跑——这是
保留的回退路径，不改。

行为：
- **不发回合**：不调用 `loop.process_input`，`report.turns_used` 不增加；`report.waves += 1`。
- **循环取 ready**：每轮从本波取「未 terminal、未 blocked、本次 run 未尝试、依赖已满足」的
  条目，最多 `MAX_QUESTIONS`(4) 条；结算后重新取，直到没有 ready。这样同波内的
  `depends_on`（调研 A 之后才调研 B）也能正确分批，而不是一次性把 B 也发出去。
- 条目 → 问题：`question=item.content`，`scope=item.files`。
- 共享背景：`task.goal` + 上一波 `task.wave_note`，截到 `SHARED_CONTEXT_MAX`(1000) 字。
- 父上下文：`ParentContext(permissions=父会话 permissions)`；子代理 `task_only`，不需要 messages。
- 权限/只读：沿用 B0（`explore` 规则 + `readonly` 会话 + dispatch 结构拒绝 + `isolate=False`）。
- 并发：runner 的 `max_concurrent_subagents` 是唯一闸门；执行器不再开并发。
- 组间检查（每轮开跑前）：`task.armed`（Esc 由 `_interrupt_running_turns` 先 disarm，这里据此停，
  **不读 `loop.cancel`**——那是批次级事件、会被回合清空，且可能残留导致新 run 被误停）、
  `_episode_budget_spent`、`_wave_budget_spent`、`_wave_time_spent`；命中则 `report.stopped` 并停止本波，
  未跑条目保持 open。组内取消依赖外层 turn future 的取消传播（Esc 也会取消它）。
- 结算：
  - `packet.status in SUCCESS_STATUSES` → `task.update(id, status="completed", evidence=summary)`，
    `StepDelivery(status="completed", attested=True, usage=packet.usage,
    summary=f"{item.content}：{_tail(packet.summary)}", turns=(0,0))`，写父会话气泡 + `PlanStepReceipt`
    （复用 `_record`）。
  - 否则 → 条目保持 open（`pending`/`in_progress` 不动），
    `StepDelivery(status="stalled", summary=f"{item.content}：{packet.error or packet.status}")`；
    本次 run 记入 `_attempted_items`，不会在同一次 run 内重试；下一次 run 可重跑。
  - 不调用 `_skip_dependents`：stalled 不是 blocked，依赖方继续等待（避免网络抖动跳过整条链）。
- 记账：packet usage 合并进 `report.tokens_used` 与 `self._wave_tokens`；
  波末与写路径同样做 `task.wave_note = ...`、`task.plan_tokens_used += _wave_tokens`、
  `_persist(parent)`、`_emit_task(task)`。**不碰 `TurnBooks.tokens_spent`**。
- 交接：`task.wave_note = "调研：#1 <summary>；#2 <summary>"`，截到
  `WAVE_NOTE_MAX_CHARS`（`domain/task.py:44`，**900**；不是自定 3000）。`TaskState.render`
  与 `to_dict` 已经按 900 截断/往返，不新增常量。
- **已知边界**：`wave_note` 每次波末被整体覆盖，所以调研摘要只保证传给**下一波**；再往后的波
  靠的是条目证据（`task.update(evidence=...)`，计划卡可见），不靠 note。要不要让后续每一波都
  携带已完成调研摘要，见 §12 的问题 1。
- 断点：research 条目完成后同样有 `StepDelivery`，`_backfill_receipts` 的既有逻辑覆盖
  （无 verify → 记一条 `attested=False` 的补验收据）。

### 5.4 配置

```python
# config/models.py，紧挨 plan_* 块
plan_research_wave: bool = True   # 关掉时 research 条目按普通回合执行（C2 行为）
```

### 5.5 复用契约（避免重复构造）

`ParallelResearchTool` 新增一个只读访问器：

```python
@property
def fanout(self) -> ResearchFanOut:
    """The C3 executor reuses this instance; it must not build a second runner path."""
    return self._fanout
```

`WaveRunner` 用鸭子类型取：

```python
tool = getattr(getattr(self._loop, "tools", None), "get", lambda _n: None)("parallel_research")
fanout = getattr(tool, "fanout", None)
```

不 `import tools`（core 不反向依赖 tools），测试里用一个假对象即可。

## 6. 逐文件改动（最小集）

| # | 文件 | 改动 | 为什么必须 |
|---|---|---|---|
| 1 | `ai_coding/tools/present_plan_tool.py` | schema 加 `kind`/`files`；`_parse_items` 改用模块内 `_Draft(NamedTuple)`（content/verify/depends_on/kind/files），4 个解包点同步；`research` 禁 verify；`files` ≤10×200 且与正文走同一个 `outside_workspace_paths` 边界；`review_items` 仅在 kind!=write 时带 kind；审批 `add(..., kind=, files=)` | 唯一的计划声明入口 |
| 2 | `ai_coding/config/models.py` | 加 `plan_research_wave: bool = True` | 开关/回退 |
| 3 | `ai_coding/core/wave_planner.py` | `group_waves` 在 kind 变化处切波 | 调研波同质 |
| 4 | `ai_coding/core/research_fanout.py` | 导出 `MAX_QUESTIONS`(4)、`SHARED_CONTEXT_MAX`(1000)、`MAX_SCOPE_ITEMS`(10)、`MAX_SCOPE_ITEM_CHARS`(200)；工具与 present_plan 改用它们 | 执行器、工具、计划声明共用同一上限，不复制常量 |
| 5 | `ai_coding/tools/parallel_research_tool.py` | 改用共享常量；新增 `fanout` 属性 | 复用同一 fan-out |
| 6 | `ai_coding/core/wave_runner.py` | `_run_wave` 开头分支 + `_run_research_wave` + `_research_note` + `_research_context` + `_research_fanout`；调研波的收尾（note/记账/persist/emit）与写路径对齐 | 编排落点 |
| 7 | `ai_coding/domain/task.py` | 把 `kind` 注释从 `write|research|verify` 收敛为 `write|research` | `verify` 从未有生产者，注释失实 |
| 8 | `ai_coding/frontend/src/state/session.ts` | `PlanItem.kind?: string`；`plan.updated` reducer 读 `kind`（后端 `to_dict()` 已带） | 计划卡要能显示调研 |
| 9 | `ai_coding/frontend/src/util/ask.ts` | `PlanItemData.kind?`；`planItems` 读 `kind`；新增 `planItemSubtitle(item)`（research → `调研（只读）`，否则 verify/依赖行） | 审批卡与历史卡本来共用这个读取器，放这里不新建模块 |
| 10 | `ai_coding/frontend/src/component/{plan-review,message,toolcard}.tsx` | 改用 `planItemSubtitle` | 审批卡/计划卡/工具卡一致 |
| 11 | `tests/test_present_plan_tool.py`（既有） | 追加 kind/files/research+verify 用例 | RIPR 红相 |
| 12 | `tests/test_wave_planner.py`（既有） | 追加 kind 切波用例 | RIPR |
| 13 | `tests/test_wave_runner.py`（既有） | 追加调研波用例（假 loop + 假 fanout） | RIPR 主体 |
| 14 | `tests/test_plan_research_wave.py`（新） | 端到端：research 波不发回合、结算/停滞/预算/开关 | 集成 |
| 15 | `docs/c3-research-wave-plan.md` | 本文 | 文档 |

**不新增**：新 agent、新工具、新事件、新 semaphore、新 worktree、`todo_write` 的 kind。

## 7. RIPR

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 先写 11–14 的测试 | `pytest` 红；失败指向缺失字段/分支 |
| **I** | §6 的 7 个产品文件 + 前端 | 新测试转绿 |
| **P** | 三个后端测试文件 + 全量 | 0 failed；`ruff`/`mypy` 干净 |
| **P** | 前端 `tsc` + `bun test` | 0 failed |
| **P** | **突变 A**：`group_waves` 删 kind 切波 | U2 红（写/调研混波） |
| **P** | **突变 B**：`present_plan` 不校验 research+verify | U1 红 |
| **P** | **突变 C**：`_run_wave` 删 research 分支 | U3 红（research 波开了回合） |
| **P** | **突变 D**：失败 packet 映射成 completed | U5 红 |
| **P** | **突变 E**：packet usage 不计入 `_wave_tokens` | U6 红 |
| **P** | **突变 F**：`plan_research_wave=False` 仍走 fan-out | U7 红 |
| **R** | `git status --porcelain` 白名单 | 仅 §6 文件 + 测试/文档 |

### 7.1 用例

| # | 用例 | 期望 |
|---|---|---|
| U1 | `present_plan` items：kind=research + verify → `invalid_items`；kind=bogus → `invalid_items`；files 11 项 → `invalid_items`；合法 research → 审批后 task 条目 `kind=="research"`、`files` 落库 | 契约 |
| U2 | `group_waves([write, research, research, write])` → 3 波，且每波 kind 单一 | 分组 |
| U3 | 2 条 research 波 + 假 fanout：`loop.process_input` **0 次**；fanout 收到 2 个问题（id/question/scope）；两条 completed；`report.waves==1`、`turns_used==0` | 无回合 |
| U4 | 同一波 5 条 research：fanout 被调用 2 次（4+1） | 分批 |
| U5 | 2 条 research，1 个 packet `timeout`：completed 1 条、stalled 1 条；stalled 条目仍 open；依赖它的写条目不 skipped | 失败语义 |
| U6 | packet usage `{total_tokens:100}` ×2 → `report.tokens_used.total_tokens==200`；波末 `task.plan_tokens_used==200` | 记账 |
| U7 | `plan_research_wave=False`：research 条目走回合（`process_input` 1 次/条） | 开关 |
| U8 | 没有 `parallel_research` 工具（裸 loop）：走回合，不崩 | 降级 |
| U9 | 波内预算已耗尽：fanout 不调用，`report.stopped` 是预算原因，条目保持 open | 预算 |
| U10 | `wave_note` 含每条调研摘要，且 ≤`WAVE_NOTE_MAX_CHARS`(900)；下一条写波 seed 能看到 | 交接 |
| U11 | 前端 `planItemSubtitle({kind:"research", verify:""})` → 含「调研」；write 条目仍是 verify 行 | UI |

## 8. 自检（可能翻车的点）

1. **子代理看不到父历史**：`task_only`；缓解是 `goal + wave_note` 的 shared context 与
   `files` scope。若实测质量差，改成给每条 `files` 直接读入内容（会增 token）。
2. **shared context 过长**：截 1000 字，和工具 `context` 同限。
3. **调研结果太粗**：每条 packet summary 600 字、findings 5 条；`wave_note` 截 3000 字，
   超出的部分只剩 ID。完整摘要仍在父会话气泡与收据里，人可回看。
4. **研究条目被声明成写**：执行器按 kind 分波，写波行为不变；模型若把研究写成 write，
   只是回到 C2 的回合路径——不会破坏正确性，只多花 token。
5. **执行器与模型双跑**：research 波不发起回合，模型没有机会同时跑同一条目。
6. **并发峰值**：≤4 子代理 × AgentLoop；`max_concurrent_subagents` 唯一闸门。
7. **非 git / 无工具**：B0 已是 `isolate=False`，不依赖 git；工具缺失直接降级到回合。
8. **预算口径变化**：调研花费计进 `plan_tokens_used`/波预算，会缩短后续回合的
   `max_turn_tokens`；这是用户拍板的「计划账本反映真实花费」，不是回合预算。

## 9. 成功后的样子

- 计划里写：「1. 调研登录入口；2. 调研权限校验；3. 修复 X；4. 跑测试」；
- 波 1 = 调研 #1–#2：**0 个模型回合**，两个 `explore` 子代理并发 20–60s 返回；
- 审批卡第 1、2 行显示「调研（只读）」；计划卡同样；
- 波 1 的 `wave_note` 把两条摘要交给波 2；
- 某条调研超时 → 该条目 `stalled`、不跳过依赖；下一次 run 重跑；
- 报告：`完成 2 步 · 用了 0 回合 · 1 波 / 27k tokens`（token 来自 packet usage）。

## 10. 收敛检查（按分析清单逐条）

| 清单项 | 落点 |
|---|---|
| 现有设计与复用契约 | §3（fan-out / 工具 / 波分组 / 收据 / 记账 / 计划声明 / 字段）+ §11.1（逐处核对过的事实） |
| 对代码的影响与执行范围 | §6（7 个产品文件 + 3 前端 + 3 既有测试 + 1 新测试） |
| 每个文件怎么实现 | §5 契约 + §6 表格 |
| 数据流影响 | §4 + §11.2（present_plan → task → group_waves → _run_research_wave → task/wave_note） |
| 成功后的效果 | §9 + §11.3 |
| 模糊项/风险 | §8 + §12（四点待拍板） |
| 最小化/第一性 | 不新增 agent/tool/event/semaphore/worktree/字段；复用同一个 fan-out 实例 |
| 契约与函数作用 | §5.1–5.5；`fanout` 访问器只有一个消费者（C3），说清了 |
| TDD/RIPR | §7（R/I/P/R + 6 条突变 + 11 个用例） |
| 架构图 | §4、§11.2 |
| 最终模样 | §9 |

## 11. 对现有代码的影响与执行范围（复核后）

### 11.1 现有设计（逐处核对过的事实）

| 位置 | 事实 | 对本方案的含义 |
|---|---|---|
| `domain/task.py:62-69` | `TodoItem.files` / `kind` 已存在，`add()` 已接受两者 | 不需要新字段；只是第一次有生产者 |
| `domain/task.py:92-93` | `to_dict` 仅在 `kind != "write"` 时写 `kind` | 前端可把 `kind` 当可选键，**不破坏现有 `toEqual` 断言** |
| `domain/task.py:44` | `WAVE_NOTE_MAX_CHARS = 900`，`render`/`to_dict`/`load` 都按它 | 波交接没有 3000 的余地；复用 900 |
| `core/wave_planner.py:70-99` | 目前只按 size / 重验证 / 同文件回退切波 | `config/models.py:476-478` 关于 research 提前切波的注释**与代码不符**；本方案补上 |
| `core/wave_runner.py:330-395` | `_run_wave` 自己 `report.waves += 1`、重置 `_wave_tokens`、记录 `started`、`_base_config`，波末写 `wave_note`、累加 `plan_tokens_used`、persist、emit | 调研分支必须把**同一套收尾**做一遍，否则账本/交接会断 |
| `core/wave_runner.py:866-884` | `_next_ready` 跳过 blocked / 已尝试 / 依赖未满足 | 调研波分批直接复用它，不另写 ready 逻辑 |
| `core/wave_runner.py:754-814` | `_record` 做本次 run 去重、写父气泡、发 `PlanStepReceipt` | 调研结算复用，不新增事件 |
| `core/research_fanout.py:92-145` | `ResearchFanOut.run(questions, shared_context, parent, observer, parent_span)`；子代理 `task_only/readonly/isolate=False` | 执行器只构造问题和结算包 |
| `tools/parallel_research_tool.py:51-53` | 工具持有唯一的 `ResearchFanOut` | 执行器通过 `fanout` 属性复用同一实例（不构造 runner） |
| `core/agent_loop.py:351` | `self.tools` 是 `ToolRegistry` | `loop.tools.get("parallel_research")` 可取到工具 |
| `server/handlers.py:848-866` | Esc = `task.disarm()` + `world.cancel.set()` + 取消 turn futures | 组间用 `task.armed` 判停；组内靠 future 取消传播 |
| `tools/present_plan_tool.py:202,233,244,258-261,319-325,371` | `_parse_items` 有三元组，共 4 个解包点（正文/verify 扫描、review、审批、proof） | 改字段必须同步全部 4 处，漏一处就是运行时解包错误 |
| `frontend/src/util/ask.ts:74-114` | `planItems` 同时服务审批卡/历史卡；`PlanItemData` 只有 3 个键 | `kind` 必须可选，否则现有前端 `toEqual` 断言全红 |
| `frontend/src/component/{plan-review,message,toolcard}.tsx` | 三处各自拼同一段 `verify：… · 依赖 …` | 抽 `planItemSubtitle` 放 `ask.ts`，三处共用，写条目的输出字节不变 |
| `tests/test_wave_runner.py:_StubLoop` | 没有 `tools` / `cancel` / `plan_research_wave` 属性 | 读取全部 `getattr` 兜底；写波测试不受影响 |
| `config/models.py:471-496` | `plan_*` 配置块已存在 | 只加一个布尔键；`test_config_loader` 断言的是具体键，不是键集合，不冲突 |

### 11.2 数据流（完整）

```text
present_plan(items=[_Draft(content, verify, depends_on, kind, files)])
  ├─ 校验 kind ∈ {write,research}；research 必须无 verify；files ≤10×200 且在 workspace 内
  ├─ 审批：task.items.clear() → task.add(..., kind=, files=) → armed=plan
  └─ review_items：仅 research 行带 kind（写行载荷与今天逐字节相同）

/plan run
  ├─ WaveRunner.run → TaskDriver
  │    └─ next_item → group_waves(task)
  │         ├─ 原规则：size / 重验证切波 / 同文件回退
  │         └─ 新规则：kind 变化处切波
  ├─ run_round → _run_wave
  │    ├─ 全 research 且开关开且 fanout 可用 → _run_research_wave
  │    │    loop:
  │    │      ① armed? 预算/墙钟? → 停
  │    │      ② ready = 本波最多 4 条（_next_ready 语义）
  │    │      ③ 标记 attempted → ResearchQuestion(content, files)
  │    │      ④ fanout.run(shared_context=goal+wave_note, parent=permissions)
  │    │      ⑤ 每包：成功→task.completed+evidence+receipt；失败→stalled（条目仍 open）
  │    │      ⑥ usage → report.tokens_used / _wave_tokens
  │    │    tail: wave_note=摘要 / plan_tokens_used+= / persist / emit
  │    └─ 否则 → 今天的写路径（一字不改）
  └─ no_item → deadlock / stalled 判定（不变）
```

### 11.3 修改成功后的效果

- 调研波：`report.waves=1`、`turns_used=0`、`tokens_used` 来自 packet；父会话看到每条调研
  的摘要气泡；`task.plan_tokens_used` 增长；下一波 seed 里有「上一波交接：调研：…」。
- 写波：与 C2 完全一致（同一批测试原样通过）。
- 失败：条目仍 open、记 stalled；下一次 `/plan run` 重跑；依赖它的条目不 skipped。
- 关闭开关或没有工具：research 条目按普通回合跑，报告里看不出差别（C2 行为）。
- 前端：调研行显示「调研（只读）」，写行字节不变。

## 12. 复核发现与待拍板

复核里改正的三处（已改入本文）：`wave_note` 用 900 不是 3000；不用 `loop.cancel`（批次级、
会被清空）而是 `task.armed` + future 取消；`_parse_items` 有 4 个解包点且 `files` 要过工作区边界。

仍需拍板的四点见下面的问卷；每一问都对应上面一处具体实现差异，不拍板就不写代码。

## 13. 复研：调研波的前提不成立

你的反馈是「调研是模型觉得需要才发起的，模型读完就行；截断等于没调研」。按这条去核对代码，
结论是：**本方案的「执行器代跑调研波」比现状更差**，理由是三条可验证的事实。

### 13.1 事实一：计划执行回合里，模型已经有这个工具

- `core/wave_runner.py:675-694` `_make_wave_session`：波会话 `agent="build"`、
  `readonly=False`、`mode` 为默认 normal。它不是 plan 会话。
- `tools/builder.py:403-415`：`parallel_research` 与 `subAgent` 一起注册在主栈；波会话用的
  就是主 loop 的同一个 registry。
- `security/permission_gate.py:336-340`：`parallel_research` 在 ALLOW 清单，不弹审批。
- `agents/registry.py:64-67`：拒绝它的 `_PLAN_SOFT_DENY` 只在 **plan 模式**生效；波会话不是。

所以一个没有 verify 的计划条目（比如「调研登录入口」）在 C2 里已经会跑一个回合，模型在
那个回合里可以直接调 `parallel_research`，拿到**合并后的完整报告**（`MERGED_MAX=8000` 字符，
与普通 build 回合完全相同），读完再标 completed。这就是你说的「模型读完不就好了」。

### 13.2 事实二：调研波必须跨会话搬运结果，而搬运通道必然有损

- 波会话是 ephemeral，波与波之间只通过 `_seed` 交接（`core/wave_runner.py:696-710`）：
  目标 + `wave_note`（`WAVE_NOTE_MAX_CHARS=900`，`domain/task.py:44`）+ 本波条目。
- `TaskState._body_lines`（`domain/task.py:461-493`）只渲染条目标记/内容，**不渲染 evidence**；
  所以条目证据对模型不可见。
- 执行器先跑完调研再交接，只能把结果塞进这 900 字，或者新造一个字段/通道；而模型本来在
  自己的回合里读 8000 字是免费的（tool result 直接进上下文）。
- 把 8k 塞进 `wave_note` 会撑爆每次任务渲染；新造 `task.research_report` 则是「同一份结果
  的第二次搬运」，而且后续每一波都要重复注入。**这不是数据流优化，是绕路。**

### 13.3 事实三：真正的缺口在「规划阶段」，不在「执行阶段」

- `_PLAN_SOFT_DENY`（`agents/registry.py:55-72`）把 `parallel_research`、`webfetch`、
  `websearch`、`subagent` 都拒了。所以**模型在写计划时不能调研**，只能串行 read/grep。
- 这正是 Claude Code 的 Plan 子代理做的事：plan 模式里用 read-only 子代理做调研，把结果
  留在独立上下文，主会话保持只读（官方文档：Plan subagent 的 tools 是 read-only，
  Write/Edit denied，purpose 是 "codebase research for planning"）。
- 我们的 `explore` 子代理白名单（`agents/registry.py:47-56`）已经含 `webfetch`/`websearch`，
  且 B0 强制 `readonly=True`。也就是说：**只要不再在 plan 模式拒绝 `parallel_research`，
  规划阶段就自动获得「只读 + 可联网 + 并发」的调研能力**，模型读完整报告后再写计划。
  一行规则、零新运行时、零跨会话搬运。

### 13.4 因此三个方向的真实取舍

| 方向 | 代码量 | 模型能否读到全文 | 风险 |
|---|---|---|---|
| **A 不加代码**：执行回合里模型自己调 `parallel_research`（今天已可用） | 0 | 能（≤8k tool result） | 规划阶段仍然不能调研 |
| **B 放开 plan 模式只读调研**：从 `_PLAN_SOFT_DENY` 去掉 `parallel_research` | ~1 行 + 测试/文档 | 能（同一回合 tool result） | 与之前「plan 模式不委派」的决策相反；子代理可联网（explore 白名单） |
| **C 保留调研波 + 全文注入** | §6 全部 + 新字段/通道 | 靠第二份拷贝，仍要设上限 | 冗余；上下文膨胀；上限仍需截断 |
| **D 改做 `wave_report` / replan** | 另立方案 | 不涉及 | C2 的波交接仍是机械账本 |

**我的建议：B**（如果「规划阶段会调研」是你要的），或 **A**（如果执行阶段够用）。
C 我不建议做——它把同一件事做了两遍，还多了一次有损搬运。

### 13.5 顺带核实的两点（原 §12 问题 2、3 的答案）

- **`attested` 语义**（`domain/plan_delivery.py:35-71`）：它是「谁结算的」——`False` 只用于
  断点补验（`fiat`）。调研若由执行器结算，应 `attested=True`；因为 `ac=[]`，`proof` 派生为
  `on-claim`，报告会写「(含 N 步无证明完成)」。这与**任何没有 verify 的写条目**完全一样
  （C2 现在就这样），不是调研特例。若走方向 A/B，这条问题根本不存在。
- **`files` 可选**：同意；`_EXPLORE_ALLOWED` 的子代理本来就能全仓 glob/grep。

### 13.6 决定（2026-10-30）

按上面三个事实，用户拍板：

- **方向 B**：放开规划阶段的只读调研（从 `_PLAN_SOFT_DENY` 移除 `parallel_research`），
  `subagent` 仍拒；plan 模式的调研子代理允许联网（`explore` 白名单本身含 `webfetch`/`websearch`）。
- **本方案（调研波）放弃**：不实现 `_run_research_wave`、`task.research_report`、
  `present_plan` 的 `kind`/`files`、前端「调研（只读）」标记——它们只服务于一个被放弃的
  数据流。`TodoItem.kind`/`files` 保持 C2 已提交的现状（`files` 已被 `group_waves` 消费）。
- 实现与真机记录见 `docs/b0-parallel-research-plan.md` §13。
