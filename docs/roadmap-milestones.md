# 缺口路线图 · 按领域划分的里程碑

> 来源：[评估报告](../ai-coding-评估报告.md) §六「缺漏清单」+ 本次全部实测（eval-00..07）。
> 用途：逐个里程碑实现。**每个里程碑都可独立交付、独立验收**。
> 约定：里程碑编号 `M<领域序号>-<子项序号>`；验收判据必须**可机检**（命令 + 判据）。
>
> **规模：8 个领域 / 32 个里程碑**（A6 + B5 + C3 + D3 + E4 + F3 + G4 + H4）。

### N-4 · 项目配置不被信任授予执行 —— ✅ 已完成（2026-10-25）

> 方案与两轮评审见 [plan-config-trust.md](plan-config-trust.md)。

| 项 | 内容 |
|---|---|
| **根因** | `ConfigLoader._overlay` 让**项目层逐键覆盖用户层**，而 `config.example.yaml` 是**仓库里被跟踪的文件**。`_overlay` 的 docstring 自己写了它防住了"项目层放宽工具开关"，却**没防"项目层新增执行能力"** |
| **受影响字段** | `hooks.events[].command`（执行任意程序）、`hooks.events[].module`（`exec_module` 任意 Python）、`mcp.servers[].command`（spawn 任意子进程）、`mcp.local.yaml` sidecar（追加 server） |
| **修法** | `loader.py` 分层读取处按 `role` 过滤：项目层的 `hooks`/`mcp` **丢弃 + warning**，除非 `AI_CODING_TRUST_PROJECT=1`（环境变量，仓库碰不到的信道）。`chosen`（sidecar 锚点）也**不推进到项目层** |
| **关键实现点** | 过滤必须在 `_normalize` **之后** —— `_to_snake('Hooks')='hooks'`，否则一个大小写变体就绕过（评审 P0） |
| **验收** | 14 条新用例（含大小写变体、sidecar、opt-in、`--path` 边界）；**突变 A/B/C 全部被测试抓住**；真机端到端：恶意仓库默认 `HOSTILE CONFIG LOADED: False`，opt-in 才 `True` |
| **契约变更** | `test_mcp_servers_from_both_layers_are_kept` → 改写为"项目层的 server 被丢弃"（原测试锁定的是 `_overlay` 的追加语义，本方案有意变更） |
| **未覆盖（另立项）** | `models[].base_url`/`api_key`（外发）、`tools.extra_workspace_roots`（越权）、`ai.websearch_api_key`（外发 key）、`tracing.dir`（越界写）、`hooks.plugin_dir`（python hook 模块来自不可信仓库树） |

### N-4b · 项目配置不被信任授予端点/凭据/越界路径 —— ✅ 已完成（2026-10-25）

> 方案与两轮评审见 [plan-config-trust-2.md](plan-config-trust-2.md)。

| 项 | 内容 |
|---|---|
| **根因（与 N-4 同源）** | `ConfigLoader._overlay` 深合并、项目层逐键胜出，而 `config.example.yaml` 是仓库里被跟踪的文件。N-4 丢了执行类的**整键**；外发/越权/越界三类**修法不同**（按成员丢 / 取交集 / 丢弃 + 消费侧护栏） |
| **缺口 1 · 外发** | 项目层只写 `models.x.base_url`，`api_key` 仍继承用户层真实密钥 → 用户 key + 整段对话被发往攻击者端点。修法：按成员丢 `base_url`/`api_key`；项目层新增的 `models` 条目整条丢弃；`default_model` 仅当指向项目层新增模型时丢 |
| **缺口 2 · 越权** | `tools.extra_workspace_roots: ["/"]` → 沙箱对整台机器免问读写。修法：取交集（覆盖语义）；展开后为空的声明从项目 document 删键以保留用户层 |
| **缺口 3 · 越界路径** | `tracing.dir`/`memory.embeddings.cache_path` 指向工作区外。修法：项目层丢弃（回落默认）+ 消费侧护栏（`TraceStore.prune` 越界拒删、`_embeddings_for` 钳回工作区） |
| **缺口 6/7 · 残余** | `hooks.plugin_dir` 默认目录移到用户级（`~/.config/ai-coding/hooks`）；MCP token 键加入端点（`_token_key`） |
| **关键实现点** | 过滤在 `_normalize` 之后；`_filter_project_document` 的 `models` 规则 0 处理 `models:`（YAML null）覆盖 mapping；`_narrow_roots` 在**展开后**的目录上取交集；`expand_root_patterns` 的 `base` 在所有调用点生效 |
| **验收** | 11 条红相用例（7 config + 1 hooks + 1 trace + 1 embeddings + 1 mcp）+ 2 条 Prove 全绿；`TraceStore(...)` 全部构造点补 `workspace=` |
| **契约变更** | ① 项目层 `models.*.base_url`/`api_key`、`ai.websearch_api_key`、`tracing.dir`、`memory.embeddings.cache_path` 不生效；② 项目层不能新增模型；③ `extra_workspace_roots` 只能收紧；④ 嵌入缓存绝对路径一律钳回工作区（**唯一跨层改动**）；⑤ `config.example.yaml` 的 `models`/`default_model` 失效；⑥ `plugin_dir` 默认改用户级；⑦ MCP token 键变 → 需重新登录一次 |

### N-5 · CI 连续失败修复（ripgrep 环境 + 3.10 时间戳解析）—— ✅ 已完成（2026-10-25）

> 方案与两轮评审见 [plan-ci-fix.md](plan-ci-fix.md)。

| 项 | 内容 |
|---|---|
| **事实澄清** | 仓库**没有 PR**（`gh pr list --state all` 为空）——失败的是直推 `main` 触发的 CI。4 次失败运行对应 **3 种互不相关的失败模式**，其中 2 种已在 HEAD 修掉（`test_loop_digest`、ruff 4 errors） |
| **根因 A** | `ubuntu-24.04` 镜像（`20261012.334.1`）**不含 ripgrep**，而 rg 分支有 **7 个消费者**：3 条硬失败、2 条 skipif、2 条静默假绿。CI 因此**从未验证过 rg 分支** —— 正是 r19/F1 泄漏 bug 的成因（`data-contracts.md` r20/F0 条目） |
| **根因 B** | `tests/test_plan_m1.py:766` 用裸 `datetime.fromisoformat(stamp)`，而 `Z` 设计符到 **3.11** 才支持，3.10 是声明下限 → 3.10 job 独有的一条失败 |
| **修法** | ① `ci.yml` 显式 `apt-get install ripgrep`（让环境满足测试的正当依赖，而非加 skipif 把问题藏起来）；② 新增 `infra/time_utils.parse_iso8601`，4 个消费者共用（3 处生产 + 1 处测试），消掉 3 份手写 `.replace("Z","+00:00")` |
| **关键实现点** | `parse_iso8601` 是**纯解析器**（无偏移返回 naive）：三处消费者的时区策略不同（OTLP 按本地、`relevance.parse_time` 补 UTC），在共用函数里二选一会静默改变另一方的数字 |
| **验收** | 3.14：3863 passed（+5 新用例）；**3.10 七步全绿**（3863 passed / mcp_stdio 1 / mcp_end_to_end 4 / memory 11/11 / trace 7/7 / ruff / mypy）。突变测试（去掉 `.replace`）在 3.10 下让 U1+U6 变红；rg 缺席复现让 3 条用例失败。**真实 CI [`40205842561`](https://github.com/linsir2/ai-coding/actions/runs/40205842561) 双 job 全绿**（本仓库首次）：rg 相关跳过 0 条（2 条 parity 从跳过转运行），6 条跳过全是 MCP 门控；runner apt 装的是 **rg 15.1.0**，与测量基线一致，无版本偏差 |
| **契约变更** | `parse_iso8601` 成为 ISO-8601 解析唯一入口（见 `data-contracts.md`）；`memory/relevance.parse_time` 改为薄封装，导出契约不变 |
| **未覆盖（另立项）** | N-6（2 条 rg 用例缺守卫） |

---

## 0. 领域总览

| 领域 | 名称 | 缺口数 | 优先级 | 依赖 |
|---|---|---|---|---|
| **A** | 正确性基线（缺陷 / 契约漂移 / 环境伪失败） | 6 | **P0** | 无 |
| **B** | 收敛与编排（检索收敛 / 重试分类 / 提示回喂） | 5 | **P0** | A |
| **C** | 恢复与持久化（step 级 durable intent / 记忆并发安全） | 3 | P1 | A |
| **D** | 安全（OS 级沙箱 / 策略-机制分离 / 危险命令收敛） | 3 | P1 | A |
| **E** | 上下文工程（压缩精细化 / L3 语义 / 预算可观测） | 4 | P1 | A |
| **F** | 记忆（语义档默认化 / 场景治理 / 召回预算实测） | 3 | P2 | A |
| **G** | 可观测与质量（lint/type 门禁 / 基准回归 / 成本看板） | 4 | P2 | A |
| **H** | 产品化（安装分发 / 插件市场 / IDE 集成 / 云同步） | 4 | P3 | A-G |

**推荐起点：`M-A1`（见 §1）** —— 理由见 §9。

---

## 1. 领域 A · 正确性基线 【P0 · 无依赖】

> 目标：**让"测试通过"这件事重新可信**。这是后续所有里程碑的验证前提。

### M-A1 · 必崩缺陷修复 —— ✅ 已完成（2026-10-24）

> 实施记录与两轮评审见 [plan-m-a1.md](plan-m-a1.md)。**调研结论与原计划不同**：
> 不是"1 行笔误"，而是**代码与 SDK 版本错配**（代码按 mcp 2.2.0 写，实装 1.30.0）。

| 项 | 内容 |
|---|---|
| **实际改动** | 产品 3 处（`mcp.py`）+ 依赖声明 1 处（`pyproject.toml`）+ 评审追加 3 处（客户端生命周期 / OAuth 拒绝语义 / 假 AS 回显）+ 负路径测试 1 条 |
| **实测版本矩阵** | mcp **1.19–1.23** 无 `streamable_http_client`；**1.24–1.30** 两者皆可；**2.0+** 要 `AuthorizationCodeResult`+`httpx2` → 依赖收紧为 **`mcp>=1.24,<2`** |
| **验收** | `pytest tests/test_mcp_oauth.py` **18 passed**；全量 **10→3 failed**；`mypy` **6→3**；`ruff`（改动文件）0 |
| **副产物** | 澄清了 `httpx2` 之谜：它在 **mcp 2.2.0 里是真实依赖**，不是笔误 —— 原代码是按 2.2.0 写的 |

### M-A2 · 契约漂移收敛（`open_all()` 返回值）—— ✅ 已完成（2026-10-24）

> 实施记录与评审见 [plan-m-a2-a5.md](plan-m-a2-a5.md)。**调研推翻了本节原判的两条**：
> ① "两处消费方已坏" —— 实测三个消费方（`world.py:195/204`、`mcp_call.py:92`）**都没坏**，
> 工具面唯一来源是 `MCPManager.available()`；② "建议选 ②" —— **物理上不可能**：
> M3 起 `open_all()` 非阻塞，返回时 server 尚未连上，工具不存在，强行返回只能撒谎。

| 项 | 内容 |
|---|---|
| **缺口** | `MCPManager.open_all()` 标注 `-> None` 且返回 `None`，但 `docs/features.md:542` 写"返回空列表"；`docs/r1a-6-analysis.md:251` 亦写 `[]` |
| **实际改动** | `docs/features.md:516/542` 措辞对齐；`r1a-6-analysis.md` ×4、`r1-analysis.md` ×2、`architecture.md` ×1、`superpowers/plans/` ×2 加"已被 M3 取代"标注；`eval-03-live-features.md:120` 同步；`scripts/sample_tasks.py` 的 `s0_mcp_off` 改读 `manager.available()` |
| **关键发现** | 原 `s0_mcp_off` 用 `(await open_all()) or []` 断言 `tools == []` —— **恒真空转**：server 真连上 3 个工具时该断言仍为 `True`（突变 C 实测）。改后断言 `rows == []` 才有实质 |
| **验收** | ① `ruff`/`mypy` 0；② 标注计数 4/2/1；③ `grep "立即返回空列表" docs/features.md` = 0；④ `sample_tasks.py --only mcp-off` → `PASS enabled=False servers=0`；⑤ 全量 **0 failed / 6 skipped** |
| **不含** | `open_all()` / `available()` / `build_mcp_catalog` / `MCPCallTool` / `world.py`（**一行未改**） |

### M-A3 · undo 部分回滚未上报 —— ✅ 已完成（2026-10-24）

> 实施记录与评审见 [plan-m-a3.md](plan-m-a3.md)。评审发现 redo 有同类缺陷（reload 判据），已纳入范围。

| 项 | 内容 |
|---|---|
| **缺口** | `undo_last_turn` 的 `ok` 恒为 `True`，与 `failed`/异常解耦：部分回滚被报成成功（改前实测 `{'ok': True, 'reason': 'restore-partial: ro/stuck.txt'}`）；且 `commands.py` 用 `ok` 决定 `RELOAD`，而 redo 的历史是**无条件追加**的 → 部分 undo/redo、`no-snapshot`、`restore-failed` 都是"历史已改写但前端不重投影"，transcript 与 tree 不一致 |
| **影响** | 用户以为全恢复了，实际有文件没恢复；且部分 undo/redo 时 transcript 与 tree 不一致 |
| **改动** | `agent_loop.undo_last_turn` 引入 `restore_ok`（`if failed:` 与 `except` 两处置 `False`，报告改用之）；`commands.py` 新增 `_rewrote_history`，undo/redo 都改以"报告是否带历史记账字段"决定 `RELOAD`，redo 文案补 reason；`test_r17_defects.py:1005` 断言翻转；`test_undo_command.py` 新增 3 条 |
| **验收** | ① 该 pytest 用例转绿；② 新增"部分 undo/redo 仍 RELOAD""早退仍 CONTINUE"回归用例；③ `docs/data-contracts.md` 同步；④ 全量 **0 failed / 6 skipped**；⑤ 突变（`ok` 改回 `True`）打红该用例 |
| **不含** | `snapshots.py`（契约已正确）、`handlers.py`、`frontend/src/routes/session.tsx`、`_REASON_TEXT`（补映射会丢路径） |

### M-A4 · 环境伪失败的隔离（CI 可信度）—— ✅ 已完成（2026-10-24）

> 实施记录与评审见 [plan-m-a4.md](plan-m-a4.md)。**调研推翻了本节原判**：
> 那 6 条"需外网"的 MCP OAuth 用例**是 M-A1 修好的**（它们用 `127.0.0.1` 假 AS，完全离线）；
> 真正的环境伪失败只有 **3 条**，且全是 root 绕过 DAC 所致。

| 项 | 内容 |
|---|---|
| **实际缺口** | 3 条测试靠 `chmod` 制造"操作系统拒绝"，而 root 绕过 DAC → 拒绝不发生 → 断言失败 |
| **影响** | root 容器（最常见的开发/CI 环境）下这 3 条**静默空转**：CI 绿了也不知道是不是真绿 |
| **实际改动** | 新建 `tests/conftest.py` 的泛化 helper `refused(monkeypatch, path, attr)`；3 条测试改为注入；`pyproject.toml` 加 `-rs` |
| **验收** | `pytest` **3806 tests / 0 failures / 0 errors / 6 skipped**；ruff 零新增；**突变测试**证明不空转；**真实 EACCES**（以 nobody 跑）证明产品代码是对的 |
| **副产物** | `scripts/verify_real_eacces.py` —— 零注入的真实业务逻辑验证 |

### M-A5 · lint / type 门禁清零 —— ✅ 已完成（2026-10-24）

| 项 | 内容 |
|---|---|
| **缺口** | `ruff check .` **6 errors**（`--fix` 自动修 3 条 + 手工 3 条）；`mypy ai_coding` **3 errors / 3 files** |
| **mypy 明细** | `core/events.py:581` 冗余 cast；`infra/deadline.py:171` arg-type；`core/mcp.py:693` 返回类型（`cls: type` 裸 type 无法窄化） |
| **改动** | `events.py:581` 去冗余 cast 并删仅此一处使用的 import；`deadline.py:171` 显式 `set[asyncio.Task[Any]]`；`mcp.py:683` `cls: type[BaseException]`；`w2_live_check.py:145` 删死行；`eval_comparison.py:81`/`eval_longtask_mechanics.py:98` 折行 |
| **验收** | `ruff check .` = **0**；`mypy ai_coding` = **0**（152 文件）；**突变 A/B** 各自精确复现原报错 |
| **副产物** | CI 的 Lint / Type-check 两步（`ci.yml:35-38`）此前在新克隆上即红，现已转绿 |

### M-A6 · 文档与实测不一致的修订 —— ✅ 已完成（2026-10-25）

> 实施记录与评审见 [plan-m-a6-n3.md](plan-m-a6-n3.md)。**调研推翻了本节原判的两条**：
> case7 不是"代码多喂一条消息"（收割异步调度导致多回合合并，是设计）；
> case11 不是"正常波动"（阈值随工作区路径漂移，87% → 77%）。

| 项 | 内容 |
|---|---|
| **缺口** | `memory_cases` **case7**：文档/用例期望每轮收割批次 ≤3，实测 `[3, 4]`；**case11**：文档记缓存前缀 92%，实测 87% |
| **实际根因** | 两条都是**期望没跟上实现**，**产品代码零改动**。① case7：收割是**异步调度**（`agent_loop._schedule_harvest`），delta 取「游标之后的全部」，多回合合并是**设计**（`agent_loop.py:1509-1514`）；旧断言 `len(sizes)==4` 测的是**事件循环时序**（同一测试两次记录不同：`[3,4]` vs `[3,4,1]`）。② case11：易失尾部含工作区路径，整请求比率随路径漂移（8/32/66/442 字符 → 87/87/86/**77%**） |
| **改动** | `scripts/memory_cases.py`：case7 断言改为「不丢不重 + 任一批 ≠ 整段历史」（复用既有 `AgentLoop.flush_harvest()`）；case11 改用**路径无关的布尔不变量**（剥离易失尾部后 `startswith`）。文档同步 5 份（含 `features.md`） |
| **验收** | ① `scripts/memory_cases.py` **11/11**；② **深路径（442 字符）下仍 11/11**；③ `docs/features.md` 已加口径说明；④ `ruff`/`mypy` 0 |
| **副产物** | **发现比率是坏度量**：per-turn-trim 臂的稳定比率同样 ~100%，但 `prefix_intact=False`（历史被重写）—— **比率会说好，不变量会说实话** |

---

## 2. 领域 B · 收敛与编排 【P0 · 依赖 A】

> 目标：**解决本次评估最大的短板** —— T2 实测 63 次工具调用 / 900 秒 / 零结论。

### M-B1 · 静态检查结果回喂通道 ⭐ —— ✅ 已完成（2026-10-25）

> 实施记录与两轮评审见 [plan-m-b.md](plan-m-b.md)。

| 项 | 内容 |
|---|---|
| **缺口** | mypy/ruff 早就能报出 `httpx2`，但**结果没有回到模型面前**。模型只能逐文件 grep |
| **实际做法** | 新增 `core/static_check.py`（复用 `infra/subprocess_runner.run_process_async`）：`[search-gate]` **L2**（streak ≥6）触发时，harness 跑 `ai.static_check.commands`，把**真实输出**拼进注入文本；每回合最多一次 |
| **安全约束（两轮评审后）** | 该字段能被**仓库里被跟踪的 `config.example.yaml`** 设置，所以配置不能充当许可：**默认不执行**，需操作者 `AI_CODING_STATIC_CHECK=1`（仓库碰不到的信道）。开启后仍 `shlex` 切分 + `shell=False` + 首词白名单（`cargo`/`go`/`eslint` 等「本身执行仓库代码」的工具已移出白名单） |
| **同源风险（待立项）** | `core/hooks.py:283` 同样执行**配置来源**的 command 且无权限门；`mcp.servers[].command` 亦然。缺的是「项目配置是否可信」这一层机制（`--trust-project-config`），不是单字段补丁 |
| **关键否决** | **命令不能自动推断**：实测本仓 `mypy .` 报 **271 errors**（`scripts/` 未排除）而 CI 跑的 `mypy ai_coding` 零错。默认只给 `ruff check .`，mypy 由使用方声明 |
| **验收** | ① 2 条全链路用例（真 loop + 真工具 + 真子进程）；② `tests/test_static_check.py` 11 例（含 env 开关/执行型工具被拒/`;` 不成 shell）；③ **未做** T2 真机复测（需 API key）|

### M-B2 · 检索收敛判据 —— ✅ 已完成（2026-10-25）

| 项 | 内容 |
|---|---|
| **缺口** | `[strategy-gate]` 只管"工具全失败"，**没有针对"工具成功但无进展"** |
| **实际信号** | `search_streak` = 连续**成功**的检索调用、中间没有成功的非检索调用。**只看工具名 + success，不解析输出** |
| **为什么不用"无新命中"** | 评审 P0：`grep`/`glob` 的 `(no matches)` 逐字节相同、`glob` 按 mtime 排序、`history_search`/`websearch` 内嵌 query、`grep` 裁 300 字符、`DuplicateCallGuard` 拒绝第 3 次相同调用 → 双向皆错且达不到阈值 |
| **阶梯** | L1（streak ≥4）换手段 → L2（≥6）跑静态检查。**无收尾档**（信号分不清调研与空转） |
| **验收** | 阶梯单测 + 信号单测 + 2 条全链路用例（含"默认预算下 L2 之后仍有模型步"）；`docs/features.md` 已加该阶梯 |

### M-B3 · 重试分类 —— ✅ **早已实现**（roadmap 原判为误）

| 项 | 内容 |
|---|---|
| **原判** | "只对四类异常统一指数退避 —— 没有'绝不重试'这一档" |
| **实测** | `FailureKind` **六档** + `retry_delay(retry_after=…)` + `AUTH_ERROR` 在 [openai_engine.py:352-358](file:///workspace/ai-coding/ai_coding/ai/openai_engine.py#L352-L358) **显式不重试**（注释原文 "Exits on the first attempt instead of after four identical 401s"）。实跑 `max_retries=3` + 401 → `calls == 1` |
| **唯一补丁** | 429 `insufficient_quota` 原先归 `RATE_LIMITED` 会重试满 4 次 → 已改为 `FATAL`（+1 测试） |
| **不做** | roadmap 提的 `RetryStrategy` / `RETRY_LADDER` **是重复造轮子** → 砍 |

### M-B4 · 检索类任务的预算约束 —— ⚠️ **大部分已实现**

| 项 | 内容 |
|---|---|
| **原判** | "`max_tool_iterations=30` + `max_auto_continuations=4` = 150 步烧掉 900s" |
| **实测** | 默认是 **10 / 3**（仓库内可读配置也是 10）；`budget_pressure()`（G8-1）已在预算将尽时注入 `[budget] 步数 x/y`；`_ask_for_closing`（G8-2）已做收尾（仅 iteration 分支） |
| **未做** | "按检索意图收紧预算" —— 需要意图分类，**给不出可辩护契约**（模型自报不可靠、启发式会误伤正常重构回合）。B4 的验收判据（≤180s 给结论或收尾）靠 B1+B2 达成 |
| **状态** | 记录为**不采纳**，不标 ✅ |

### M-B5 · 收敛可观测 —— ✅ 已完成（2026-10-25）

| 项 | 内容 |
|---|---|
| **缺口** | 看不到"这个回合检索了多少次、有没有收敛" |
| **实际做法** | 新增 `SearchStalled` 事件（`iteration`/`level`/`calls`/`streak`），在 `_note_intervention` 发；`trace.py` 的 `dataclasses.asdict` 让它**自动**落进 trace jsonl，无需登记 |
| **验收** | 全链路用例断言 `levels == [1, 2]`；trace jsonl 可直接读到 |
| **未做** | `/trace` 面板的专门展示（前端不改；jsonl 已有） |

---

## 3. 领域 C · 恢复与持久化 【P1 · 依赖 A】—— ✅ 已完成（2026-10-25）

> 方案：[plan-m-c.md](plan-m-c.md)（含两轮联网调研 + 两轮评审）。
> **范围裁剪**：write-ahead 落在 **sidecar**（`sessions/write-ahead/`）而非 `TurnCloser.save` ——
> 后者会**丢弃在飞的工具调用**，而这个坑仓库里已踩过三次。

### M-C1 · step 级 durable intent —— ✅ 已完成（2026-10-25）

| 项 | 内容 |
|---|---|
| **缺口** | 恢复粒度是**会话级**（`world.find_armed_session` 只捞 `armed_profile ∈ {plan, goal}`）。对标：opencode **write-ahead claim**（step 级）、Codex **rollout 写恢复** |
| **改动方向** | 会话 JSON 增加 step 级 intent 记录（已执行的工具调用 + 结果摘要），重启后可从**最近已完成 step** 续跑，而非整段重跑 |
| **验收判据** | ① 单测：模拟进程在 step N 后崩溃 → 重启续跑且**不重复执行已完成的工具**；② `docs/data-contracts.md` 更新会话 schema |
| **实际落地** | `mutates_world` 工具的 **write-ahead sidecar**（`sessions/write-ahead/`），**回合结束清空**（复用域 = 恢复窗口，避免 `websearch`/`bash("npm install")` 在后续回合返回旧结果）；`digest` 复用 `call_fingerprint` |

### M-C2 · 记忆并发写安全 —— ✅ 已完成（2026-10-25）

| 项 | 内容 |
|---|---|
| **缺口** | `MemoryStore._mutate` 在 `_save` **之前**放锁，`_save` 无锁读 `self._notes` → 两线程落到同一 `<path>.tmp`，`os.replace` 后另一线程继续写正式文件 → **文件变非法 JSON，加载读到 0 条** |
| **复现** | 需 `threading.Barrier.wait()` 强制同步，4 线程 × 30 次写，25 次里 5-6 次损坏（文档已自述） |
| **改动方向** | `_save` 进锁（文档说"会改动每个调用方的持久化形状"，需评估）；或改用 `fcntl.flock` 跨进程文件锁 |
| **验收判据** | 新增 `test_memory_concurrent_write_never_corrupts`：强制同步下 25 次写 **0 损坏**，且重载后条数一致 |
| **实际落地** | `_save` 进锁 + `fcntl.flock` + **唯一 tmp 路径**（三管齐下：进程内互斥 + 跨进程互斥 + 消除同路径竞争） |

### M-C3 · 崩溃后续跑的验收门 —— ✅ 已完成（2026-10-25）

| 项 | 内容 |
|---|---|
| **缺口** | `resume_count >= 5` 才 disarm，期间没有"这次续跑有没有真的前进"的判据 |
| **改动方向** | 续跑时比较"续跑前后产出"，无前进则提前 disarm 并给交接报告 |
| **验收判据** | 单测：续跑无前进 → 提前终止 + 交接报告 |
| **实际落地** | **boot 到 boot** 四元组指纹（含**非合成消息数**）+ **连续 2 次无前进** → 提前 disarm + `ResumeStalled`。（原判据 `rounds_run` 每轮必增，**不可满足**；且崩在轮中途时"记 after"根本不执行 —— 评审 P1 修） |

---

## 4. 领域 D · 安全 【P1 · 依赖 A】—— ✅ 已完成（2026-10-26）

> 方案：[plan-m-d.md](plan-m-d.md)（写边界）+ [plan-m-d2.md](plan-m-d2.md)（读边界 + 网络墙，三轮方案评审 2 个 P0 + 一轮实现评审 1 P1 全部收口）。
> **范围裁剪**：本轮补齐"双墙"（文件墙 + 网络墙）。**未做**：IP/CIDR 级出网管控、
> AF_UNIX 路径级控制（seccomp 无法仲裁路径）、`/proc` 完全隔离 —— 见 `plan-m-d2.md` §7.1。

### M-D1 · OS 级沙箱 —— ✅ 已完成（2026-10-26）

| 项 | 内容 |
|---|---|
| **缺口** | `security/sandbox.py` **只做路径限制**（拦 `..`/绝对路径/符号链接逃逸）。对标 Codex：`execpolicy`（纯策略）+ `sandboxing`（纯机制）+ 跨平台后端（Seatbelt / bwrap+landlock / MXC） |
| **改动方向** | ① 先做 **Linux landlock/bubblewrap**（bwrap 已有依赖先例）；② 把策略判定（ALLOW/WARN/DENY）与机制（真正隔离）**分离**，`bypass_sandbox` 要求显式 allow |
| **验收判据** | ① 单测：沙箱内进程**写不到**工作区外（真实子进程验证，不是 mock）；② `ruff`/`mypy` 0 |
| **实际落地** | `security/process_sandbox.py`：`ProcessSandboxPolicy`（off/warn/enforce）/ `SandboxBackend` / `BwrapBackend` / `spawn_argv`（唯一 wrap 入口）。**策略与机制分离**，`enforce` 默认、fail-closed（机制缺失即拒绝并给出稳定原因码）。`BashTool` 与 `JobRegistry` 各自 catch `SandboxGap` → `error="denied"`。**子智能体栈同样接线**（roots 绑定子 worktree）。验收①**在 CI（真内核 + bubblewrap）实测通过** |

### M-D2 · 危险命令判定的可解释性 —— ✅ 已完成（2026-10-26）

| 项 | 内容 |
|---|---|
| **缺口** | `security/bash_redlines.py` 用正则/tree-sitter 判危险命令，但**误报/漏报无基准** |
| **改动方向** | 建一个**危险命令语料基准**（正例：`rm -rf /`、`sudo`、`dd if=`、`> /dev/sd`、强制 push…；负例：`rm -rf ./build`、`sudo` 出现在引号里…），输出 FP/FN 指标 |
| **验收判据** | 基准脚本 + 指标表；FN = 0（漏报零容忍），FP 有界 |
| **实际落地** | `security/redline_corpus.py`（**六类 golden 语料 135 条**，verdict 逐条冻结）+ `scripts/redline_benchmark.py`（进 CI）。**基准的定位是回归检测**：FN=0 / FP=0 为硬阈值，`known_miss`/`known_fp` 按判定输出上报。**附带合并了"双读法"**（`bash_redlines` 与 `permission_gate` 各一份规则 → 单一判据 `bash_red_line_with_fallback`，用独立冻结快照证明行为保持）。**16 条 `known_miss` 让"语法可读但无规则命中即放行"这个真缺口可见**（GuardFall 机理在本仓库的具体落点） |

### M-D3 · 权限授予的最小化 —— ✅ 已完成（2026-10-26）

| 项 | 项 | 内容 |
|---|---|---|
| **缺口** | `security/grants.py` 的"always"记忆是**目录级**的；`extra_workspace_roots` 是启动时一次性展开 |
| **改动方向** | 到期/撤销机制；UI 可见"当前会话被授予了哪些权限" |
| **验收判据** | 单测覆盖授予/撤销/到期；`/permissions` 面板可查 |
| **实际落地** | `grants.json` **形状不变 + `meta` 可选键**（合项目"加可选键、不 bump 版本"的迁移约定）→ TTL 可配（默认 24h）；`GrantStore.load_with_expiry` + gate 的 `_grant_expiry` → **磁盘加载的授权也会在会话内过期**；`revoke(key)`（全键精确 / 基名撤销全部 posture 变体）+ `permissions.revoke` IPC + `/permissions revoke` + 面板逐条撤销（↑/↓ 选、`x`）。`process_sandbox` 进 `EXECUTION_KEYS`（项目层不能关沙箱） |

---

## 5. 领域 E · 上下文工程 【P1 · 依赖 A】—— ✅ 已完成（2026-10-26）

> 方案：[plan-m-e.md](plan-m-e.md)（四轮方案评审 7 个 P0 + 一轮实现评审 1 P2 全部收口）。
> **一句话**：E 不是独立子系统，而是横切在 ReAct 循环请求组装路径上的一层上下文塑形。

### M-E1 · L3 语义显式化 —— ✅ 已完成（2026-10-26）

| 项 | 内容 |
|---|---|
| **缺口** | L3 的语义（"只处理末尾连续 tool 段"）是**隐式**的 —— 无文档、无测试锁定（3 个用例全为尾部输入） |
| **改动方向** | 明确语义并写进文档 |
| **验收判据** | 单测覆盖 + 文档 |
| **实际落地** | **A1**：语义写进 `features.md` / `data-contracts.md`（L3 是**回合中段护栏**，只处理末尾连续 tool 段 + 批总量 > 窗口一半 + 单条 > 30000）+ 测试锁定"非尾部大输出不被持久化"（**零行为变更**）。附 `LayerStat.skipped` 让"L3 跳过几条"可观测 |

### M-E2 · L2/L4 顺序导致的计数不稳定 —— ✅ 已完成（2026-10-26）

| 项 | 内容 |
|---|---|
| **缺口** | 全仓没有断言管线顺序的单测；`eval-04` 的端到端计数断言**已被改成"直接验证真实 `elide_and_persist`"** —— 即承认了计数不可断言，但没把这个承认固化 |
| **改动方向** | 明确该顺序是刻意的并写进文档 |
| **验收判据** | 单测覆盖 |
| **实际落地** | 文档化现有判据（**触发按 token** `should_compact`、**成功按内容真值** `result != history`、**不按条数** —— Hermes 教训）；**管线顺序不变量**（`force=True` + 双注入点，断言全序 `L3→L1→L2→L4`）；**内容真值契约**（`_SameCountRewriter` 造"同条数异内容" → `CompactionEnded`）；**管线级原子组**回归锁。**修正了一个方案误判**：原写"按 token 判定压缩成功"，而该前提**不存在** |

### M-E3 · 压缩预算可观测 —— ✅ 已完成（2026-10-26）

| 项 | 内容 |
|---|---|
| **缺口** | `CompactionEnded` 只有条数 + 摘要自身 usage，**没有每层 token 估算与命中数**；`/trace` 不渲染压缩 |
| **改动方向** | 扩展事件 + `/trace` 渲染 |
| **验收判据** | 单测覆盖 + `/trace` 可见 |
| **实际落地** | `LayerStat`（`removed`/`rewritten`/`skipped` + `before/after_tokens`）+ `CompactionEnded.layers`（由 `Compactor._compress_once` 读 `last_layer_stats` 填入）；`/trace` 按 **token 差**渲染 + "L4 未触发"；前端卡片；`to_wire`/`session.ts`/两处整字典断言同步。**修正了一个误诊**：`r7_cases` 的 `7058 > 24000` **不是预算缺口** —— 它读了未解析的 `AIConfig().max_context_tokens = 0`（阈值 0.0）；真窗口 48000 → 阈值 24000，7058 是 **29%**。修法是**改判据载体** |

### M-E4 · 压缩前缀缓存友好度 —— ✅ 已完成（2026-10-26）

| 项 | 内容 |
|---|---|
| **缺口** | 缺 CI 里的离线门禁；无"所有注入通道都按预算分页"的统一断言；`cache_probe.py` 是手工脚本、不在 CI |
| **改动方向** | 加断言 + 接入 CI |
| **验收判据** | 单测覆盖 |
| **实际落地** | 用 **`canonical_request(instructions, Assembled.history, tools_text)`** 做**全前缀布尔断言**（`stable_b.startswith(stable_a)`，**路径无关**）+ **五类 skip**（`task_injection=system` / 回合内压缩 / instructions 变化 / `tools_text` 变化 / plan reminder）+ **"至少执行一次"护栏**。**修正了一个方案错误**：原写断言 `prefix_shared_ratio ≥ 0.9`，而 **M-A6 已明确弃用百分比阈值**（分母含易失尾部 → 深路径下只有 77%） |

---

## 6. 领域 F · 记忆 【P2 · 依赖 A】

### M-F1 · 语义档可用化

| 项 | 内容 |
|---|---|
| **现状** | 实测 paraphrase@3 **0/46**（词面）→ 开语义档后 **6/46**，可达 R@3 0.99→1.00 |
| **缺口** | 默认关（因 deepseek 无 `/embeddings`）；`min_cosine=0.60` 只在 text-embedding-v4 上标定过 |
| **改动方向** | ① 加"端点是否支持 embeddings"的**自动探测**，支持则默认开；② 提供基准脚本供换 provider 时重标 |
| **验收判据** | 探测逻辑单测；基准脚本可复跑 |
| **实际落地（2026-10-27）** | ✅ 有意偏离①"支持则默认开"（默认开=默认触网，与 OWASP excessive functionality 相反）；改为把"配了但不工作"做成**可观测**：`RecallDiagnostics.semantic_status` 四态（`off`/`endpoint_unsupported`/`on`/`error`），工具 payload 带该字段。标定脚本 `scripts/calibrate_memory_threshold.py`（注入 transport，FP/FN 曲线 + 推荐值 + FP@5 + 有效域）。 |

### M-F2 · 场景标签治理

| 项 | 内容 |
|---|---|
| **缺口** | 实测采纳率 93%（很好），但**实测推翻了自己两个说法**：① 场景标签不是查询词（是过滤器不是召回器）；② JSON 片段会被当记忆存进去（已修） |
| **改动方向** | 把这两条实测结论写回 `docs/features.md` 的召回契约；补"标签治理"（过多标签时的合并策略） |
| **验收判据** | 文档与实测一致；标签数上限策略有单测 |
| **实际落地（2026-10-27）** | ✅ 同义折叠表**一份**（`ai_coding/memory/scenes.py::SCENE_SYNONYMS`，`migrate_scenes.py` 导入同一份），`fold_scene()` 在**闸门/过滤/读/写四处**生效；`normalize_scene` 不变；文档写明"标签是过滤器不是召回器"与"不设全局上限，因为上界是 `max_index_entries=200`"。 |

### M-F3 · 召回预算实测

| 项 | 内容 |
|---|---|
| **缺口** | `memory.recall_timeout_seconds=2.0` 对实测 ~5ms "虚位以待" |
| **改动方向** | 建召回延迟基准（随库规模增长），让超时阈值有实测依据 |
| **验收判据** | 基准脚本 + 指标表 |
| **实际落地（2026-10-27）** | ✅ `scripts/memory_latency_cases.py`：三档（命中/普通未命中/过滤未命中）× 规模（50/200/1_000，10_000 显式），造库不走 `add_note`，`recall_timeout_seconds=0`；断言只用计数桩，p50/p95 打印；写盘与 `index_summary` 各自独立微基准。文档写明 `2.0` 相对哪个分位、且**不含写盘**。 |

---

## 7. 领域 G · 可观测与质量 【P2 · 依赖 A】

### M-G1 · 基准回归门禁

| 项 | 内容 |
|---|---|
| **缺口** | `scripts/retrieval_cases.py --check` 是**本地门禁，CI 目前不调**（文档自述） |
| **改动方向** | 接入 CI；`memory_cases`、`r18_acceptance`、`trace_cases` 一并接 |
| **验收判据** | CI 里这些脚本任一不达标 → 红 |
| **实际落地（2026-10-27）** | ✅ 实测：`memory_cases` / `trace_cases` / `redline_benchmark` **早已在 CI**，真缺口只有 `retrieval_cases.py --check` 与 `r18_acceptance.py`（今天都是绿的）。两个 step 已接进 `quality` job。**`r7_cases`（exit 1）与 `r11c_cases`（exit 2）离线且今天是红的**，本轮点名不接（需先修 domain-B 过期期望 / C5 场景未产出）。其余 `*_cases.py` 的离线分支基本重跑已有单测，不接。 |

### M-G2 · 成本看板

| 项 | 内容 |
|---|---|
| **缺口** | `core/aggregate.py::build_report` 已能算 token，但 `--since` 依赖目录 mtime（rsync 会漂移）；且已无 CLI 入口 |
| **改动方向** | 修 `--since` 的漂移（改用文件内时间戳）；恢复一个 CLI 或做成 `/usage` 面板 |
| **验收判据** | rsync 后的目录不漂移；面板可查 |
| **实际落地（2026-10-27）** | ✅ 实测 `--since` 是**死参数**（3 个生产调用方都不传），但 `prune` 读同一个目录 mtime 且**真被调用** —— rsync 不带 `-t` 后 `prune` 永久失效。修法：`SessionRef.mtime` → `last_activity`（取 turn 文件里最后一个 `ts`，回退目录 mtime），`prune` / `--since` / 排序三处同源。`render_overview()` 本来就在、只是**零消费者**，恢复 `ai-coding trace overview [--since] [--format]` 让它第一次被用上。`/stats` 与 `/export` 的 **age 与会话顺序**会变（修正，非回归）。 |

### M-G3 · 测试分层

| 项 | 内容 |
|---|---|
| **缺口** | 3799 个用例混跑，慢；且环境伪失败（M-A4）混在里面 |
| **改动方向** | 分 `unit` / `integration` / `live` 三层，CI 分别跑；live 层需 gate 变量 |
| **验收判据** | CI 分层清晰，任一层可单独复跑 |
| **实际落地（2026-10-27）** | ✅ 注册 `integration` / `live` 两个 marker + `--strict-markers`；`addopts` **故意不放 `-m`**（实测：`-m 'not live'` 会让 `pytest tests/test_mcp_stdio.py` 收集 0 条、exit 5）。CI 拆 4 job（quality / unit / integration / live）× 2 版本。`tests/test_live_gate.py` 是 canary：`-m live` 缺 gate 变量时**失败**而不是静默全 skip。顺带修一个既有遗漏：`test_mcp_progress.py` 第一次在 CI 真跑。 |

### M-G4 · 危险命令与权限的端到端基准

| 项 | 内容 |
|---|---|
| **缺口** | `sample_tasks` 的 `bash-deny` 场景是**单例**，不是基准 |
| **改动方向** | 扩成基准集（正例/负例各 N 条），输出指标 |
| **验收判据** | 基准脚本 + 指标 |
| **实际落地（2026-10-27）** | ✅ 判定级基准（`redline_corpus` + `redline_benchmark`）早已成熟；本轮补**规则链层**：`security/permission_corpus.py`（58 行自描述语料）+ `scripts/permission_benchmark.py`（驱动真实 `PermissionGate.check()`，冻结 `(verdict, rule)`，报 FN/FP + 规模下限，退 0/1，进 CI）。**发现并记录一个缺口**：`grep`/`glob` 带绝对越界路径时 ALLOW（`_directory_of` 去掉前导 `/`），作为 `KNOWN_MISS` 打印，不改 `check()`。 |

---

## 8. 领域 H · 产品化 【P3 · 依赖 A-G】

### M-H1 · 安装与分发

| 项 | 内容 |
|---|---|
| **缺口** | 只能 `pip install -e .`；无版本发布、无 lockfile、无 PyPI 包 |
| **改动方向** | 补 `pyproject` 元数据、CHANGELOG、CI 发 PyPI；前端 bun 产物打包 |
| **验收判据** | 干净环境 `pip install ai-coding` 可用 |

### M-H2 · 配置与密钥管理

| 项 | 内容 |
|---|---|
| **缺口** | 密钥只能写 `config.local.yaml`；无密钥轮换、无环境变量优先级文档 |
| **改动方向** | 环境变量优先 + `.env` 支持 + 启动时密钥来源提示 |
| **验收判据** | 文档 + 单测覆盖优先级 |
| **实际落地（2026-10-27）** | ✅ 环境变量 + `${VAR}` 引用（**不加 `.env`、不加 keychain**）：`${VAR}` → 字面值 → provider 约定变量，**env 只填空位不覆盖字面值**；`${VAR}` 缺失/为空是硬错并给出变量名。新增 `ai_coding/providers.py`（5 行 provider 表：默认 `base_url` + 约定环境变量名）与 `config/secrets.py`；`provider` 进 `MODEL_DESTINATION_MEMBERS`（它同时是端点与凭据选择器）。新增 `ai-coding config init` / `config path`（目录 0700、文件 0600），修掉"`cp config.example.yaml config.local.yaml` 反而得到空 models"的 onboarding 断头。子进程脱敏复用 `process_sandbox` 既有的 `--unsetenv` 循环。边界：`ai.websearch_api_key` 与 MCP `headers` 仍是字面值。 |

### M-H3 · IDE 集成

| 项 | 内容 |
|---|---|
| **缺口** | 只有终端 TUI |
| **改动方向** | 复用现有 IPC（`server/service.py`）做 LSP/编辑器插件；或先做 VS Code webview |
| **验收判据** | 编辑器内可起会话、看 transcript |

### M-H4 · 插件 / 技能市场

| 项 | 内容 |
|---|---|
| **现状** | 技能市场（F1）已有设计文档与实现 |
| **缺口** | 未见对外市场 |
| **改动方向** | 技能包的签名/校验/来源可信 |
| **验收判据** | 恶意技能包被拒 |

---

## 9. 推荐起点：**M-A1（+ M-A5）**

### 为什么不是"影响最大"的 M-B1？

M-B1（静态检查回喂）确实是**性价比最高的功能改进**，但它有一个前提：
**你得先能可信地判断"修好了没有"**。而当前：

- `pytest` 有 **10 条失败，其中 8 条你分不清是真坏还是环境**（M-A4）
- `ruff`/`mypy` 有 **10 条问题**，`httpx2` 就藏在里面（M-A1/M-A5）
- 文档与实测**有 3 处不一致**（M-A2/M-A6）

**在这个基线上做 M-B1，你会无法验证它的效果** —— 加了回喂通道，
跑 T2 复测，结果红了也分不清是"没修好"还是"环境又伪失败了"。

### 推荐顺序与理由

| 顺序 | 里程碑 | 理由 |
|---|---|---|
| **1** | **M-A1 + M-A5** | 最小（1 行 + lint 清理）、**立即见效**（`mypy` 少一条 error）、**解锁后续所有验证** |
| **2** | **M-A4** | 让 CI 重新可信 —— 之后每个里程碑的验收才有效 |
| **3** | **M-A2 + A3 + A6** | 契约漂移与文档不一致，一次性清干净（都小） |
| **4** | **M-B1** | 此时才有条件验证"回喂是否真的改善了收敛" |
| **5** | **M-B2 + B3** | 收敛判据 + 重试分类（T3 设计已就绪，可直接实现） |
| 6+ | C / D / E / F / G / H | 按依赖推进 |

### 每个里程碑的固定工作流

```
1. 写失败测试（先红）
2. 最小改动让它变绿
3. 跑：pytest / ruff check . / mypy ai_coding / 相关 scripts/*_cases.py
4. 更新受影响文档（docs/features.md、docs/data-contracts.md）
5. 单独 commit：fix(<里程碑>): <一句话>
6. 在本文件把该里程碑标 ✅ 并附验证输出
```

---

## 10. 进度看板

| 里程碑 | 状态 | 验证输出 |
|---|---|---|
| M-A1 必崩缺陷 | ✅ **已完成** | `pytest tests/test_mcp_oauth.py` **18 passed**；全量 10→**3 failed**；mypy 6→**3**；方案见 [plan-m-a1.md](plan-m-a1.md) |
| M-A2 契约漂移 | ✅ **已完成** | `ruff`/`mypy` 0；`sample_tasks --only mcp-off` → `PASS servers=0`；突变 C 证明原用例恒真空转；漂移点归零（含 superpowers 计划稿）；方案见 [plan-m-a2-a5.md](plan-m-a2-a5.md) |
| M-A3 undo 上报 | ✅ **已完成** | 全量 **3812 tests / 0 failed / 6 skipped**；方案见 [plan-m-a3.md](plan-m-a3.md) |
| M-A4 环境伪失败 | ✅ **已完成** | `pytest` **3806 tests / 0 failures / 0 errors / 6 skipped**；真实 EACCES 验证 3 passed；方案见 [plan-m-a4.md](plan-m-a4.md) |
| M-A5 lint/type 清零 | ✅ **已完成** | `ruff` 6→**0**；`mypy` 3→**0**（152 文件）；突变 A/B 复现原报错 |
| M-A6 文档一致性 | ✅ **已完成** | `memory_cases` **11/11**（深路径 442 字符下仍 11/11）；`features.md` 加口径说明；方案见 [plan-m-a6-n3.md](plan-m-a6-n3.md) |
| M-B1 静态检查回喂 | ✅ **已完成** | `core/static_check.py` + L2 注入真实输出；全链路用例通过；方案见 [plan-m-b.md](plan-m-b.md) |
| M-B2 检索收敛判据 | ✅ **已完成** | `SEARCH` 阶梯 L1(≥4)/L2(≥6)（= `interventions.py` 的 `search_streak_threshold` / `search_streak_l2`）；11+3+1 条用例；信号不解析输出 |
| M-B3 重试分类 | ✅ **早已实现** | `FailureKind` 六档 + AUTH_ERROR 首轮不重试；补 `insufficient_quota` 归 FATAL |
| M-B4 检索预算 | ⚠️ **不采纳** | `budget_pressure` + 收尾已存在；意图分类无可辩护契约 |
| M-B5 收敛可观测 | ✅ **已完成** | `SearchStalled` 事件（iteration/level/calls/streak）自动落 trace jsonl |
| M-C1~C3 恢复与持久化 | ✅ 已完成 | 2026-10-25 |
| M-D1~D3 安全 | ✅ 已完成 | 2026-10-26 |
| M-E1~E4 上下文工程 | ✅ 已完成 | 2026-10-26 |
| M-F1~F3 记忆 | ✅ 已完成 | 2026-10-27 |

| M-G1~G4 可观测与质量 | ✅ 已完成 | 2026-10-27 |
| M-H1~H4 产品化 | ⬜ 未开始 | — |

### 待立项缺陷（§十三）的处置

| 缺陷 | 状态 |
|---|---|
| **N-1** `gitcmd.git()` 合并 stderr 致快照静默失效 | ✅ **已修复**（2026-10-27）—— `merge_stderr=False` + `_SHA_RE` 校验；非 root 真子进程复现脚本 `scripts/verify_snapshot_stderr.py` |
| **N-2** 既有 flaky（`test_tool_surface_warning` / `test_worktree`） | ✅ **已修复**（2026-10-27）—— 根因**实测确认**：前者是环境块内嵌 `时间: <本地时间>`（`request_assembly.py:393`），两次循环跨秒即不同 → 冻结时钟；后者 `timeout=0.001` 与真实 git 赛跑（快机 git 赢）→ 改为注入 `timed_out=True` |
| **N-6** 2 条 rg 用例缺守卫 | ✅ **已修复**（2026-10-27）—— 补 `skipif` + 正向断言"rg 分支真的跑了" |
| **§2.6 新发现** `BwrapBackend.wrap()` 的 fail-open | ✅ **已修复**（2026-10-27）—— 机制缺席时返回 `None`（契约诚实），9 条用例改写/补守卫 |
| **安全残余** CIDR / 运行期根 | ✅ **已修复**（2026-10-27）—— 代理支持 CIDR + 只 resolve 一次；进程层 roots 改活 |

> 方案与三轮方案评审 + 两轮实现评审见 [plan-n-backlog.md](plan-n-backlog.md)。


---

## 十三、M-A4 期间发现的新缺陷（待立项，不在 M-A4 范围）

### N-1 · `gitcmd.git()` 把 stderr 并进 stdout，使解析 stdout 的调用方变脆

| 项 | 内容 |
|---|---|
| **位置** | [gitcmd.py:38](file:///workspace/ai-coding/ai_coding/core/gitcmd.py#L38) —— `return outcome.returncode or 0, (outcome.stdout + outcome.stderr).strip()` |
| **契约** | docstring 明写"returns `(returncode, stdout+stderr)`" —— **合并是有意的** |
| **后果** | `SnapshotStore.capture` 直接 `sha = out.strip()` 当 stash sha 用。git 一旦往 **stderr** 打警告（如读不到用户 gitconfig：`warning: unable to access '/root/.config/git/ignore': Permission denied`），`capture` 拿到的就是**警告文本**，`update-ref` 失败 → `capture` 返回 `None` → **快照静默失效**（实测：`capture -> None`、`exists -> False`） |
| **触发条件** | 任何让 git 往 stderr 输出警告的环境（无 HOME / 无 gitconfig / 权限受限） |
| **为何不在 M-A4 修** | 这是**产品缺陷**，不是测试缺陷；M-A4 的交付物是"测试可移植化"。修它需要改 `gitcmd` 的输出语义或让调用方校验 `^[0-9a-f]{40}$` —— 属于独立里程碑 |
| **建议修法** | ① `gitcmd.git()` 分离 stdout/stderr（改契约，需评估两个调用方）；或 ② 调用方校验 sha 形状，不符则视为失败并**明确报错**（而非静默返回 None） |
| **实测证据** | `scripts/verify_real_eacces.py` 在 `HOME` 未设时：`capture -> None` → 干净中止；设 `HOME=/tmp` 后：`capture -> 'refs/ai-coding/undo/s/1'` |

### N-2 · 既有 flaky 用例（评审实测发现）

`tests/test_tool_surface_warning.py::test_the_warning_never_changes_the_request`（`:210`）——
全量套件 5 次运行出现 1 次失败；单独循环 40 次复现 1 次（断言两侧内容肉眼相同 → 引擎请求记录存在竞争）。
**与 M-A4 改动无关**，但它会让"让 CI 重新可信"的目标周期性落空，建议纳入 M-A5 收口。

### N-3 · `mcp_echo_server.py` 引用了当前 mcp 版本不存在的模块 —— ✅ 已修复（2026-10-25）

| 项 | 内容 |
|---|---|
| **位置** | `scripts/mcp_echo_server.py:15`、`scripts/mcp_many_tools_server.py:23`、`scripts/mcp_progress_server.py:18`（**同一缺陷 ×3**） |
| **现象** | `ModuleNotFoundError: No module named 'mcp.server.mcpserver'`（真机实跑 MCP 服务器返回 `status: failed`） |
| **根因** | 这是 **mcp 2.x** 的模块（M-A1 收紧为 `>=1.24,<2` 后不存在）。`fastmcp.server.MCPServer` 只是 lowlevel `Server` 的别名 —— **没有 `.tool()`，`run()` 也不是 `transport=` 形式**，换 import 路径没用 |
| **实际修法** | 改用官方高层 API `mcp.server.fastmcp.FastMCP`（三个脚本各 2 行） |
| **影响** | `tests/test_mcp_stdio.py`、`test_mcp_end_to_end.py`、`test_mcp_progress.py` 平时被 `AI_CODING_MCP_STDIO=1` 门控故 skip（未暴露）；**CI 里前两个 step 会红**（`ci.yml:45/51` 设了该变量） |
| **验收** | `AI_CODING_MCP_STDIO=1 pytest` 三个文件 → **6 passed**（改前 1+3+1 failed）；`scripts/r7_cases.py` 的 MCP 档随之恢复 |
| **评审副产物** | 代码评审发现第三个夹具 —— 我最初只 grep 了 `mcp_echo_server`，漏了它（见 plan §13.4 I-3） |

### N-6 · 2 条 rg 用例缺 rg 守卫（本机无 rg 时静默假绿）

| 项 | 内容 |
|---|---|
| **位置** | `tests/test_m4_security.py:461`（`test_grep_include_dotenv_still_filters_credentials`）、`tests/integration/test_r17_parity.py:206`（`test_both_runners_answer_the_same_question`） |
| **现象** | rg 缺席时**仍然通过，但根本没验证到 rg 分支**：前者 `for use_rg in (False, True)` 且不注入 runner，rg 缺席时 `use_rg=True` 那轮拿到错误串，而断言只查 `"TOKEN=secret" not in result.output`；后者 autodetect，`real` 就是兜底 → 兜底和自己比，恒绿 |
| **实测** | `PATH=/tmp/norg`（不含 `rg`）：这两条 `2 passed` |
| **为何现在不修** | CI 已装 rg（N-5），这两条在 CI 里已真跑 rg 分支；加守卫只能帮到"本机没装 rg 的开发者"，属 N-5 范围外的加固 |
| **建议修法** | 与 `test_r17_parity.py:183/366` 同款 `@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep is not installed")`；`test_m4_security.py:461` 是循环，需在 `use_rg=True` 那轮 `pytest.skip(...)` |
