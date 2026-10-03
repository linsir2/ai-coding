# R7 待办清单（从 R1–R6 各轮文档与实测中汇总）

> **这是盘点，不是方案。** 收集方式：扫 R1–R6 全部文档里的「明确不做 / 候选 / 顺延 / 已知限制 /
> 决策点」，逐条到**当前代码**核验是否还成立（有些在后续轮次里已经顺手做掉了，本表标为「已解决」
> 并从待办里划掉）。RAG / 向量检索按你的要求**不纳入本轮**，单列在 §4。
>
> 核验命令见 §6；每条都给了实测或代码出处，没有凭印象的条目。

---

## 0. 一句话结论

真正值得进 R7 的是四件事：**① 循环内核又长回去了（795 行 / `process_input` 228 行，比 R5 拆分前还大）；
② 压缩把工具正文清空这件事本身（L2 头尾窗口）——R6 只是补了摘要，没治病根；
③ 人在环的两处收口（审批回调的 deadline 洞 + REPL 提问通道复用同一个读取器）；
④ MCP 的每步成本与审批噪音**。其余是候选池，可挑。

---

## 1. 代码结构债（可测量，越拖越贵）

| # | 事项 | 出处 | 现状核验（今天实测） | 建议 | 规模 |
|---:|---|---|---|---|---|
| 1.1 | **`core/agent_loop.py` 瘦身（第二回合）** | r5-analysis §7/§11.3 承认 654/153 未达标；R6 又加功能 | **795 行**（R5 前是 788），`process_input` **228 行**（R5 后曾 153）。R5 的收益被 R6 吃回去了 | **P0** | 中 |
| 1.2 | 拆法：把回合账本收进一个对象 | R6 遗留（本次实测发现） | `process_input` 里现在有 9 个局部状态：`iterations / nudges / attempted_calls / successful_calls / failed_streak / strategy_nudges / failures / last_text / summaries`，散落在 228 行里 | **P0**（与 1.1 同批） | 小-中 |
| 1.3 | `AgentLoop.__init__` 参数对象化 | D-R5-3（当时**不做**） | 64 行、17 个协作者 | P2 | 小（但要改公开构造 API） |
| 1.4 | R5 的量化目标（≤360 行 / ≤130 行） | r5-analysis §7 | 早已不复存在（795/228），目标数字本身是拍脑袋估的 | P2（重设目标再谈） | —— |

## 2. R6 明确留给 R7 的优化项

| # | 事项 | 出处 | 现状核验 | 建议 | 规模 |
|---:|---|---|---|---|---|
| 2.1 | **L2 占位改「头尾窗口」**（`[Previous: used X]` → 保留头 L/2 + 尾 L/2） | D-R6-13（本次不做） | `context_compression.placeholder_old_results` 仍直接丢弃正文；R6 实测里模型抱怨「这些命令的输出从来没有真正返回给我」 | **P0**（对准病根） | 小-中（会改 `features.md` 的 L2 定义 + `tests/test_context_compression.py:94` 的既有断言） |
| 2.2 | ~~GA 式**模型写的 checkpoint** 替代 harness 派生摘要~~ | D-R6-1（R7 优化项） | **R8a 已做**：`checkpoint_write` 的 note 替掉 digest 行的结果句（docs/r8-analysis.md 3.1） | —— | —— |
| 2.3 | **审批回调的 deadline 洞** | D-R6-9（本次不动） | `core/dispatch.py::_ask` 仍是裸 `await approval.approve(...)`：卡住的审批挂死整回合（R6c 的 `ask_user` 已经用 `asyncio.to_thread` 做对了） | **P0**（小改动、安全收益） | 小 |
| 2.4 | **REPL 提问通道复用 `PromptSession`** | r6-analysis §10.8 / integration_testing R6c 段 | 实测：真 pty 下问题会打印、工具会等待；管道/脚本场景 prompt_toolkit 先占住输入 → 降级为 `no_answer`；真人在终端敲的那一行能否到达工具**未验证** | **P0**（这是 R6c 唯一没验证的路径） | 小-中 |
| 2.5 | ~~`evidence` 用于召回同分择优~~ | D-R6-10（只观测） | **R8b 已做**：同分时破平局 + `memory_search` 的 `evidence` 过滤；合并判定刻意不动（docs/r8-analysis.md 3.2） | —— | —— |
| 2.6 | 自进化 / 自主调度（GA 的 `reflect/scheduler.py` 那层） | R4 §16 与 R6 §14「明确不做」 | 无 | P2（要单独一轮） | 大 |

## 3. 编排 / 工具 / MCP

| # | 事项 | 出处 | 现状核验 | 建议 | 规模 |
|---:|---|---|---|---|---|
| 3.1 | **MCP 远端工具一律 WARN → REPL 每次调用弹审批** | r1-analysis §4.9「后续可给 server 声明 allow 策略」；features.md:143 | 今天又实测到同一类问题（`ask_user` 也是 WARN，一个问题两次提示，已修）；MCP 工具**仍然**每次弹 | **P0**（体验，且已有先例） | 小-中 |
| 3.2 | **MCP 工具 schema 的每步成本**：13 工具 server 让 `tools_chars` 5241 → **10481**（≈翻倍），每步 prompt 都付 | features.md:138（R4 实测，只记录未治理） | 无对策（原生 FC 不能省 schema），但可以「按需注册 / 分域注册 / server 级开关」 | P1 | 中 |
| 3.3 | `mcp.connection_timeout` 默认 **30s**，而 npx 冷启动实测 **~27s** | features.md:141 | `MCPConfig.connection_timeout = 30`（未改）；文档已建议 60–90 | P1（一行默认值 + 文档 + 用例） | 小 |
| 3.4 | `subagent.context_mode`（full / task_only）开关位 | r1a-5 §8核验④ 留位未实现 | `rg context_mode ai_coding/` → **无** | P2 | 小 |
| 3.5 | ~~`depends_on` 硬门禁~~ | r1-analysis §4.1 YAGNI | **R9a 已做**：`Blockers` 纯查询 + `todo_write` 的两道门禁（add 校验 / 状态门禁）+ `ai.enforce_dependencies` 开关（docs/r9-analysis.md §3.1） | —— | —— |
| 3.6 | ~~工具层流式输出 / 取消语义~~ | R2「明确不做」 | **R8c 已做**：`run_process(on_chunk)` + Esc 取消（真的 `killpg`，且不计失败）+ MCP 进度（docs/r8-analysis.md 3.3） | —— | —— |
| 3.7 | ~~非 bash 工具的统一超时~~ | r1a-3「不做」 | **已解决**：R2 的 `max_duration_seconds` + 分发器 `min(工具上限, 回合剩余)` | —— | —— |

## 4. 记忆层（按你的要求：不含 RAG）

| # | 事项 | 出处 | 现状核验 | 建议 | 规模 |
|---:|---|---|---|---|---|
| 4.1 | ~~**SQLite + FTS5/BM25 持久化**（替代 JSON 文件 + idf 覆盖度）~~ | 你当时的原话：「要不要 sqlite 持久化 + fts5，还是 bm25…要不然就先按照你说的来，后续我们再改成这个」 | **R10 已用更小的代价拿到了 BM25 的排序能力，并否决了换存储**：`bm25_scores`（Okapi BM25，`relevance.py`）已经在纯内存的列表上跑起来了，52 条语料上 R@3 0.80→0.85、FP@5 0.518→0.350。SQLite/FTS5 会换的只是**打分来源**，不换门禁、不换诊断、不换工具面——那三样才是这一轮的产出。而且它还有一条本轮实测到的代价：`MemoryStore` 的并发写在 `_mutate` 放锁早于 `_save` 时会落到同一个 `<path>.tmp`（4 线程实测 25 次里 6 次产出非法 JSON，下次加载读到 0 条），换存储引擎能顺手修掉，但那是持久化重构，不该塞进检索轮次 | **已否决**。重新考虑的两个触发条件：**(a)** 语料大到 `bm25_scores` 每次查询全量算 df/长度成为实测瓶颈（今天 52 条语料不是）；**(b)** 要修那个并发写 hazard，且决定连持久化一起换 | 中（若触发） |
| 4.2 | 记忆 scope 策略（workspace / session） | r3 D4：默认 workspace | 已实现，无待办 | —— | —— |
| 4.3 | 跨机器 / 多用户记忆同步 | r3「明确不做」 | 无 | P2 | 大 |
| 4.4 | 记忆写进会话文件 / 与 task-state 合并 | r3「明确不做」 | 保持单一真相源 | **不建议** | —— |
| 4.5 | `open_thread` 30 天过期、整理阈值等经验值 | r3 参数 | 可调，无实测支撑 | P2 | 小 |

## 5. 观测 / 工程（低频但真实）

| # | 事项 | 出处 | 现状核验 | 建议 | 规模 |
|---:|---|---|---|---|---|
| 5.1 | ~~OpenTelemetry / 外部 APM 导出~~ | R4「不做」，留了 hook（O6） | **R9d 已做**（零依赖那一半）：`trace export --format otlp-json`，按 span 分组的真 span 树、确定性 id；**不发 HTTP、不装 OTel SDK**（D-R9-5）。真 collector 验证仍未做 | —— | —— |
| 5.2 | ~~轨迹的跨会话聚合分析（`summary.json` 只到会话粒度）~~ | R4「不做」 | **R9b 已做**：`TraceStore.sessions()` + `core.aggregate` + `trace overview`（`--since` 与 `prune` 同口径） | —— | —— |
| 5.3 | ~~Web/GUI 轨迹查看器~~ | R4「不做」 | **R9c 已做**（静态那一档）：`trace export --format html` 出单文件页面（内联 CSS、无 JS、无外链、全转义）；**不做 web 服务**（D-R9-6） | —— | —— |
| 5.4 | ~~轨迹轮转/清理~~ | R4 计划 | **已解决**：`TraceStore.prune()` 已在 `cli.py:466` 调用，`retention_days` 生效 | —— | —— |
| 5.5 | `pytest-timeout` 之类插件 | R2「不做」（要联网装包） | CI 用 runner 有界性 + `timeout-minutes` 兜底 | P2 | 小 |
| 5.6 | Windows 平台验证（`start_new_session` 属 POSIX） | R2「不做」 | 无 | P2 | 中 |
| 5.7 | 真实 LLM 回归基准（把 `sample_tasks.py` / 三个 `*_cases.py` 汇成一份可对比的分数与趋势） | **本文档新提**（不是历史遗留，但符合你反复强调的「真实跑起来」） | **检索这一半 R10 已经做了**：`scripts/retrieval_cases.py` 是一份真分数、可复现、带 `--check` 门禁的基准（52 条语料 / 46 条标注问法，R@3 / FP@5 / paraphrase@3 / no-answer 四档）。剩下的是**把** `sample_tasks.py` 与各轮 `*_cases.py` 的数字汇成同一张趋势表——仍是各自为政，但基准的**形状**已经定了，其余部分照它做即可 | P1（剩「汇总趋势」这一半） | 中 |

## 6. 已裁决、不要再翻出来的（避免重复讨论）

- Java 兼容退役；`TurnResult` 删除；不自持循环 → **已定**。
- 工具 schema 省略重发（GA 技巧）→ 原生 function calling 不适用。
- hook 永远 fail-open → **反着做**（我们的门卫必须能阻断）。
- monkey-patch 解析器取用量、全局 `STATS` dict → 不做。
- 把记忆/摘要/自进化日志注入系统提示 → 打穿缓存前缀。
- 每轮注入记忆详情 / 每轮跑 L4 摘要 → 不做。
- 新增 `TurnStatus` 成员（R6）→ 用 reason 区分。
- 把 `TurnOutcome.text` 改写成交接文本 → 会误触发整理。
- 主动把 `sessions/*.json` 交给模型 → 实测引发 270KB 整读。
- 向量检索 / embedding（**RAG**）→ R10d 已实现并默认关闭（`memory.embeddings`，标准库 urllib + 磁盘缓存 + 纯 Python 余弦，零新依赖）。当初不纳入的理由（缺可信分母）由 R10a 语料扩展解决了一半：paraphrase 从手标 12 条变成推导 46 条。剩下一半（单标注者）仍在。

---

## 7. 建议的 R7 切片（四件事，按顺序）

> **状态（2026-09-24 收尾）：四件全部实现。** 方案见 `docs/r7-analysis.md`，逐任务计划与
> 执行偏差见 `docs/r7-plan.md` 的「执行状态」，验收脚本 `scripts/r7_cases.py`
> （离线 11/11 + 真实 3/3）。每行末尾的「结果」列写的是实测值与预估的差别。

> 主题：**「收口与长程成本」**——本轮不加新能力，把 R5/R6 之后重新长出来的东西收回去，
> 并解决两个「每一步都在付钱」的问题（L2 清空正文、MCP 工具面）。

| 顺序 | 切片 | 为什么排这里 | 验收判据（可测） | 结果 |
|---:|---|---|---|---|
| R7a | **循环内核再瘦身**（1.1 + 1.2） | 唯一一个「越拖越贵」的债；R6 加的功能已经让 795/228 超过 R5 前 | `agent_loop.py` 回到 ≤650 行、`process_input` ≤150 行；**行为与事件序列逐项不变**（沿用 R5 的取证方式） | ✅ **578 行 / 139 行**；golden 事件序列逐条一致，既有断言零改动（多搬了 `RequestAssembler`/`TurnHooks`/`SafeObserver` 三处才到位） |
| R7b | **L2 头尾窗口**（2.1） | 对准 R6 实测出的病根（工具正文被清空、模型抱怨「输出没返回」）；L4/L1/L2 三层里只有它还完全丢弃信息 | 压缩后同一份 60 轮历史里，中段工具结果的**关键行**仍可检索到；预算不超过 target 的 0.5×；缓存前缀比例不下降 | ✅ 信封 + 头/尾窗口 + `transcripts/elided/` 原文可回收；R6 配置下 est tokens 2,371 → **2,743**（+372），被清掉的最旧一条的关键行回来了；默认配置下 69 条信封 / 20 条带窗口 / 205KB 原文 |
| R7c | **人在环两处收口**（2.3 + 2.4） | 都是小改动；一个防挂死，一个补 R6c 唯一未验证的路径 | 卡住的审批不再挂死整回合（有超时用例）；真 pty 下 `ask_user` 的答案能进工具结果（脚本化 pty 取证） | ✅ 1.0s 的回合不再被 5s 的审批拖成 5.00s（现在 1.00s，`approval_timeout` 是独立结果）；**更正**：真人路径一直能读到答案，坏的是脚本管道（旧 `eof` → 新 `answer="改 A"`） |
| R7d | **MCP 工具面治理**（3.1 + 3.2 + 3.3） | 每步 prompt 成本 × 每次调用的审批噪音，两个实测数字都在手上（10481 chars / 每次弹审批 / 27s vs 30s） | 配置了 allow 策略的 server 不再弹审批；给出「按需注册」前后 `tools_chars` 对照 | ✅ `servers[].auto_approve`（默认 false）免审批；`servers[].tools` 让工具面 **13,301 → 5,666**（-57%）；`connection_timeout` 30 → 60 |

候选池（有空再挑，不必进 R7）：2.2 模型写的 checkpoint、2.5 evidence 参与排序、
3.5 depends_on 硬门禁、3.6 工具流式/取消、4.1 SQLite+FTS5（待你定性质）、5.1/5.2/5.3 观测三件、
5.5/5.6 工程习惯、5.7 真实基准。

**已由 R7e 做掉**（见 `docs/r7e-analysis.md` / `docs/r7e-plan.md`）：
- 3.4 `subagent.context_mode`（`full` / `task_only`）——第 3.4 行关闭；
- 3.2 的工具面治理**再加一半**：超阈值时每 loop 一次 `[tools]` 提示（R7d 给的是开关，R7e 给的是提醒）；
- `r7-analysis.md` §2.5 优化菜单里的 **O2 `memory_write`**、**O4 回合级 steering**、**O5 工具面阈值告警**、**O6 `context_mode`**：四条全部落地。

**之后几轮又关掉的候选**：

- **R8 做掉 2.2 / 2.5 / 3.6**：`checkpoint_write`、evidence 同分破平局 + 过滤、工具流式与 Esc 取消
  （含真 `killpg` 与 MCP 进度）——见 `docs/r8-analysis.md`。
- **R9 做掉 3.5 与 5.1 / 5.2 / 5.3（各自的一部分）**：`depends_on` 硬门禁、跨会话总览、
  单文件 HTML 导出、零依赖 OTLP/JSON 导出——见 `docs/r9-analysis.md`。**仍未做**：真 collector
  往返（5.1 只到「产出 OTLP/JSON 文件」）、交互式 Web/GUI（5.3 只到静态页）。
- **R10 做掉 4.1 的一半、并给 5.7 定了形状**：BM25 排序已落地（纯内存列表，无存储迁移），
  所以 SQLite+FTS5 被**否决**而不是顺延——它换的只是打分来源，门禁/诊断/工具面才是这一轮的产出。
  检索基准 `scripts/retrieval_cases.py` 带 `--check` 门禁，真分数 R@3 0.85 / FP@5 0.350 /
  paraphrase@3 6-12 / no-answer 4-6。详见 [功能清单](features.md) 的记忆章节与
  [集成测试](integration_testing.md) 的 R10 段。

---

## 8. 核验命令（本文档每条现状都可用它们复现）

```bash
wc -l ai_coding/core/agent_loop.py            # 795
.venv/bin/python - <<'PY'                      # process_input 228 行
import ast; t=ast.parse(open('ai_coding/core/agent_loop.py').read())
print([(n.name, n.end_lineno-n.lineno+1) for n in ast.walk(t)
       if isinstance(n, ast.AsyncFunctionDef) and n.name=='process_input'])
PY
rg -n "await self.approval.approve" ai_coding/core/dispatch.py   # 无 deadline 包裹
rg -n "previous_result|PREVIOUS_RESULT" ai_coding/core/context_compression.py
rg -n "connection_timeout" ai_coding/config/models.py            # 30
rg -n "context_mode" ai_coding/ || echo "not implemented"
rg -n "prune" ai_coding/cli.py                                   # 已接线
```
