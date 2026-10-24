# 缺口路线图 · 按领域划分的里程碑

> 来源：[评估报告](../ai-coding-评估报告.md) §六「缺漏清单」+ 本次全部实测（eval-00..07）。
> 用途：逐个里程碑实现。**每个里程碑都可独立交付、独立验收**。
> 约定：里程碑编号 `M<领域序号>-<子项序号>`；验收判据必须**可机检**（命令 + 判据）。

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

### M-A1 · 必崩缺陷修复

| 项 | 内容 |
|---|---|
| **缺口** | `ai_coding/core/mcp.py:787` 的 `import httpx2`（应为 `httpx`） |
| **现状证据** | `mypy`：`Cannot find implementation or library stub for module named "httpx2"`；PyPI 无此包 |
| **触发条件** | 任何走「远程 streamable-HTTP MCP 服务器 + OAuth」的路径 |
| **后果** | `ModuleNotFoundError`，该分支 100% 崩溃 |
| **改动** | 1 行：`import httpx2` → `import httpx`，`httpx2.AsyncClient` → `httpx.AsyncClient` |
| **验收判据** | ① `mypy ai_coding` 该条 error 消失；② 单测 `tests/test_mcp_oauth.py` 从 `assert False` 转为通过（需模拟远端端点）；③ 新增一条**断言该分支真的 import 到 `httpx`** 的回归测试 |

### M-A2 · 契约漂移收敛（`open_all()` 返回值）

| 项 | 内容 |
|---|---|
| **缺口** | `MCPManager.open_all()` 标注 `-> None` 且返回 `None`，但 `docs/features.md:542` 写"返回空列表"；`docs/r1a-6-analysis.md:251` 亦写 `[]` |
| **两处消费方已坏** | `scripts/sample_tasks.py:86`（已评估期修复）、`scripts/r7_cases.py:353/424` |
| **决策（二选一，建议选 ②）** | ① 改文档承认 `None`；② **让 `open_all()` 返回工具列表**（对齐 R1a-6 原始设计，`for tool in await open_all()` 的写法才成立） |
| **验收判据** | ① `docs/features.md` / `docs/r1a-6-analysis.md` 与代码一致；② `scripts/sample_tasks.py`、`scripts/r7_cases.py` 在不加 `or []` 的情况下通过；③ `ruff`/`mypy` 0 新增问题 |

### M-A3 · undo 部分回滚未上报

| 项 | 内容 |
|---|---|
| **缺口** | `tests/integration/test_r17_defects.py::test_undo_names_the_files_it_could_not_restore` 失败：`{'ok': True, 'files': ['ro/stuck.txt'], 'reason': ''}` —— 报告"成功"却隐藏了部分回滚 |
| **影响** | 用户以为全恢复了，实际有文件没恢复 |
| **改动方向** | `core/snapshots.py` / `agent_loop.undo_last_turn`：`failed` 非空时不得报 `ok: True`，`reason` 必须点名未恢复文件 |
| **验收判据** | ① 该 pytest 用例转绿；② 新增"只读目录下 undo"的回归用例；③ 文档 `docs/data-contracts.md` 的 undo 契约同步 |

### M-A4 · 环境伪失败的隔离（CI 可信度）

| 项 | 内容 |
|---|---|
| **缺口** | 10 条 pytest 失败里 **8 条是环境伪失败**：2 条因 `root` 可写/可删一切（`test_grant_store`、`test_snapshots`），6 条因 MCP OAuth 需外网 |
| **影响** | 真缺陷被噪音淹没；CI 红了也分不清是不是真坏 |
| **改动方向** | ① 权限类用例加 `@pytest.mark.skipif(os.geteuid() == 0, ...)` 或改用非 root 用户运行；② MCP OAuth 用例加 `@pytest.mark.skipif(not os.environ.get("AI_CODING_MCP_OAUTH"), ...)`，并在 CI 里显式开启 |
| **验收判据** | ① `pytest -q` 在 root 沙箱内 **0 失败**（或失败项全部显式标注为环境依赖）；② CI 日志能一眼区分"真失败 / 跳过" |

### M-A5 · lint / type 门禁清零

| 项 | 内容 |
|---|---|
| **缺口** | `ruff check .` **4 errors**（全在 `scripts/`：import 排序 ×2、未用 `shutil`、未用 `wire`）；`mypy ai_coding` **6 errors / 3 files** |
| **mypy 明细** | `core/events.py:581` 冗余 cast；`infra/deadline.py:171` arg-type；`core/mcp.py:637` `mcp.client.auth` 无 `AuthorizationCodeResult`；`core/mcp.py:668` 返回类型；`core/mcp.py:787` `httpx2`（M-A1 修）；`core/mcp.py:789` 解包数量 |
| **改动方向** | `ruff check . --fix` 清 3 条；`mcp.py:637/668/789` 按 `mcp` 库实际 API 修正（版本漂移） |
| **验收判据** | `ruff check .` = 0；`mypy ai_coding` = 0；CI 增加这两步为硬门禁 |

### M-A6 · 文档与实测不一致的修订

| 项 | 内容 |
|---|---|
| **缺口** | `memory_cases` **case7**：文档/用例期望每轮收割批次 ≤3，实测 `[3, 4]`；**case11**：文档记缓存前缀 92%，实测 87% |
| **改动方向** | 先定位是**代码错**还是**文档错**：case7 若代码多喂一条消息 → 修 `memory_turn.py` 的增量窗口；case11 若属正常波动 → 把文档改成"≥85%"并写明测量条件 |
| **验收判据** | `scripts/memory_cases.py` **11/11**；`docs/features.md` 的数字与实测一致，且注明测量日期与环境 |

---

## 2. 领域 B · 收敛与编排 【P0 · 依赖 A】

> 目标：**解决本次评估最大的短板** —— T2 实测 63 次工具调用 / 900 秒 / 零结论。

### M-B1 · 静态检查结果回喂通道 ⭐（最高性价比）

| 项 | 内容 |
|---|---|
| **缺口** | mypy/ruff 早就能报出 `httpx2`，但**结果没有回到模型面前**。模型只能逐文件 grep |
| **改动方向** | 在 `core/agent_loop.py` 的请求装配处加一条：**当模型连续 N 次检索类工具调用（`grep`/`glob`/`read`）无新命中时**，自动注入一条合成提示，附上 `ruff`/`mypy` 的**增量输出**（或至少提示"先跑静态检查"） |
| **验收判据** | 用 T2 同题复测：`ai-coding` 应在 **≤5 次工具调用、≤120s** 内给出 `mcp.py:787` 的结论；新增回归用例 `tests/test_convergence.py` |

### M-B2 · 检索收敛判据（"够用就停"）

| 项 | 内容 |
|---|---|
| **缺口** | `grep`/`glob` 连续无新发现时没有"换策略"提示。项目**已有** `[strategy-gate]`（针对工具全失败），但**没有针对"工具成功但无进展"** |
| **改动方向** | 扩展 `core/interventions.py`：新增 `no_new_findings` 阶梯 —— 连续 K 次检索未命中 → 提示"扩大/缩小范围或换关键词"；再 K 次 → 提示"直接跑静态检查"；再 K 次 → 主动收尾并给交接报告 |
| **验收判据** | ① 单测覆盖三级阶梯；② T2 复测收敛；③ `docs/features.md` 新增该阶梯的说明 |

### M-B3 · 重试分类（对标 Codex `retry_delay`）

| 项 | 内容 |
|---|---|
| **缺口** | `ai/openai_engine.py::_request_with_retry` 只对 `RateLimitError/ServiceUnavailableError/APIConnectionError/TimeoutExceeded` **统一指数退避** —— 没有"绝不重试"这一档 |
| **改动方向** | 加 `RetryStrategy { NO_RETRY, SERVER_HINT, EXPONENTIAL_BACKOFF }` + `RETRY_LADDER` 映射（**完整设计见 [eval-06 T3](eval-06-comparison.md)**，含代码骨架） |
| **三档分类** | **NO_RETRY**：`ContextWindowExceeded`、`InvalidRequest`、认证失败、配额用尽；**SERVER_HINT**：429（有 `Retry-After` 就用）；**EXPONENTIAL_BACKOFF**：`Stream`、`Timeout`、`ConnectionFailed`、5xx |
| **关键约束** | 重试等待**计入** `turn_timeout_seconds` 预算（`deadline.py` 已有硬超时）；**不得**占用 `max_tool_iterations` |
| **验收判据** | ① 三档各有单测（用 fake engine 注入各类异常）；② 端到端：注入 401 → **1 次调用即终止**；注入 503 → 退避重试后成功 |

### M-B4 · 检索类任务的预算约束

| 项 | 内容 |
|---|---|
| **缺口** | `max_tool_iterations=30` + `max_auto_continuations=4` = 单回合最多 150 个模型步，T2 就是这么烧掉 900s 的 |
| **改动方向** | ① 对"纯检索/诊断"意图（可用 `turn_policy` 判定或让模型自报）用**更紧的迭代预算**；② 给 `/tasks` 里的长任务加**成本上限**（`turn_token_budget` 已有，需在 UI 可见） |
| **验收判据** | T2 复测在 **≤180s** 内要么给结论、要么主动收尾并给交接报告 |

### M-B5 · 收敛可观测

| 项 | 内容 |
|---|---|
| **缺口** | 现在看不到"这个回合搜索了多少次、有没有进展" |
| **改动方向** | trace 里加 `search_exhausted` / `no_new_findings` 事件；`/trace` 面板显示"检索次数 / 命中数 / 是否收敛" |
| **验收判据** | T2 场景的 trace jsonl 里能读到收敛相关事件 |

---

## 3. 领域 C · 恢复与持久化 【P1 · 依赖 A】

### M-C1 · step 级 durable intent

| 项 | 内容 |
|---|---|
| **缺口** | 恢复粒度是**会话级**（`world.find_armed_session` 只捞 `armed_profile ∈ {plan, goal}`）。对标：opencode **write-ahead claim**（step 级）、Codex **rollout 写恢复** |
| **改动方向** | 会话 JSON 增加 step 级 intent 记录（已执行的工具调用 + 结果摘要），重启后可从**最近已完成 step** 续跑，而非整段重跑 |
| **验收判据** | ① 单测：模拟进程在 step N 后崩溃 → 重启续跑且**不重复执行已完成的工具**；② `docs/data-contracts.md` 更新会话 schema |

### M-C2 · 记忆并发写安全

| 项 | 内容 |
|---|---|
| **缺口** | `MemoryStore._mutate` 在 `_save` **之前**放锁，`_save` 无锁读 `self._notes` → 两线程落到同一 `<path>.tmp`，`os.replace` 后另一线程继续写正式文件 → **文件变非法 JSON，加载读到 0 条** |
| **复现** | 需 `threading.Barrier.wait()` 强制同步，4 线程 × 30 次写，25 次里 5-6 次损坏（文档已自述） |
| **改动方向** | `_save` 进锁（文档说"会改动每个调用方的持久化形状"，需评估）；或改用 `fcntl.flock` 跨进程文件锁 |
| **验收判据** | 新增 `test_memory_concurrent_write_never_corrupts`：强制同步下 25 次写 **0 损坏**，且重载后条数一致 |

### M-C3 · 崩溃后续跑的验收门

| 项 | 内容 |
|---|---|
| **缺口** | `resume_count >= 5` 才 disarm，期间没有"这次续跑有没有真的前进"的判据 |
| **改动方向** | 续跑时比较"续跑前后产出"，无前进则提前 disarm 并给交接报告 |
| **验收判据** | 单测：续跑无前进 → 提前终止 + 交接报告 |

---

## 4. 领域 D · 安全 【P1 · 依赖 A】

### M-D1 · OS 级沙箱

| 项 | 内容 |
|---|---|
| **缺口** | `security/sandbox.py` **只做路径限制**（拦 `..`/绝对路径/符号链接逃逸）。对标 Codex：`execpolicy`（纯策略）+ `sandboxing`（纯机制）+ 跨平台后端（Seatbelt / bwrap+landlock / MXC） |
| **改动方向** | ① 先做 **Linux landlock/bubblewrap**（bwrap 已有依赖先例）；② 把策略判定（ALLOW/WARN/DENY）与机制（真正隔离）**分离**，`bypass_sandbox` 要求显式 allow |
| **验收判据** | ① 单测：沙箱内进程**写不到**工作区外（真实子进程验证，不是 mock）；② `ruff`/`mypy` 0 |

### M-D2 · 危险命令判定的可解释性

| 项 | 内容 |
|---|---|
| **缺口** | `security/bash_redlines.py` 用正则/tree-sitter 判危险命令，但**误报/漏报无基准** |
| **改动方向** | 建一个**危险命令语料基准**（正例：`rm -rf /`、`sudo`、`dd if=`、`> /dev/sd`、强制 push…；负例：`rm -rf ./build`、`sudo` 出现在引号里…），输出 FP/FN 指标 |
| **验收判据** | 基准脚本 + 指标表；FN = 0（漏报零容忍），FP 有界 |

### M-D3 · 权限授予的最小化

| 项 | 项 | 内容 |
|---|---|---|
| **缺口** | `security/grants.py` 的"always"记忆是**目录级**的；`extra_workspace_roots` 是启动时一次性展开 |
| **改动方向** | 到期/撤销机制；UI 可见"当前会话被授予了哪些权限" |
| **验收判据** | 单测覆盖授予/撤销/到期；`/permissions` 面板可查 |

---

## 5. 领域 E · 上下文工程 【P1 · 依赖 A】

### M-E1 · L3 语义显式化

| 项 | 内容 |
|---|---|
| **缺口** | **本次实测最重要的设计发现**：`persist_large_outputs` 只检查**消息列表末尾连续的 `role=="tool"` 段**（[context_compression.py:251-257](file:///workspace/ai-coding/ai_coding/core/context_compression.py#L251-L257)），所以**回合结束时 L3 永远不生效** —— 它是**回合中段**护栏 |
| **实测证据** | eval-04：10 轮真实对话后 `persisted=0`；只有用真实函数 + 合成输入才触发 |
| **改动方向** | ① 文档写明"L3 是回合中段护栏，会话 JSON 里观测不到"；② **或者**让它也作用于非尾部的大结果（需评估成本） |
| **验收判据** | `docs/features.md` 与 `docs/data-contracts.md` 更新；新增"回合中段触发 L3"的可观测测试 |

### M-E2 · L2/L4 顺序导致的计数不稳定

| 项 | 内容 |
|---|---|
| **缺口** | 管线顺序 L3→L1→**L2→L4**，L4 摘要会先吃掉中段（含旧工具结果），导致"压缩后 elided 数量"在 0/1 间跳动 —— **不是稳定断言** |
| **改动方向** | 明确该顺序是**刻意的**并写进文档；或让 L2 在 L4 之后跑（需评估对摘要质量的影响） |
| **验收判据** | 文档说明 + 一条"顺序不变量"单测 |

### M-E3 · 压缩预算可观测

| 项 | 内容 |
|---|---|
| **缺口** | `r7_cases::l2_long_history_stays_under_budget` 未达预算（`est tokens=7058 envelopes=67 of 280 messages`） |
| **改动方向** | `/trace` 面板显示每层压缩前后的 token 估算与命中数；超预算时给提示 |
| **验收判据** | 该 r7 用例转绿或明确标注为已知取舍 |

### M-E4 · 压缩前缀缓存友好度

| 项 | 内容 |
|---|---|
| **缺口** | 实测：记忆放系统提示前缀命中 **7%**、放尾部 **94%**；每轮压缩历史中段只有 26%（改按预算分页后 92%） |
| **改动方向** | 把"按预算分页"推广到**所有**注入通道；对标 opencode 的**预计算摘要**（上下文到 85% 后台生成，复用 prompt cache） |
| **验收判据** | 基准脚本量化前缀命中率；目标 ≥90% |

---

## 6. 领域 F · 记忆 【P2 · 依赖 A】

### M-F1 · 语义档可用化

| 项 | 内容 |
|---|---|
| **现状** | 实测 paraphrase@3 **0/46**（词面）→ 开语义档后 **6/46**，可达 R@3 0.99→1.00 |
| **缺口** | 默认关（因 deepseek 无 `/embeddings`）；`min_cosine=0.60` 只在 text-embedding-v4 上标定过 |
| **改动方向** | ① 加"端点是否支持 embeddings"的**自动探测**，支持则默认开；② 提供基准脚本供换 provider 时重标 |
| **验收判据** | 探测逻辑单测；基准脚本可复跑 |

### M-F2 · 场景标签治理

| 项 | 内容 |
|---|---|
| **缺口** | 实测采纳率 93%（很好），但**实测推翻了自己两个说法**：① 场景标签不是查询词（是过滤器不是召回器）；② JSON 片段会被当记忆存进去（已修） |
| **改动方向** | 把这两条实测结论写回 `docs/features.md` 的召回契约；补"标签治理"（过多标签时的合并策略） |
| **验收判据** | 文档与实测一致；标签数上限策略有单测 |

### M-F3 · 召回预算实测

| 项 | 内容 |
|---|---|
| **缺口** | `memory.recall_timeout_seconds=2.0` 对实测 ~5ms "虚位以待" |
| **改动方向** | 建召回延迟基准（随库规模增长），让超时阈值有实测依据 |
| **验收判据** | 基准脚本 + 指标表 |

---

## 7. 领域 G · 可观测与质量 【P2 · 依赖 A】

### M-G1 · 基准回归门禁

| 项 | 内容 |
|---|---|
| **缺口** | `scripts/retrieval_cases.py --check` 是**本地门禁，CI 目前不调**（文档自述） |
| **改动方向** | 接入 CI；`memory_cases`、`r18_acceptance`、`trace_cases` 一并接 |
| **验收判据** | CI 里这些脚本任一不达标 → 红 |

### M-G2 · 成本看板

| 项 | 内容 |
|---|---|
| **缺口** | `core/aggregate.py::build_report` 已能算 token，但 `--since` 依赖目录 mtime（rsync 会漂移）；且已无 CLI 入口 |
| **改动方向** | 修 `--since` 的漂移（改用文件内时间戳）；恢复一个 CLI 或做成 `/usage` 面板 |
| **验收判据** | rsync 后的目录不漂移；面板可查 |

### M-G3 · 测试分层

| 项 | 内容 |
|---|---|
| **缺口** | 3799 个用例混跑，慢；且环境伪失败（M-A4）混在里面 |
| **改动方向** | 分 `unit` / `integration` / `live` 三层，CI 分别跑；live 层需 gate 变量 |
| **验收判据** | CI 分层清晰，任一层可单独复跑 |

### M-G4 · 危险命令与权限的端到端基准

| 项 | 内容 |
|---|---|
| **缺口** | `sample_tasks` 的 `bash-deny` 场景是**单例**，不是基准 |
| **改动方向** | 扩成基准集（正例/负例各 N 条），输出指标 |
| **验收判据** | 基准脚本 + 指标 |

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
| M-A1 必崩缺陷 | ⬜ 未开始 | — |
| M-A2 契约漂移 | ⬜ 未开始 | — |
| M-A3 undo 上报 | ⬜ 未开始 | — |
| M-A4 环境伪失败 | ⬜ 未开始 | — |
| M-A5 lint/type 清零 | ⬜ 未开始 | — |
| M-A6 文档一致性 | ⬜ 未开始 | — |
| M-B1 静态检查回喂 | ⬜ 未开始 | — |
| M-B2 检索收敛判据 | ⬜ 未开始 | — |
| M-B3 重试分类 | ⬜ 未开始 | — |
| M-B4 检索预算 | ⬜ 未开始 | — |
| M-B5 收敛可观测 | ⬜ 未开始 | — |
| M-C1~C3 恢复与持久化 | ⬜ 未开始 | — |
| M-D1~D3 安全 | ⬜ 未开始 | — |
| M-E1~E4 上下文工程 | ⬜ 未开始 | — |
| M-F1~F3 记忆 | ⬜ 未开始 | — |
| M-G1~G4 可观测与质量 | ⬜ 未开始 | — |
| M-H1~H4 产品化 | ⬜ 未开始 | — |
