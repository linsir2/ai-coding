# 酒店会话审计报告 —— ai-coding 任务模式实跑复盘

> 审计对象：ai-coding CLI 会话 `6aae97b2182245a080a5cd33930ed05c`
> 时间：2026-10-08 10:12:56 – 11:04:17（+8，墙钟 51m21s）
> 任务：用任务模式（todo_write 计划 + 逐步门控）在 `/home/linsir365/projects/awesome-tool-kit/hotel` 创建一个 FastAPI + SQLite 的小型酒店管理系统
> 规模：15 turns / 101 model steps / 129 tool calls / 85 次审批
> 产出方式：主会话侦察 + 4 个并行 subagent（会话复盘 / 代码审查 / 记忆层审计 / 外部对标）
> 证据：`.ai-coding/traces/6aae97b2182245a080a5cd33930ed05c/turn-*.jsonl`、`sessions/6aae97b2182245a080a5cd33930ed05c.json`、`hotel/` 代码、`.memory`

---

## 0. 结论摘要

1. **会话真实消耗 = 1,682,856 in / 69,901 out（cached 1,340,928，79.7% 命中）**，不是「一千多万」。
   「一千多万」的来源见 §1。
2. 成本高度集中：**T3（建计划+连做 3 步）独吞 40.9%**，T15（审查回合）20.6%，T9+T10（FastAPI 源码考古）8%。
3. 真正的问题不是「每步上下文太大」（均值 16.6K，合理），而是 **101 步太多**：
   越界工具调用失败重试、每步 3–4 次往返、验收标准脆性、压缩后丢指令。
4. 代码交付质量 **6/10**：3 个 HIGH 正确性缺陷（含 1 个 agent 自己发现却未修的 500），测试覆盖边界系统性缺失。
5. 记忆层 **设计意图完备、生产几乎全部失效**：46 条里 12 条 JSON 垃圾、11 条属于另一个项目、0 次合并、0 次淘汰；
   最关键的一条约束（沙箱 workaround）**写进了库但从未被注入**（§5.3），直接导致后续 3 个回合继续撞同一面墙。

---

## 1. Token 账本与「一千多万」溯源

### 1.1 真实账本（口径：TurnFinished.usage 为会话累计，差分得到每回合）

| Turn | 内容 | steps | Δprompt | Δcompl | avg/step |
|---|---|---|---|---|---|
| 1 | 你好 | 1 | 3,044 | 80 | 3,044 |
| 2 | opencode 编排调研 | 7 | 141,661 | 5,523 | 20,237 |
| **3** | **建 12 步计划 + 做步骤 1–3，MAX_ITERATIONS(15)** | 15 | **687,374** | 9,342 | **45,825** |
| 4 | [ledger-gate] 追问 + models.py | 6 | 36,085 | 2,812 | 6,014 |
| 5–8 | schemas / rooms / guests / bookings | 26 | 178,155 | 12,727 | ~6.9K |
| 9+10 | main.py + FastAPI `_IncludedRouter` 考古→降版本 | 15 | 134,257 | 15,056 | ~9K |
| 11 | tests（10 passed） | 7 | 79,382 | 8,327 | 11,340 |
| 12 | stream 中断（ERROR，无重试补跑） | 2 | 8,509 | 287 | 4,254 |
| 13–14 | README + 冒烟 | 10 | 67,944 | 4,118 | ~6.8K |
| **15** | **审查（发现删房 500 但未修）** | 12 | **346,445** | 11,629 | **28,870** |
| 合计 | | 101 | **1,682,856** | **69,901** | 16,662 |

- 缓存：1,340,928 / 1,682,856 = **79.7%**；尾块（memory/task/digest/budget）在不可缓存区。
- `history_chars` 增长：T3 it1=78,806 → it15=109,177；T15 it5=140,104 → it6=19,380（静默压缩）。
- **无任何压缩事件落盘**（事件全集：IterationStarted / MemoryInjected / ContextAssembled / ModelStepFinished / AssistantMessage / ToolStarted / ToolFinished / ToolApprovalRequested / LedgerUpdated / TaskUpdated / MemoryHarvested / TurnFinished）。

### 1.2 「一千多万」溯源

| 数字 | 出处 | 说明 |
|---|---|---|
| 1,682,856 | 本会话 `summary.json` / `sessions/6aae….json` | 酒店会话真实值（≈168 万） |
| **12,331,156** | `trace overview` 的 **TOTAL 行**（全量 session summary 求和） | **≈1233 万**，最可能的「一千多万」来源；其中 10-05 一天占 10,407,006 |
| 10,278,700 | 会话 `5ce5b8fb…`（2026-10-05，编排层调研） | 单一会话，独自一千万 |
| 27,509,025 | 本 OpenCode 会话（行业务不相关） | 一周累计 input |

> 结论：酒店会话没有千万级消耗；如果用户看到的展示不是以上三处，需要具体出处再溯源。

---

## 2. 逐回合时间线（含驱动方式）

| T | 用户/驱动 | 实际动作 | 结局 |
|---|---|---|---|
| 1 | 「你好」 | 回复 | COMPLETED |
| 2 | 「看看 opencode 编排层」 | memory_search + bash×9 + read | COMPLETED |
| 3 | harness 任务模板（"Create a detailed implementation plan for: …然后开始实现"） | 探索→todo_write×12 建计划→步骤 1–3（骨架/venv/database.py）；write 失败改 heredoc | **MAX_ITERATIONS(15)** |
| 4 | `[ledger-gate]` 注入消息 | 答复沙箱根因 + 写 models.py + memory_write | COMPLETED |
| 5–12 | 8 条「只做这一步，不要做别的：…（这是计划步骤 N，做完停下等验收）」 | 逐步写 schemas/rooms/guests/bookings/main/tests；T7 越权提前写 bookings.py；T8 空转一格；T12 stream 中断 | 7×COMPLETED + 1×ERROR(T12) |
| 13–14 | README、端到端冒烟 | heredoc README；uvicorn+curl 冒烟 | COMPLETED |
| 15 | 「你给我审查一下你做成的这个项目」 | cat 全源码 + pytest + 探针 → 发现删房 500 → 试图修复时 read×3 失败 → 放弃 | COMPLETED（缺陷遗留） |

配对错位：user 步骤消息与 assistant 交付总结微秒级同时间戳（如 msg62/63 均 02:29:22.707424）——交付对在下一步起点补写，账本可读性差。

---

## 3. 根因链（为什么会烧这么多）

> 注：本节部分表述在第二轮代码级复核后被修正（任务模式实为 plan_runner、T12 重试语义、turnUsages 成因、压缩 stage 归属），以文末「附录 A：二轮代码级复核勘误」为准。

### R1 任务模板把「规划 + 实现」塞进同一个回合 → T3 = 687K（40.9%）
- 模板原文要求 "plan it out, then start implementing"；模型 15 轮内建完 12 条计划（todo_write 12 次）又连做 3 步；
- 每步把 78K→109K chars 的完整历史重发（45.8K tokens/step）；
- 行至预算尽头才收尾（"budget is running low … let me quickly write database.py"）。
- 对照：`plan_runner` 本来有「每步一个 bounded turn」的设计（此前实测省 ~30%），但本次跑的是手动逐步模式，两者没有闭环。

### R2 工作区边界与任务目标在规划期就矛盾（17 次必败调用 + 6 次假审批）
- 工作区锁定 `/home/linsir365/projects/ai-coding`，交付目标在 `awesome-tool-kit`；
- `read` 11 次 + `write` 6 次全部 `resolves outside workspace`；
- 6 次 write **先弹审批再因沙箱失败**（顺序错误：审批应在沙箱判定之后）；
- 模型转用 bash heredoc 写盘 9 个文件——`write` 沙箱边界实际被 bash 绕过，边界形同虚设，只制造摩擦力。

### R3 记忆写入了约束、却从未送达模型（本报告最重要的新发现）
- 沙箱约束已入库且是 `decision` 级：
  - `5bf4b09d` decision「write/edit 只能写工作区…外部路径必须用 bash」
  - `b0d87e1c` fact「read/write 被沙箱限制…必须改用 bash cat/heredoc」
- 但 T5–T15 实际注入的永远只有 4 条 hotel 项目 decision（`21e53e7c/b4a6f232/1f49c676/14aad296`，共 ~421 字符）——
  常驻索引块 **600 字符上限按「pinned→decision→新→旧」截断**，最要命的那条被挤掉了；
- 结果：T5/T6/T7 每回合第一步继续尝试 `read` 外部路径，失败→改 bash，单个回合成了一笔固定学费。
- 叠加：`read` 错误信息只有 `resolves outside workspace`，**不告诉模型该怎么做**（bash 救不了这次 read……但下次它还会先试 read）。
- 跨轮重复整读无检测（`DuplicateCallGuard` 只在本轮内生效）；database.py 被 cat 5–7 次、models.py 6 次。

### R4 验收标准脆性 → FastAPI 源码考古 134K（8%）
- 计划第 9 步 verify 命令直接断言 `{r.path for r in app.routes}` 包含 `/rooms` 等；
- FastAPI 0.142.4 / Starlette 1.7.0 的 `include_router` 惰性挂载为 `_IncludedRouter`，`route.path` 不存在；
- 模型连读 8 段 site-packages 源码（starlette/fastapi routing）试图找 eager 开关，最后靠 `pip install fastapi==0.115.12` 降级；
- **降级只改了 .venv，requirements.txt 未 pin** → 新环境重装会再次触发。
- 根因：verify 命令由模型自己写，允许探测框架内部；缺少「验收命令必须是黑盒行为断言」的约束。

### R5 审查回合全量重读 + 静默压缩丢指令 → T15 = 346K（20.6%）
- it1 把 81 条消息/111,933 chars 全量发出（52.6K tokens），it1–5 占该回合约 80%；
- it6 历史从 91 条/140,104 chars **骤降到 24 条/19,380 chars**（无事件、无声告警）；
- it10 推理**串台回第 2 回合话题**（"The user is asking about the orchestration layer"）——压缩后当前指令没被钉住；
- 最后一步试图修 500 时又用 `read` 读外部路径（×3 全败），放弃修复。

### R6 逐步模式的回合起点近乎重置
- T5–T14 每回合首个 ContextAssembled 只有 2 条消息、510–1,367 chars；
- 状态全靠注入 task 块（~1.6–2.0K chars）恢复 → 每回合都要重新读文件、重新确认环境；
- 「只做这一步」的门控本身可行，但上下文不继承导致重复劳动（读 database.py 5–7 次即是证据）。

### R7/R8/R9/R10（次级）
- **R7 预算不一致**：max_iterations=15（T1–3、T15）vs 8（T4–14）。
- **R8 会话账本损坏**：`session.turnCount=4`、`turnUsages` 仅 4 条且存的是累计值（末条 1,682,856 标成 turn 4）；15 回合丢 11 条。
- **R9 步骤外溢**：T7 在 guests.py 步骤顺手 `cat > bookings.py`，T8 空转，还可能造成后续重复下达。
- **R10 审批摩擦**：85 次（bash 79 + write 6）；bash 不可记住是刻意的安全设计，但 79 次/51 分钟是可观的人机成本。
- T12：引擎流式中断，3 轮重试后 handoff 续跑，会话中无用户可见提示。

---

## 4. Hotel 代码质量（6/10）

测试：`10 passed, 1 warning in 2.10s`（warning 来自 starlette 内部）。
规模：Python 739 行（含测试 276 行）。结构分层清晰（models/schemas/routers/database）。

| # | 严重度 | 发现 | 证据 | 修法 |
|---|---|---|---|---|
| 1 | **HIGH** | 删有预订的 room/guest → 500（agent 自报缺陷，未修） | `app/routers/rooms.py:84-88`、`guests.py:58-62`；`NOT NULL constraint failed: bookings.room_id` | 删前查关联预订，存在则 409；或 cascade |
| 2 | **HIGH** | `price: Infinity` 入库成功但序列化 500，且此后 `GET /rooms` **永久 500**（毒化读接口） | `schemas.py:16` 只 `ge=0`；DB 实测存 `(1,'INF1',inf,'real')` | `allow_inf_nan=False` + 自定义校验异常 handler |
| 3 | **HIGH** | PUT 显式 `null` → 500 或误导 409 | `guests.py:47-54`（name null→500）、`bookings.py:97-100`（date null→TypeError）、`rooms.py:74-78`（price null→409 "number exists"） | update 对 None 显式 422；409 文案按真实约束分支 |
| 4 | MED | `NaN` 请求 → 500（响应体无法序列化） | `schemas.py`、Starlette JSONResponse | 同 #2 |
| 5 | MED | 房间 status 与预订脱钩；TOCTOU 双订窗口；extra 字段静默吞；status 无枚举；SQLite FK 未开 | 探针 + `bookings.py:39-82`、`schemas.py:17,72`、`database.py:11-14` | 见探针报告 |
| 6 | MED | 测试缺口：父实体删除、PUT null、extra、inf/NaN、status 枚举、PUT 日期非法全未覆盖 | `tests/test_api.py` | 按探针场景补回归 |
| 7 | LOW | README 宣称完整 CRUD 未提 500；依赖未锁版本；测试依赖混在运行依赖 | `README.md`、`requirements.txt` | 修 #1 后补文档 + pin 版本 |

已验证正确：半开区间重叠判定、跨日衔接、422/409/404 语义、分页参数校验、房间号唯一双保险、临时 DB 测试夹具。

---

## 5. 记忆层审计

### 5.1 设计（现状）

- **写**：回合末 `harvest` → 末 12 条消息 + `<known-memory>` 顶 5 条 → LLM extractor（同主模型，temp 0.2，max_tokens 400，输入截断 40K 字符）→ JSON 候选 → `_reconcile`（ADD/UPDATE/SUPERSEDE/NOOP）→ `evict(cap=200)`。
- **读①**：常驻尾块（pinned/decision，**budget 600 字符**，pinned→decision→新→旧）挂在 `[system, history, memory, task, hook, digest, budget]`，**每个 iteration 重发**。
- **读②**：`memory_search` 工具（BM25 + coverage/band 双门 + 可选 embeddings），按需。
- **生命周期**：dedupe_key 精确去重；同 kind+scene Jaccard≥0.80 近似合并；仅 open_thread 30 天 TTL；pinned/decision 免淘汰。

### 5.2 实测数据（`.memory`，46 条，32.6 KB）

| 维度 | 数值 |
|---|---|
| kind | fact 33 / decision 9 / preference 2 / open_thread 2 |
| 文本总量 | 3,432 字符（均值 74.6） |
| **畸形条目** | **12 条 / 491 字符（14.3%）**：11 条是 JSON 字段碎片（`"text": "…`、`"kind": "decision",`）+ 1 条 `[]`，全部来自同一旧 session |
| 跨项目污染 | 11 条属于 hotel（另一个仓库） |
| 合并/淘汰 | **0 次**（最大 Jaccard 0.36 < 0.80；无 superseded） |
| 语义重复 | 路径 ×2、用户名 ×2、config 约定 ×3、沙箱边界 ×2、hotel 路径 ×2、hotel 依赖 ×2 |
| 命中 | `hits` 合计仅 6（全库） |

### 5.3 关键失效：约束没送达（对应 R3）

| 项 | 值 |
|---|---|
| 库中沙箱约束 | `5bf4b09d`(decision) / `b0d87e1c`(fact) / `245b6eab`(preference) |
| 实际注入（T5–15） | 仅 4 条 hotel decision（~421 字符） |
| 失效机制 | 600 字符预算按「新→旧」截断，沙箱 decision 被挤掉 |
| 后果 | T5–T15 继续重试 read/write 外部路径，每回合固定学费 |

### 5.4 成本

- 常驻块：本会话共重发 **48,759 字符**（402×4 回合 + 506×11 回合 × iteration 数），约会话 prompt 的 2–3%，且全在不可缓存的 20% 里。
- 收割：**每回合 1 次主模型 LLM 调用**（12/15 次返回空），不看结果都照跑；调用绕过 trace/budget，账面不可见。
- 收益：15 次收割 → 10 条新增 + 1 条更新；memory_search 全会话只用 1 次。
- benchmark（`scripts/retrieval_corpus.json`，78 notes/124 queries，embeddings off）：all R@3=0.59 / FP@5=0.65；reachable R@3=0.99；**paraphrase R@3=0/46**（释义检索完全依赖未启用的语义档）；no-answer 4/10。

### 5.5 弱点排序

1. HIGH 任务态/跨项目污染，scope 形同虚设（全 `workspace`，从不按工作目录写）。
2. HIGH 跨会话重复无有效去重（阈值 0.80 形同虚设）。
3. HIGH 存量 JSON 垃圾从未清理，写入端无校验。
4. MED-HIGH 常驻块无相关性门控 + 每 iteration 重发 + 预算截断把关键约束挤掉。
5. MED 无 TTL/状态区分，consolidate 空转，2 个死配置。
6. MED 收割成本不可见（主模型、每回合、不记账）。
7. LOW plan-run 下 provenance 失真；`max_per_turn_injections` 语义混乱。

---

## 6. 外部对标（摘要）

| 机制 | 代表 | 证据/收益 |
|---|---|---|
| 稳定前缀 + append-only，KV-cache 当第一指标 | Manus | cached $0.30 vs $3/MTok（10×）；ai-coding append-only 实测 92–93% 命中 |
| 索引常驻 + 正文按需（progressiveness） | Claude Code（MEMORY.md 索引 ≤25KB，topic 文件 JIT；skill 只发 name/desc） | 常驻成本恒定，细节按需 |
| 压缩后只重注入固定锚点、恰好一次 | Claude Code；Codex 因重复注入被提 issue | 你们压缩后连当前指令都没保住 |
| 禁止易变状态入库（No Execution, No Memory） | GenericAgent SOP | 对应 12 条垃圾 + 任务态污染 |
| 检索 <7K tokens/查询 vs 全文 25K+ | mem0 | 按需检索的量级优势 |
| 步骤级新窗口隔断历史 | ai-coding plan_runner 已有（省 ~30%）、Claude Code todo（3+ 步才建） | 任务模式应走这条路 |
| 指令/技能增量下发（diff，取 min） | OpenCode v2 | `instruction-discovery.ts:127-148` |
| 子 agent 隔离大读，只回吐 1–2K | Anthropic 多 agent 研究/Claude Code | 审查类工作量应进子 agent 窗口 |
| 高时效记忆带有效期、失效不删除 | Zep / Letta | 你们 0 supersede |

---

## 7. 优化清单

### P0（确定收益）
1. **沙箱/工作区治理**：计划期检测目标路径与工作区不一致（切换工作区或拦截）；审批在沙箱判定之后；read/write 越界错误信息给可行动替代（"use bash heredoc …"）。
2. **任务模式拆回合**：计划回合只出计划（或交给 plan_runner）；统一 max_iterations；步骤间上下文有控制的继承（禁止靠全量重读恢复）。
3. **记忆四连**：scope 按工作目录 + 注入按 scope 过滤；写入拒绝 JSON/空文本；一次性清洗 12 条垃圾；学 GA「易变状态禁入库」并落地为机械过滤。
4. **注入改为相关性/意图驱动**：常驻块保留（缓存友好），但「与当前任务强相关的决策/约束」必须能覆盖 600 字符预算的截断（例如任务态块顶置、或按 task 关键词重排）。
5. **收割可见化**：extractor 走 budget/trace + 换小模型 + 空收割短路。

### P1
6. 压缩：pin 当前用户指令 + 压缩事件落盘 + 压缩后校验「指令仍在」。
7. 审查/大读场景：强制 offset/limit 或子 agent 隔离；session 级重复读检测。
8. 验收命令生成约束：禁止探测框架内部，必须黑盒行为断言；hotel requirements 锁版本。
9. hotel 代码修 3 个 HIGH + 补边界测试。

### P2
10. session 账本 bug（turnUsages/turnCount）调查与修复。
11. reasoning 落盘截断 501 字符 → 提升审计能力。
12. CA 估算校准（低估 1.18–1.46×）。
13. 审批体验（bash 79 次）——安全边界决策，需用户拍板。

粗估：P0 全做，同类任务 101 步/1.68M → 50–60 步/600–800K（需 trace overview 实测验收）。

---

## 8. 证据索引

- Trace：`.ai-coding/traces/6aae97b2182245a080a5cd33930ed05c/turn-1..15.jsonl`
- 会话账本：`sessions/6aae97b2182245a080a5cd33930ed05c.json`（messages/task/turnUsages）
- 记忆库：`.memory`
- 代码：`ai_coding/{core,memory,ui,tools,infra,security}/`
- 对标：`docs/orchestration-explore/source-research/`、各 peer 源码（opencode/GenericAgent/Hermes/OpenClaw/TencentDB-Agent-Memory）
- 外部：Claude Code docs、OpenCode v2 source、Anthropic context engineering、Manus context engineering blog、mem0/Letta/Zep

---

## 附录 A：二轮代码级复核勘误（2026-10-08，第二轮）

复核方式：对 `trace 6aae` 的关键结论逐条回到代码求证（file:line 见复核报告）。以下修正原文中的误读与不完整判断：

- **A1 酒店跑的是 TUI + `/plan`（任务模式）**：入口 `cli.py:59-65` 无条件 `run_app`；`textual_app.py:318-347` 的 `_plan_turn` → `plan_runner.plan_and_run`（`plan_runner.py:56-104`）。英文模板 `PLAN_PROMPT` 在 `plan_runner.py:56-65`，`msg21` 即其格式化结果。
- **A2「只做这一步…」不是用户手动驱动**：它是 `STEP_HEAD + _step_prompt`（`plan_runner.py:42,632-636`）在每步完成后经 `_record_on_parent`（`plan_runner.py:534-552`）**写回父会话的展示气泡**（user 气泡 + assistant 交付）。步回合跑在 `ephemeral=True` 的临时 step session（`plan_runner.py:341-366`），这就是「微秒级同时间戳」与「每步回合起点近乎重置」的真正来源（设计使然，非 bug）。
- **A3 步骤 1–3 在 T3 规划回合内完成**：`stepDeliveries` 显示 1–3 为 fiat 回填（`attested=false`），4–12 为正常执行（`attested=true`；步骤 9、11 各 repair 一次）。也就是说规划回合没有 planning-only 闸，模型在 15 轮里顺手做了 3 步。
- **A4 预算 15 vs 8 是两套配置而非不一致 bug**：父回合 `max_tool_iterations=15`（`turn_policy.py:102`）；步回合由 `PlanRunner` 换成 `plan_step_max_iterations=8`（`plan_runner.py:337-339,463-474`）。真缺口是「规划回合无执行纪律」。
- **A5 T12 不是「重试 3 轮后放弃」**：partial output 的流中断按设计**不重试**（防重复输出/半截 tool call，`openai_engine.py:328-331,430-439`）；全会话 0 条 `ProviderRetried`。「3」只是 `max_retries` 默认值。续跑由 plan_runner 的一次 repair（`plan_runner.py:495-504,638-643`）完成——设计正确，但用户不可见。
- **A6 `turnCount=4 / turnUsages=4 条`不是「丢 11 条」**：父会话回合恰好是 T1/T2/T3/T15 共 4 个；步回合在 ephemeral session 不落盘。真正的 bug 是 **usage 值语义**：`BudgetLedger` 跨回合不重置（`turn_report.py:92,112-114`），`record_turn_usage` 存的是**会话累计值**（`turn_close.py:243-246`），末条 1,682,856 标成 turn 4。
- **A7 压缩 stage 归属**：T15 it6 的 91→24 条是 **L1 `snip_middle`**（`max_messages=50, keep_head=3, keep_tail=20` → 3+1+20=24，`context_compression.py:62-77`），非 L4（it5 est=43,648 < 阈值 48,000，L4 未跑）。指令在会话索引 80，被 tail 窗口滑出 → T15 it10 串台到被保留的 head（T2 话题）。根因：装配无「当前指令 pin」+ 压缩无事件。
- **A8 ledger-gate 三个真 bug**：每回合从 `books.failures` 重建（`turn_books.py:186-193`，计数按回合归零）；`resolves` 只有 `U-ac-*` 会填（`ledger.py:76-102`）；T3 在预算边缘（it15）注入，占掉最后一格且未被回答（`agent_loop.py:417-439`）。
- **A9 其他复核确认**：审批先于沙箱判定（`dispatch.py:329-344` vs `read_tool.py:73-76`/`write_tool.py:70-73`）；bash 无文件沙箱且 `PathEscapeGuard` 默认 observe-only 未配置（`guard.py:59-61,303-328`）；harvest 空结果不短路 LLM 调用（`service.py:612-622`）、extractor 绕过 budget/trace（`llm_helpers.py:30-55`）；候选只取 delta 末 12 条而游标整段推进（`service.py:597`、`memory_turn.py:107-108`）。
