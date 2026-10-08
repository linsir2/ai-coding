# 编排层与记忆层重设计纲要（提案）

> 日期：2026-10-08 ｜ 状态：**提案，未实施**
> 证据基座：`2026-10-08-hotel-session-audit.md`（含附录 A 二轮勘误）+ 五路对标调研
> 对标对象：Claude Code 2.1.x / OpenCode v2 / Codex；Letta·mem0·Zep·TencentDB-Agent-Memory·GenericAgent；Hermes / OpenClaw / Cline / Goose / Aider / Cursor

---

## 1. 根因模型：一条主线，六个断面

**主线：我们把「必须发生的行为」交给了 prompt 与模型自律，而 harness 只提供了最薄的保证；状态、约束、成本三者都没有 owner。**

| 断面 | 证据（hotel 会话） | 现有机制的洞 |
|---|---|---|
| ① 计划与执行没有回合边界 | T3 建 12 步计划又连做 3 步，687K（40.9%） | `PLAN_PROMPT` 只是文案；`stop_policy=natural` 下 todo 闸永不触发 |
| ② 约束没有一等公民 | 17 次越界 read/write、6 次假审批、T5/T6/T7 重撞同一堵墙 | 沙箱只活在工具内部；报错不可行动；沙箱约束作为「事实」输掉记忆选举（600 字符按 recency 截断） |
| ③ 状态放错地方 | 步回合 ephemeral 不落盘、usage 存会话累计；hotel 笔记污染 ai-coding 库 | 账目/provenance/scope 三处归属混乱 |
| ④ 压缩只保证「变短」 | T15 历史 91→24 条（L1 snip），当前指令被滑出，it10 推理串台 | 无 pin、无事件、无「完成/挂起」三态 |
| ⑤ 完成定义是自由文本 | verify 命令直查 `app.routes` → FastAPI 源码考古 134K | 验收标准由模型自写自判，无 harness 校验 |
| ⑥ 成本不可见 | extractor 每回合一次主模型调用、步会话开销、压缩都不在同一本账 | 无法测量 = 无法优化 |

---

## 2. 设计原则（从反例翻转）

- **P1 prompt ≠ 机制**：任何「必须」的行为要有代码闸门（状态机/权限态/断言/事件），prompt 只是建议。
- **P2 约束是对象**：约束（项目规则、沙箱边界、硬决策）要有类型、scope、时效和**注入保证**，不能靠自由文本+选举。
- **P3 谁调用谁记账**：一切 LLM 调用（extractor/summarizer/repair）进同一 BudgetLedger 与 trace。
- **P4 压缩=可恢复的有损变换**：锚点（当前指令、活跃约束、工作检查点、计划）**恰好一次**重注入；压缩事件必须落盘。
- **P5 完成定义属于 harness**：黑盒行为验收 + 证据（acceptance/verify），不是模型自称 done。
- **P6 缓存友好是硬不变量**：稳定前缀、append-only、动态只进尾段、指令/规则内容寻址。
- **P7 一责一 owner**：task state 归 TaskState，账目归 BudgetLedger，provenance 归记忆条的来源指针。

---

## 3. 编排层目标架构

### 3.1 一个 owner：TaskRun 状态机

```
idle → planning(禁副作用) → plan_review(一次确认/auto) → executing(step i/N)
     → verifying(黑盒验收) → repairing(≤1 次) → done | failed | aborted
```

- **planning 回合**：以「plan agent 权限态」运行——deny write/edit 与变更类 bash，allow read/search/memory_search/todo_write。规划回合只出计划，**绝不执行**。
- **executing**：plan_runner 每步一个 bounded turn（保留现有设计），step seed 必须携带：goal、当前步、verify、**会话约束集**、**working checkpoint**。
- **verifying**：verify 命令经形态门（见 M8）；失败进 repairing 一次，再失败按错误分类上报。

### 3.2 机制清单（M1–M14）

| # | 问题 | 机制 | 来源 | 落点 |
|---|---|---|---|---|
| M1 | T3 计划+执行同回合 | planning 权限态 + `PLAN_PROMPT` 拆成「规划契约」（只输出计划）；执行统一交给 plan_runner | OpenCode `plugin/plan.ts:33-43`（plan agent deny edit） | `core/plan_runner.py:56-104`、`security/` |
| M2 | 步回合起点重置、靠全量重读恢复 | step seed 固定携带「会话约束集 + working checkpoint（≤1200 字符）」；checkpoint 只记任务态，禁持久事实 | Hermes `working_checkpoint_tool.py:16,59-69` | `core/plan_runner.py:341-394` |
| M3 | 15/15 盲跑、预算边缘还在注入 nudge | 两级预算压力（90%/95%）追加在最后一条 tool result、下轮剥离；预算边缘禁止 ledger/nudge 注入 | Hermes `run_agent.py:6893-6912` | `core/turn_policy.py:342-375`、`agent_loop.py:417-439` |
| M4 | 错误恢复一刀切 | 错误分类 → 5 种 Outcome：zero-output→retry(退避)；partial→continue 提示；overflow→压缩重放；同错连续 N 次→中断上报 | OpenCode `runner/step.ts:32-42`、`runner/retry.ts:42-92` | `ai/openai_engine.py:324-439`、`agent_loop.py:558` |
| M5 | 17 次必败循环无熔断 | 连续失败/连续拒绝计数器，触发即中止回合并上报（不再静默重试） | Codex auto-review 熔断（3 连拒/50 中 10 拒） | `core/dispatch.py`、`core/agent_loop.py` |
| M6 | 审批先于沙箱、报错不可行动 | 顺序改为：能力/沙箱判定 → 审批 → 执行；越界错误给出可行动替代（"use bash heredoc …"）；审批键带 scope | OpenClaw `tool-dispatch.ts:203-232` | `core/dispatch.py:329-344`、`tools/read_tool.py:73-76`、`security/approval.py` |
| M7 | 工作区与任务目标矛盾 | 计划期检测「目标路径不在工作区」→ 拒绝并提示切换/显式授权；不再让模型跑 12 回合才撞墙 | 无（本案例独有教训） | `plan_runner.py` 规划前校验 |
| M8 | verify 可探框架内部 | 验收命令形态门：必须可执行、黑盒、有超时和输出断言；禁止访问 `.venv/`/site-packages；失败不得改验收标准绕过 | 三家 peer 的共识（验收属于 harness） | `plan_runner.py:457-504` |
| M9 | 压缩丢失关键内容 | 压缩锚点三件套：**当前用户指令 verbatim + 活跃约束集 + working checkpoint** 恰好一次重注入；压缩发事件；摘要模板含 WorkState(Completed/Active/Blocked) | Hermes/Cline 摘要模板；OpenCode `compaction.ts:120-152`；CC 锚点重载 | `core/context_compression.py:62-77,342-371`、`turn_report.py` |
| M10 | 大文件整读、跨轮重复读 | 工具结果三层预算：单工具自截 → 超阈值 spill 到 `.ai-coding/outputs/` 留指针 → 单轮聚合超限按体积降序 spill；session 级重复读检测 | Hermes `tool_result_storage.py:1-23,115-225`；CC 压缩后≤5 文件 | `tools/`、`core/agent_loop.py` |
| M11 | 子 agent 回吐不可控 | 完成/失败/取消统一 `<subagent state=…>` 合成消息注入父回合；回吐加上限；失败不打断父 turn | OpenCode `subagent-completion.ts:36-43`（它未截断，是弱点，我们不跟） | `core/subagent*.py` |
| M12 | 账目语义混乱 | BudgetLedger 按回合差分；ephemeral 步回合也记账；extractor/summarizer/repair 入账；压缩/harvest 发 usage 事件 | 本审计 R8/A6 | `core/budget.py`、`turn_report.py:92-114`、`turn_close.py:243-246` |
| M13 | 装配随内容漂移 | 装配不变量：最共享→最不共享排序、tool schema 稳定排序、提醒只追加尾部、断言 prefix_shared_ratio 区间 | OpenCode `context.ts:147-158`、`llm.ts:240-241` | `core/request_assembly.py` |
| M14 | 指令/规则全量重发 | （Phase 3）内容寻址指令 + epoch：指令源 sha256、变更发 diff、压缩时 advanceEpoch 实现「恰好一次重注入」 | OpenCode `instruction-state.ts:139-154`、`instruction-discovery.ts:127-148` | `infra/instructions/`（新） |

### 3.3 现有组件处置

| 组件 | 处置 |
|---|---|
| `plan_runner` / TaskState / acceptance / L1–L4 压缩骨架 / tail blocks | **保留**（方向正确，缺的是上面那把闸） |
| `ledger-gate` | **降级为 trace 观测**（不再作为用户消息注入），观察一个迭代周期后决定删除（D1） |
| `ToolSurfaceWarning` / `PathEscapeGuard`(observe) | 保留观察，工作区不一致改由 M7 在 plan 期拦截（D2） |
| 死配置（`consolidate_every_turns`、`detail_max_chars`） | 接线或删除 |
| `PLAN_PROMPT` 单段 | 拆「规划契约」与「执行 seed」两段（M1） |

---

## 4. 记忆层目标架构

### 4.1 需求回归（三条）

1. **项目约定**（hotel 的 DB 路径、依赖栈）→ 归属应是「项目」而非「workspace 会话」；
2. **会话连续性**（沙箱约束送达 T5–T15）→ 这是**会话级约束**，不是 durable 知识，不该参加全局选举；
3. **任务状态**（「已完成骨架，下一步 schemas」）→ 应进 TaskState/checkpoint，**不进记忆库**。

### 4.2 四层模型（A-lite + C 组合）

| 层 | 内容 | 存储 | 注入 | 生命周期 |
|---|---|---|---|---|
| **L0 约束层** | 项目规则（AGENTS.md 式）、会话约束集、用户偏好 | 项目文件（git 版本化）/ 会话态 / 全局库 | 项目规则进静态前缀；会话约束 pin 进 tail **不参与选举**；偏好按需检索 | 随项目/会话；偏好手动 |
| **L1 durable knowledge** | 项目级事实与硬决策 | `.memory`（project-scoped） | relevance×importance 选举入常驻（≤600 字符） | 双时态失效不删；measure 到超阈值才 evict |
| **L2 working notes** | 会话中间结论 | `.memory`（tier=working） | 仅 `memory_search` | TTL（7 天/会话结束） |
| **L3 task state** | 目标/步骤/进度/checkpoint | TaskState + 会话文件 | task 块 + 压缩后重注入 checkpoint | 随任务 |

### 4.3 写入门（五道机械闸，可测）

1. **EvidenceGate**：必须引用来源（turn/call/文件 hash 或用户原话）；No Execution, No Memory（GA SOP）。
2. **VolatileFilter**：拒绝时间戳/PID/临时路径/「下一步」类进度句——机械正则 + kind 白名单。
3. **ShapeGate**：拒绝 JSON 碎片、空文本、超长（本轮 12 条垃圾的根防）。
4. **ProjectResolver**：由 cwd/repo 根 + 工具目标路径推导 project 归属；跨仓库任务的目标笔记只能进目标仓库的库（或标注 project 后隔离）。
5. **Budget**：importance 参与选举与淘汰（不再是 recency + 永不淘汰）。

> 存量清洗：一次性迁移删除 12 条畸形、给 11 条 hotel 笔记打 `project=hotel` 或移交 hotel/.memory。

### 4.4 读路径

- L0 项目规则：走既有 `ProjectInstructionsLoader` 静态加载（零选举、天然按项目隔离、缓存友好）。
- 会话约束集：按「会话内出现的工具失败/用户明示」收集，pin 进 tail，**预算内永不截断**；同时发送「被裁条目」事件（不再静默）。
- durable 选举：`relevance(当前任务窗口) × importance`（复用 `rank_notes_detailed`），替代纯 recency；`pinned` 语义保留给真·硬约束。
- 检索：`memory_search` 默认 `scope=project`；召回结果 **append 进历史**（前缀缓存永久命中），不再每 iteration 重建 tail。
- 关键词覆盖：sandbox/工作区类约束允许按工具名（read/write）触发，不依赖语义命中。

### 4.5 生命周期与巩固

- durable 矛盾 → `supersede`（保留旧条，`invalid_at`），支持「当时为真」（Zep/Graphiti 双时态）。
- consolidate 触发：**事件驱动**——仅当 harvest 真有变更，且距上次 ≥N 回合或累计变更 ≥M 条；语义合并（paraphrase）只在 embeddings 开启时做（Jaccard 0.80 已被实测否证，最大 0.364）。
- evict：按 importance×hits×recency 分数阈值，而非 cap=200 等到爆。

### 4.6 成本

- extractor 换小模型 + 走 BudgetLedger/trace（P3）；空收割短路：回合无新用户意图、无成功工具证据、无决策关键词时不调用 LLM；harvest 窗口取 delta 全部（当前只取末 12 条而游标整段消费，会静默跳过中间消息）。

### 4.7 推陈出新（N1–N6，Phase 3 备选）

| # | 机制 | 解决的问题 | 来源 |
|---|---|---|---|
| N1 | **睡眠期巩固**：会话结束/空闲时后台 subagent 只做合并/失效/晋升，不接触主路径 | 记忆维护挤占对话、越攒越乱 | Letta sleep-time compute；SCM arXiv:2604.20943 |
| N2 | **记忆 git 化**：`.memory` 版本化，promote/revert 可回滚，可 export/import 为新项目冷启动包 | 污染写入可逆；项目知识可迁移 | Letta MemFS；MemOS MemCube |
| N3 | **skill-as-procedure**：把「验收必须黑盒」「外部路径用 bash」沉淀为带触发条件的程序性记忆（进 skill 系统三级披露） | 一次性把教训变成可复用执行策略，而非躺着的 fact | TencentDB Skill；agent memory survey |
| N4 | **KV-cache 感知分频**：稳定段（项目规则+指令锚点）进系统前缀；动态段（任务/预算/digest）只进尾 | 把 79.7% 命中率推向 >90% | Manus；TencentDB `prepend/appendSystemContext` |
| N5 | **证据图**：每条记忆携带来源指针（turn/tool call/文件 hash）；文件变更时关联记忆标 stale | provenance 可审计、防过时知识 | Graphiti provenance；MemGPT/GA reflection 的引用要求 |
| N6 | **记忆体检进 CI**：畸形率、跨项目率、合并率、选举命中率、harvest 成本，作为回归指标 | 防止设计意图再次「生产失效」 | 本审计（46 条里 14.3% 垃圾 / 0 合并的教训） |

---

## 5. 路线图与验收

### P0 — Hotfix（每项独立可测，天级）

1. M6 审批顺序 + 可行动错误文案；M7 工作区不匹配在规划前拦截。
2. 记忆：ShapeGate/ProjectResolver/五闸最小版 + 存量清洗 + L0 选举改 relevance×importance（让 `5bf4b09d` 必入窗口）。
3. M12 账本：per-turn 差分语义 + ephemeral 记账 + extractor 入账。
4. M9-min：压缩时 pin 当前指令 + 压缩事件。
5. ledger-gate 降级观测（D1）。

### P1 — 编排重构

M1（plan-only 权限态）、M2（checkpoint seed）、M3（两级预算压力）、M4/M5（错误分类+熔断）、M8（验收形态门）。

### P2 — 记忆重构

四层模型落地、会话约束集、双时态、事件驱动 consolidate、M10（spill+重复读检测）、渐进子目录规则提示（Hermes/Goose 式，附 tool result 不动 system prompt）。

### P3 — 前沿

N1–N6，按 D3 的结论取舍排列。

### 验收口径（回归 fixture + dogfood）

- **fixture（对 6aae 的六个失败序列做场景化断言）**：
  1. 目标在工作区外 → planning 期即拒绝（不再出现 17 次必败）；
  2. 连续 read 越界 → 错误含替代方案，且第 2 次触发熔断；
  3. 压缩场景 → 当前指令字符串必须仍在装配结果中；
  4. memory fixture → 沙箱约束必入 L0 且 ≤600 字符；hotel 笔记不出现在 ai-coding 的 L0；
  5. verify 含 `.venv`/`app.routes` 内省 → 形态门拒绝；
  6. 预算 14/15 → 不再注入 nudge、不再盲跑。
- **dogfood**：重跑酒店类任务（`/plan`），目标：**≤60 步、≤800K tokens、0 越界失败、0 串台**（baseline：101 步 / 1.68M / 17 次 / 1 次），用 `trace overview` 与本文档对表。
- **账本**：`trace overview` 的 TOTAL 与 BudgetLedger 一致；extractor/summarizer 消耗可查。

---

## 6. 待拍板决策

| # | 决策 | 选项 | 我的建议 | 代价/理由 |
|---|---|---|---|---|
| D1 | ledger-gate 去留 | 修复 / 降级观测 / 直接砍 | **降级观测，一个周期后砍** | 一次有效干预都没有；条件允许修复但复杂度不值 |
| D2 | bash 沙箱强度 | 保持 observe / enforce PathEscapeGuard | **保持 observe + M6/M7** | 安全边界，需你拍板；bash 是刻意逃生口，M7 才是本案例真正该修的 |
| D3 | 记忆分层选型 | A-lite / B 零常驻 / C 项目策展 | **A-lite + C 组合**；B 作预算敏感开关 | A 修本轮全部失败；C 解决项目归属；B 重开案例 8 的风险 |
| D4 | plan-only 回合 | 接受规划回合不能动文件 / 维持现状 | **接受**（peer 三家一致） | 改变 /plan 交互预期（多一次确认），需你知情 |
| D5 | embeddings 默认档 | 开 / 关 | **维持默认关** | deepseek 端点无 /embeddings；实测 paraphrase 收益边际 |
| D6 | 审批记忆双档（会话精确 / 项目前缀） | 引入 / 不引入 | **暂不引入**（先看 M6/M7 后的审批量） | 安全边界，需你拍板 |

---

## 7. 参考索引

- 本仓库：`2026-10-08-hotel-session-audit.md`（现象+账本）、`source-research/`（历史四份调研）、`2026-10-07-gap-report.md`
- Peers：OpenCode v2 `packages/core/src/{instructions,session,tool,permission}`；Claude Code `code.claude.com/docs/{memory,context-window,permissions,agent-sdk/todo-tracking}`；Codex `codex-rs/core/prompt.md`、`sandboxing/auto-review`
- 记忆系统：Letta（sleep-time compute 博客 / memory blocks 文档）、mem0（arXiv:2504.19413 + mem0.ai/research）、Zep/Graphiti（arXiv:2501.13956）、TencentDB-Agent-Memory（本地 repo，README.md:248-277）、GenericAgent `memory/memory_management_sop.md`
- 学术/工程：MemGPT arXiv:2310.08560、Generative Agents arXiv:2304.03442、LongMemEval arXiv:2410.10813、Manus context engineering 博客、Anthropic effective-context-engineering、Cline auto-compact docs、Goose `context_mgmt/mod.rs`、Aider `repomap.py`、Cursor rules docs、OpenClaw `src/skills/*`
