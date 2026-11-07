# 多批次缺陷修复总计划（2026-11-07）

## 0. 摘要

本计划把两轮审查（压缩机制专项 + 全项目七线）中发现、**尚未修复的 42 项问题**（可拆为 51 个可执行任务）归类为 **6 个批次**，分 **3 个波次**执行。每个批次严格走五阶段流水线：

```
调研方案 → 方案评审 → 修复 → 实现验证 → 代码评审
```

每个阶段由独立子智能体承担、各自落盘产物、阶段之间有硬门禁；任一阶段不通过则回退上一阶段（最多 2 轮），仍不通过则停下来向用户报告。

两项已拍板的决策：

- **MCP 进程沙箱**：新增 `mcp.sandbox` 开关，**默认关闭**；开启后 stdio server 走 `process_sandbox` 包装，环境不支持时回退直连并上报 Degraded。
- **提交节奏**：全部批次完成、统一评审通过后 **一次性提交**（不 push）。过程中用 `git diff` 快照做回退点，不使用中间 commit。

---

## 1. 当前状态

### 1.1 已修复（16 项，5 个 commit + 1 个评审跟进）

| commit | 范围 |
|---|---|
| `5e641bc` | 压缩：无 checkpoint 时 L0 视图不可复现、冷启动测量忽略 L0（两个 P0） |
| `3f27ae3` | 子代理结算后无法续聊（P0）、rewrite 后审批未重判、guard 计数互清、`plan_verify_items` 层级、`thinking_retries` 未重置、TUI gate 缺 resolver |
| `d9606b1` | 跨会话 stale-read、skill approve/reject 路径穿越、损坏会话拖垮 `/resume` |
| `340aec8` | 子代理崩溃丢改动、取消无上界、未命名异常跳过收尾 |
| `3f5d350` | `max_result_data_bytes` 接线、bash 日志头尾、steering/write 配置接线、`/model`、`/skills`、删 `auto_process_tool_results` |
| `fd5a929` | 评审跟进：结算顺序不变量 + journal 折叠测试 |

当前门禁：全量 pytest 通过、ruff 全绿、mypy 186 文件 0 错误。

### 1.2 待修复清单（42 项 / 51 个任务）

严重度：**P1** = 会导致越权、丢数据、功能静默失效或成本失控；**P2** = 健壮性/一致性/可观测性。

#### A. MCP（11 项）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| MCP-01 | stdio server 直接 spawn，绕过进程沙箱 | P1 | `core/mcp.py:869-879`、`server/world.py:269` |
| MCP-02 | `close` 超时后 cancel 但不 await，子进程可能残留 | P1 | `core/mcp.py:503-512` |
| MCP-03 | 单次调用超时只放弃调用，会话仍复用 | P1 | `core/mcp.py:963-980,1503-1506` |
| MCP-04 | 远端文本 `output` 无任何裁剪 | P1 | `core/mcp.py:911-918` |
| MCP-05 | `list_tools()` 只拉一页，无 `list_changed` | P2 | `core/mcp.py:459` |
| MCP-06 | remote 无法手动重连；失败文案错报 "no command" | P2 | `core/mcp.py:1446,1554` |
| MCP-07 | 超时/传输/远端 error 全映射 `mcp_error` | P2 | `core/mcp.py:978,989,1010`、`domain/error_codes.py:214` |
| MCP-08 | 重连风暴：断开后 delay 立即重置 1s | P2 | `core/mcp.py:1234,1254,1258,1260` |
| MCP-09 | OAuth 回调端口占用即抛，不回退 | P2 | `core/mcp.py:613-620`、`config/models.py:748` |
| MCP-10 | `mcp_chars_warn` 永不触发；子 agent 无 MCP 且无说明 | P2 | `core/agent_loop.py:1824`、`tools/builder.py:140-153` |
| MCP-11 | `test_mcp_progress.py` 传入已不存在的字段，被静默忽略 | P2 | `tests/test_mcp_progress.py:44-53` |

#### B. 上下文工程（7 项 / 9 个任务）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| CTX-01 | 裸路径/目录引用块不扣预算，可产生无界 tail（压缩触及不到） | P1 | `core/request_assembly.py:765-791,809-816` |
| CTX-02 | 裸路径为算行数整读文件 | P1 | `core/request_assembly.py:202-222` |
| CTX-03 | 显式 @ 先整读再截断（GB 级/二进制内存峰值） | P1 | `core/request_assembly.py:155-180` |
| CTX-04 | plan 文本三份重复；同一 skill 既推荐又全量注入 | P2 | `ai/prompt.py:154-186`、`core/request_assembly.py:535,550` |
| CTX-05 | 单条长记忆超预算 | P2 | `memory/service.py:714-722,891-976` |
| CTX-06 | `budget.py` 多个指标算了没人用 | P2 | `core/budget.py:157-196`、`core/turn_report.py:159` |
| CTX-07a | closing 步 `tools=[]` 仍按全量 schema 估算 | P2 | `core/agent_loop.py:2916-2927` |
| CTX-07b | canonical 不含图像 token | P2 | `core/trace.py:173-181` |
| CTX-07c | `history_chars` 不含工具参数 | P2 | `core/turn_report.py:152` |

#### C. 压缩遗留（5 项）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| CMP-01 | 文件重注入只活一轮请求，但文案称"不必重新读取" | P1 | `core/context_compression.py:1707-1771` |
| CMP-02 | `checkpoint_view` 每轮回扫全量，约 225ms/轮 | P1 | `core/context_compression.py:1584-1658` |
| CMP-03 | `_MICROCOMPACT_PROTECT_TOKENS=40k` > 48k 窗口的 ceiling(32k) | P1 | `core/context_compression.py:366` |
| CMP-04 | 信封可能比正文更大（`_NOTE_ALLOWANCE` 写死 160） | P2 | `core/context_compression.py:56,344-345` |
| CMP-05 | provider 低报/冻结时 gate 被蒙蔽（实测 4,978 vs 344k 字符） | P1 | `core/request_assembly.py:248-250` |

#### D. Skill（6 项）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| SKL-01 | SOP 自动推荐不查 deny | P1 | `core/sop_router.py:331-336,352-360` |
| SKL-02 | 安装只查顶层软链，`copytree(symlinks=False)` 解引用嵌套软链 | P1 | `skills/install.py:45-60,140` |
| SKL-03 | 注入无预算：hard 步骤、@mention 全文、`format_block` 描述 | P1 | `core/sop_router.py:638-661`、`core/request_assembly.py:892-899` |
| SKL-04 | `<skills>` 双重包裹 | P2 | `skills/catalog.py:38-41,165`、`ai/prompt.py:187-190` |
| SKL-05 | `skill install ./path` 被误判为 git 仓库 | P2 | `skills/install.py:150-161` |
| SKL-06 | review cursor 仅在 created=True 前进；Dendritic 休眠；phase 未校验 | P2 | `core/agent_loop.py:2039-2040`、`core/sop_router.py:329,363` |

#### E. 编排 / 计划（5 项）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| ORC-01 | 计划实施回合无任何上限 | **决策：保持不设上限**，仅文档化 | `core/turn_policy.py:251-266`、`core/agent_loop.py:1681-1684` |
| ORC-02 | `max_strategy_nudges` 是无效旋钮 | P2 | `core/turn_policy.py:72,106,225`、`core/interventions.py:99` |
| ORC-03 | `search_streak_threshold=0` 未真正关闭 gate | P2 | `core/interventions.py:167-168,181-182,343-351` |
| ORC-04 | 最后一轮仍注入 POST nudge，永不读取 | P2 | `core/agent_loop.py:1418-1437,2765` |
| ORC-05 | 阶梯 FINISH 统一记为 `MAX_ITERATIONS` | P2 | `core/turn_close.py:64-69` |

#### F. 子代理 / 后台（5 项）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| SUB-01 | 产物路径指向已删除的 worktree | P2 | `tools/subagent_tool.py:87-89`、`core/worktree.py:230-234` |
| SUB-02 | 停机不关 runner/子 loop/worktree；空闲句柄不剪枝；`Scheduler.stop` 裸 `create_task` | P2 | `server/world.py:56-83`、`core/scheduler.py:671`、`core/subagent.py:1319-1335` |
| SUB-03 | 同 `idempotency_key` 并发可双跑 | P2 | `core/scheduler.py:153-186,250-270` |
| SUB-04 | 自定义 agent 对模型不可见（无清单注入） | P2 | `agents/registry.py:223-255`、`ai/prompt.py:129-151` |
| SUB-05 | 子代理 gate 固定主 workspace，worktree 内绝对路径授权解析不到 | P2 | `core/subagent.py:1696-1699`、`security/permission_gate.py:811-835` |

#### G. 工具 / 会话 / 可观测（3 项 / 10 个任务）

| ID | 问题 | 级别 | 锚点 |
|---|---|---|---|
| X-01a | `shell_read` 网络升级绕过审批 | P2 | `tools/shell_read_tool.py:74-82`、`security/permission_gate.py:542-545` |
| X-01b | `write(mode=append)` 非原子、无版本复核 | P2 | `tools/write_tool.py:227-229` |
| X-01c | `overwrite` 前缀截断漏检（`offset<=0` 直接放行） | P2 | `tools/write_tool.py:138-140` |
| X-01d | `allow_once` 不是一次性 | P2 | `security/permission_gate.py:300,866-880`、`core/dispatch.py:949` |
| X-02a | trace 幽灵目录 `traces/unknown` | P2 | `server/world.py:268`、`core/trace.py:388,684-699` |
| X-02b | `tracing.dir` 双源（overview 硬编码） | P2 | `cli.py:324-325,631-632` |
| X-02c | `Braid` 吞 observer 异常且不暴露 failures | P2 | `core/observers.py:74-93`、`core/turn_report.py:206` |
| X-03a | 后台 job 记录/日志无保留上限 | P2 | `core/jobs.py:719-736,792-849` |
| X-03b | `session_service.mark_*` 旁路锁，跨进程 last-write-wins | P2 | `service/session_service.py:127,329` |
| X-03c | `/rewind` 不存在（实为 `/undo`+`/redo`） | P2 | `app/slash.py:22-51`、`app/commands.py:212-236` |

---

## 2. 批次划分与并行波次

### 2.1 文件所有权矩阵

同波次内不允许两个批次改同一文件；跨波次可以复用。

| 波次 | 批次 | 独占文件 |
|---|---|---|
| W1 | **B1 MCP** | `core/mcp.py`、`tools/mcp_call.py`、`config/models.py`(MCP 段)、`server/world.py`(MCP 构造/关闭) |
| W1 | **B2 上下文+压缩** | `core/request_assembly.py`、`core/context_compression.py`、`core/file_mentions.py`、`core/budget.py`、`core/turn_report.py`、`core/trace.py`、`memory/service.py` |
| W1 | **B5 子代理** | `core/subagent.py`、`core/scheduler.py`、`core/worktree.py`、`tools/builder.py`、`ai/prompt.py`、`tools/subagent_tool.py` |
| W2 | **B3 Skill** | `core/sop_router.py`、`skills/*.py`、`core/skill_review.py`、`core/request_assembly.py`、`ai/prompt.py` |
| W2 | **B4 编排/计划** | `core/turn_policy.py`、`core/interventions.py`、`core/agent_loop.py`、`core/turn_close.py`、`core/task_driver.py`、`config/models.py` |
| W2 | **B6 工具/会话/可观测** | `tools/shell_read_tool.py`、`tools/write_tool.py`、`core/dispatch.py`、`security/permission_gate.py`、`core/observers.py`、`core/jobs.py`、`service/session_service.py`、`app/commands.py`、`app/slash.py`、`cli.py`、`server/world.py`、`tools/builder.py` |
| W3 | — | 统一评审 + 全量验证 + 单次提交 |

**冲突规避**：`request_assembly.py` 被 B2（W1）与 B3（W2）先后修改；`ai/prompt.py` 被 B5（W1）与 B3（W2）先后修改；`config/models.py` 被 B1（W1）与 B4（W2）先后修改；`server/world.py` 被 B1（W1）与 B6（W2）先后修改。均在不同波次，顺序执行即可。

### 2.2 波次推进规则

- W1 三个批次**并行**（子代理并发上限 5，留 2 个余量给评审）。
- W1 全部通过代码评审后，跑一次**全量 pytest + ruff + mypy**，再释放 W2。
- W2 三个批次并行，规则同上。
- 任一批次卡住（2 轮评审仍不通过），该波次其余批次继续，卡住项进入 `DEFERRED.md` 并在最终报告里列出。

---

## 3. 五阶段流水线（核心设计）

### 3.1 阶段定义

每个批次建目录 `.trae/documents/remediation/<batch>/`，五份产物依次生成。

| 阶段 | 执行者 | 产出 | 门禁 |
|---|---|---|---|
| **S1 调研方案** | `general-purpose` 子代理（可读可写文档） | `01-research.md` | 覆盖本批每个 issue；每个 issue 有推荐方案，无 TBD |
| **S2 方案评审** | `Explore` 子代理（只读，对抗式） | `02-design-review.md` | 给出 `PASS` 或 `CHANGES` + 逐条理由 |
| **S3 修复** | `general-purpose` 子代理（写代码） | 代码 + `03-implementation.md` | 定向测试通过 + ruff + mypy；每 issue ≥1 条新测试 |
| **S4 实现验证** | 主智能体（我） | `04-verification.md` | diff 与 01 方案逐条对齐；无越界改动；本波全量测试通过 |
| **S5 代码评审** | `Explore` 子代理（只读，只看 diff） | `05-code-review.md` | `APPROVE` 或 `REQUEST-CHANGES` + P0/P1/P2 |
| 回退 | — | — | S5 不通过 → 回 S3（最多 2 轮）；S2 不通过 → 回 S1（最多 2 轮） |

### 3.2 `01-research.md` 必填结构

```markdown
# <batch> 调研方案
## 逐项分析（每个 issue 一节）
### <ID> <标题>
- 现状（带 file:line 与关键片段）
- 根因
- 候选方案 ×2~3（含"不做"选项）+ 取舍表（兼容性/回归面/测试成本/风险）
- 推荐方案：具体到函数签名、改动点、错误路径
- 测试计划：测试文件 + 用例名 + 断言要点（必须能在修复前失败）
- 风险与回退
## 批次级依赖与顺序
## 明确不做的事
```

### 3.3 `02-design-review.md` 必查清单

评审者必须逐条回答（缺项即 `CHANGES`）：

1. 是否改变对外行为或配置默认值？是否已在方案中声明？
2. 是否有更小改动达到同样效果？
3. 每条新测试是否**在修复前会失败**（mutation 检查）？
4. 是否触碰禁改区（见 3.5）？
5. 是否与同波次其它批次的文件冲突？
6. 错误路径是否覆盖：空输入、超大批量、权限拒绝、磁盘失败、并发？
7. 是否新增死配置/死参数？

### 3.4 子代理契约

每个子代理的 prompt 必须包含：

- 项目根、只读/可写边界（S2/S5 只读）。
- 本批 issue ID 列表 + 每项的 file:line 锚点（从 §1.2 复制）。
- 文件所有权白名单（只允许改这些文件）。
- 阶段产出路径与模板。
- 禁止事项：不 commit、不 push、不改测试断言使其变弱、不删测试、不改默认模型/窗口、不联网。
- 失败处理：无法在方案内解决时，写入 `BLOCKED` 并说明，不要自行扩大范围。

### 3.5 禁改区

- `tests/` 中已有断言不得放宽或删除；只能新增用例或修正错误的期望（需在 `03-implementation.md` 说明）。
- 不改 `default_model`、`context_window`、`turn_timeout_seconds` 等运行默认值。
- 不动 `docs/`（除新增 remediation 文档）。
- 不引入新依赖。
- 不做与 issue 无关的重构、重命名、格式化。

---

## 4. 逐批设计

### B1 — MCP 加固

**覆盖**：MCP-01 ~ MCP-11
**目标**：越界执行可开关、生命周期确定性关闭、结果有界、错误可分辨。

**关键设计决策**

| ID | 方案 |
|---|---|
| MCP-01 | `MCPConfig` 新增 `sandbox: bool = False`；`MCPManager.__init__` 增 `sandbox: tuple[Any, Any] | None`；`_server_parameters` 前用 `spawn_argv(command, policy, backend, roots)` 包装；backend 不可用 → 记 Degraded 并回退直连。`server/world.py` 把已有 `sandbox_policy, sandbox_backend` 传入。 |
| MCP-02 | `_MCPSessionOwner.close` 超时后 `cancel()` → `await asyncio.wait({task}, timeout=grace)`（复用 `subagent._settle_cancelled` 同款写法），放弃时 `_log.warning`。 |
| MCP-03 | 执行超时或传输死亡后置 `owner.dead = True`；下一次调用前重建 session（与 `note_transport_death` 共用路径）。 |
| MCP-04 | `_join_text` 增 `max_chars=_MAX_RESULT_CHARS`（默认 200_000），超出用 head+tail + `[truncated]` 标记；`structured` 一并走已有的 `_bounded_data`。 |
| MCP-05 | `list_tools` 按 `nextCursor` 循环，上限 20 页；catalog 末尾在截断时声明"可能不完整"。`list_changed` 只写注释说明未支持。 |
| MCP-06 | `connect(force=True)` 去掉 `not server.command` 判定，remote 也允许；`_connecting_detail` 对 remote 报 url。 |
| MCP-07 | 细分 `mcp_timeout` / `mcp_transport` / `mcp_tool_error`，在 `domain/error_codes.py` 映射到 TIMEOUT / UNAVAILABLE / TOOL_ERROR 族。 |
| MCP-08 | 只有"稳定运行 ≥60s"才重置 delay；否则继续翻倍至 `_BACKOFF_MAX`。 |
| MCP-09 | `_CallbackServer.start` 捕获 `OSError`（EADDRINUSE）→ `port=0` 重试一次。 |
| MCP-10 | `mcp_chars_warn` 改判 catalog 字符数（当前 schema 判定永不触发）；子 agent 无 MCP 写入 `tools/builder.py` 注释 + `docs` 不新增。 |
| MCP-11 | 更新 `test_mcp_progress.py` 为当前字段。 |

**测试**：`tests/test_mcp_lifecycle.py`（close 等待、重连退避）、`tests/test_mcp.py`（超时后重建、分页、错误码）、`tests/test_mcp_catalog.py`（不完整声明）、`tests/test_mcp_oauth.py`（端口回退）、`tests/test_mcp_stdio.py`（sandbox flag 关闭时 argv 不变；开启时用 fake backend 断言包装）。

**验收**：flag 关闭时 `git diff` 对现有请求无行为变化；开启且 backend 不可用时能正常启动并上报 Degraded。

---

### B2 — 上下文工程 + 压缩遗留

**覆盖**：CTX-01 ~ CTX-07c、CMP-01 ~ CMP-05
**目标**：把"压缩之外的无界路径"堵住，并让已修好的压缩机制真正生效。

**关键设计决策**

| ID | 方案 |
|---|---|
| CTX-01 | 目录/裸指针分支也执行 `used += estimate_tokens(block)` 并在 `used >= budget` 后降级为一行指针；目录条目数按剩余预算缩放（`max(10, remaining*4)`）。 |
| CTX-02 | `_bare_pointer` 不再 `read_text`；用 `stat().st_size` 估算行数（`≈ size // 40`）并标注"约"。 |
| CTX-03 | `_file_content` 先 `stat`；> 2MB 或后缀不在 `FILE_PATH_EXTENSIONS` 时返回指针；正常路径按 `chars_for_tokens` 只读前缀（`open().read(n)`）。 |
| CTX-04 | skill 去重：同一 id 已在 `@mention` 全文块出现时，SOP 推荐块跳过该 id；plan 文本保留 system `<plan-mode>`，history 的 `plan_reminder` 在 system 已含时跳过。 |
| CTX-05 | `memory/service.py` 写入前单条上限（如 2,000 字符）；渲染时首行也参与预算。 |
| CTX-06 | 删除 `budget.py` 中无生产消费方的字段（`estimated_tokens`/`calibration`/`cache_hit_ratio`/`by_model`/`cost` 中确认无读者的部分），同步修改仅引用它们的测试。 |
| CTX-07a/b/c | closing 步用空 tools 估算；canonical 计入 `image_tokens`；`history_chars` 计入 `_tool_args_text`。 |
| CMP-01 | 重注入块进入 `checkpoint_view`（有 checkpoint 时）：从 `session.effects.reads` 取最近 5 个文件，预算 8k，内容 hash 稳定；`compress` 路径不再单独追加，避免重复。 |
| CMP-02 | `checkpoint_view` 的"是否更小"判定改用字符数（`sum(len(content))`）；L0 结果按 `(len(messages), 尾消息 id, anchor)` 做一次 memo。 |
| CMP-03 | `protect_tokens = max(8_000, min(40_000, window // 3))`。 |
| CMP-04 | 信封体积判定改为实际比较 `len(envelope) < len(body)`，不满足则跳过。 |
| CMP-05 | 冻结计量检测：若 provider 读数与上一步完全相同且本地 estimate 增长 >5%，则忽略 provider 读数改用 `working_history_tokens`；连续 3 次后开始生效（避免抖动）。 |

**测试**：`tests/test_file_mentions.py`（裸路径/目录扣预算、大文件不整读）、`tests/test_request_assembly.py`（@ 去重、plan 重复）、`tests/test_microcompact_and_refresh.py`（重注入跨请求稳定、protect 随窗口）、`tests/test_context_compression_l2_window.py`（信封不增大）、`tests/test_compaction_gate_working_view.py`（冻结计量）、`tests/test_turn_reporter.py`（事件口径）。

**验收**：构造"用户粘贴 200 个裸路径 + 5 个 @大文件"的请求，tail 字符数不超过 `budget + 单块上限`；连续 20 轮 `checkpoint_view` 总耗时 < 1s（原约 4.5s）。

---

### B3 — Skill

**覆盖**：SKL-01 ~ SKL-06
**目标**：deny 在所有面一致；安装不引入外部文件；注入有预算。

**关键设计决策**

| ID | 方案 |
|---|---|
| SKL-01 | `SOPRouter.__init__` 增 `denied: frozenset[str] = frozenset()`；`route`/`intervene` 在打分前过滤；`RequestAssembler` 构造 router 时用 `denied_skill_ids(self._skills, settings.skill_permissions)` 计算。 |
| SKL-02 | `_inspect` 用 `os.walk(followlinks=False)` 递归检测任意 symlink；发现则拒绝安装并返回原因（不静默跳过）。 |
| SKL-03 | `format_block` 的描述复用 `catalog._MAX_DESCRIPTION_CHARS`；`_format_intervention` 的步骤截断到 500 字符；`@mention` 全文按 `_FILE_TOKENS_CAP` 截断并加 `[截断]` 标记。 |
| SKL-04 | `ai/prompt.py` 去掉外层 `<skills>` 包裹（catalog 已自带标签）。 |
| SKL-05 | `git_source_url` 先判本地路径（`Path(source).exists()` / 以 `./`、`/`、`~` 开头）；不满足才按 owner/repo 解析。 |
| SKL-06 | review cursor 在 `created=False` 时也推进；`dendritic_path` 在 `RequestAssembler` 中接线并加测试（若 `sop_router` 的 Dendritic 代码无法在离线环境稳定测试，则删除休眠代码，二选一在 S1 明确）；`phase` 不匹配时回退为"不限"而非永不推荐。 |

**测试**：`tests/test_sop_router.py`（deny 过滤、phase 回退、Dendritic 接线）、`tests/test_skill_install.py`（嵌套 symlink 拒绝、`./path` 本地安装）、`tests/test_prompt.py`（单一 `<skills>`）、`tests/test_skill_review.py`（cursor 推进）。

**验收**：配置 `skill_permissions: {alias: deny}` 时，catalog、SOP 推荐、@mention、`skill` 工具四个面全部拒绝同名技能。

---

### B4 — 编排 / 计划

**覆盖**：ORC-01 ~ ORC-05
**目标**：旋钮真实生效、注入不浪费、结束原因可读。

**关键设计决策**

| ID | 方案 |
|---|---|
| ORC-01 | 不改行为。在 `turn_policy.py:251-266` 与 `agent_loop.py:1681-1684` 的注释里写明"设计如此：计划实施回合由模型自停/结构续写约束，不设迭代与时钟上限"，并在最终报告标注为"已决策不修"。 |
| ORC-02 | 让 `LadderLimits.strategy_l1` 被 `_due` 消费（`strategy` 分支按 `level <= strategy_l1` 放行）；补测试覆盖 `max_strategy_nudges=0`（关闭）与 `=2`。 |
| ORC-03 | `_search_due` 把 `if not threshold: return False` 提到 `repeat_streak` 判定之前；`_due` 同步调整顺序。 |
| ORC-04 | POST 注入前检查 `index < max_iterations`（或 `_has_a_round_left`）；无余量时写入 `decision.note`，不 `add_message`。 |
| ORC-05 | `terminal_outcome` 保留 `TurnStatus` 枚举，reason 增加阶梯来源（从 `TurnDecision.intervention/level` 取），例如 `"strategy ladder L4: ..."`；补测试断言 reason 含来源。 |

**测试**：`tests/test_interventions_loop.py`（nudge 预算、threshold=0 完全关闭）、`tests/test_agent_loop_react.py`（最后一轮不注入）、`tests/test_turn_close.py`（reason 来源）。

**验收**：`search_streak_threshold: 0` 时一个 turn 内不再出现 `[search-gate]`；`max_iterations=3` 时第 3 轮不再产生新消息。

---

### B5 — 子代理 / 后台

**覆盖**：SUB-01 ~ SUB-05
**目标**：停机不留孤儿、并发不双跑、子代理可被发现。

**关键设计决策**

| ID | 方案 |
|---|---|
| SUB-01 | `subagent_tool` 的结果文案改为 branch/commit + ledger 路径；worktree 内路径改为"（已清理）"或直接移除。 |
| SUB-02 | `Scheduler` 新增 `shutdown()`：取消所有 turn task（保存引用）、逐个 `runner.close(run_id)`；`world.aclose` 在关 loop 前调用；`_prune_background` 增加空闲 handle 的 TTL 剪枝（默认 30 分钟，可在 settings 配）。`Scheduler.stop` 的 task 存入 `self._tasks` 并配 done callback 清理。 |
| SUB-03 | `run()`/`spawn()` 在 `create_task` 之前 `_idem_put(key, digest, status="running")`；`finally` 里失败/取消时删除；补并发测试。 |
| SUB-04 | `ai/prompt.py` 增 `<agents>` 块：`name — description`（每条 ≤120 字符，总预算 600 字符，来自 `AgentCatalog`），仅当 catalog 非空。 |
| SUB-05 | `_open_handle` 里为 child 建 gate 时传 `workspace=workdir`；确认 `build_tool_registry(workdir)` 与 gate 同源（同一 `Sandbox.workspace`）。 |

**测试**：`tests/test_subagent_runner.py`（shutdown 关闭子 loop、空闲剪枝、gate workspace）、`tests/test_scheduler.py`（同 key 并发拒绝、stop 引用）、`tests/test_subagent_tool.py`（产物文案）、`tests/test_prompt.py`（agents 块）。

**验收**：调用 `world.aclose()` 后无残留 `subagent:*` task（用 `asyncio.all_tasks()` 断言）；同 key 并发两个 `run()` 只执行一次。

---

### B6 — 工具 / 会话 / 可观测

**覆盖**：X-01a ~ X-03c（10 个任务）
**目标**：写路径可靠、审计不丢、命令名实相符。

**关键设计决策**

| ID | 方案 |
|---|---|
| X-01a | `ShellReadTool` 显式声明 `network = None` 并在 `precheck` 后仍走 gate 的网络判定（或直接在 `execute` 传 `network=False`）。 |
| X-01b | `append` 改为：读旧内容 → `write_text_atomic(old + content, expected_version=...)`；保留超时与冲突语义。 |
| X-01c | `_mid_file_slice_message` 增加 `offset == 0` 分支：正文是旧文件的严格前缀且更短 → 同样拒绝。 |
| X-01d | `allow_once` 的授权在 `precheck` 消费后立即从 grants 删除（one-shot）。 |
| X-02a | recorder 在 `session_id` 为空时**缓冲**事件，首个真实 turn 时补写；不创建 `unknown/` 目录。 |
| X-02b | `cli.py` 对 `tracing.dir` 统一 `expanduser().resolve()`；`trace overview` 读同一解析结果。 |
| X-02c | `Braid` 暴露 `failures` 聚合（各内层和），`turn_report` 优先读 `Braid`，无则退回 `SafeObserver`。 |
| X-03a | `JobRegistry` 在启动/结算时保留最近 N 个已结算 job（默认 200）与 M 天（默认 7），超出删除 log + json。 |
| X-03b | `mark_pending_approval` / `mark_pending_posture` 走同一 `RLock`；补并发测试（线程级即可）。 |
| X-03c | `slash.py` 增加 `rewind` 别名指向 undo（并在帮助里注明 rewind = undo），或直接返回明确提示。选别名，最小改动。 |

**测试**：`tests/test_tool_shell_read.py`、`tests/test_tool_write.py`（append 原子、前缀拒绝）、`tests/test_dispatch_grants.py`（allow_once 一次性）、`tests/test_trace.py`（无 unknown 目录）、`tests/test_cli_tracing.py`、`tests/test_turn_reporter.py`（Braid failures）、`tests/test_jobs.py`（保留上限）、`tests/test_session_service.py`（并发 mark）、`tests/test_slash.py`（rewind 别名）。

**验收**：`allow_once` 后第二次同调用仍需审批；`tracing.dir` 指向自定义目录时 `trace overview` 与 `/stats` 看到同一批文件。

---

## 5. 已定决策（不再询问）

1. **MCP 沙箱**：`mcp.sandbox` 默认 `false`；开启后包装 stdio 命令，环境不支持则回退并上报。
2. **计划实施回合**：保持不设迭代/时钟上限（ORC-01 只文档化）。
3. **提交**：全部完成后单次提交，不 push。
4. **`max_result_data_bytes` 语义**：按**字符**（与字段名保持现状，S1 中在 docstring 写明；不改字段名以免破坏配置）。
5. **不新增依赖**；不改默认模型/窗口。

---

## 6. 验证计划

### 6.1 每批次（S3/S4）

```bash
# 定向
.venv/bin/python -m pytest tests/<本批测试文件> -q
# 静态
.venv/bin/ruff check ai_coding tests/<改动文件>
.venv/bin/mypy ai_coding
```

### 6.2 每波次结束

```bash
.venv/bin/python -m pytest -q          # 全量，约 5-7 分钟
```
要求：exit 0；失败项必须属于"已知 flake"（当前已知：`test_job_tools.py::test_killing_a_running_job` 偶发），否则阻塞下一波。

### 6.3 最终（W3）

1. 全量 pytest + ruff + mypy。
2. 逐批 `git diff` 与 `01-research.md` 对齐（由统一评审子代理执行）。
3. 写 `FINAL-REPORT.md`：42 项 × 状态（已修 / 已决策不修 / DEFERRED）+ 验证证据 + 残留风险。
4. 用户审批后单次 commit。

---

## 7. 提交与回滚

- 过程中**不 commit**。每批 S3 完成后执行：
  ```bash
  git diff > .trae/work/<wave>/<batch>.diff
  ```
- 若后续批次破坏了前一批，用 `git checkout -- <files>` + `git apply <batch>.diff` 恢复，不丢其它批次进度。
- 最终一个 commit，message 按批次分节（MCP / 上下文 / 压缩 / Skill / 编排 / 子代理 / 工具会话）。
- 不 push。

---

## 8. 风险登记

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| MCP 沙箱包装在不同平台 argv 不一致，导致 server 起不来 | 中 | 高 | 默认关闭；不可用时回退直连并上报；真实子进程测试默认跳过，需手动 `AI_CODING_MCP_STDIO=1` 验证 |
| CMP-02 缓存引入"视图过期"（session 被追加但复用旧 L0） | 中 | 中 | memo key 含消息数与尾消息 id；补"追加后视图必须变化"测试 |
| CTX-06 删除 budget 字段破坏外部读取 | 低 | 中 | 先 grep 全仓确认无读者；只删确认无人用的 |
| CTX-04 plan 去重改变 prompt，影响缓存命中 | 中 | 中 | 保持块顺序不变，只去掉重复文本；对比前后 `prefix_shared_ratio` |
| ORC-04 改动可能导致收尾缺提示 | 低 | 低 | 无余量时写入 outcome.note；保留原有收尾路径测试 |
| 同波次文件冲突 | 中 | 中 | 所有权矩阵 + 每批开工前 `git status` 校验 |
| 全量测试受 flake 干扰 | 中 | 低 | 单独重跑失败项；记录 flake 名单 |

---

## 9. 执行清单

```
[ ] W0 准备
    [ ] 建 .trae/documents/remediation/INVENTORY.md（42 项单一事实源，含状态列）
    [ ] 建 .trae/work/ 目录用于 diff 快照
    [ ] 基线：全量 pytest 记录当前耗时与 flake
[ ] W1（并行 3 批）
    [ ] B1 MCP        S1→S2→S3→S4→S5
    [ ] B2 上下文+压缩  S1→S2→S3→S4→S5
    [ ] B5 子代理      S1→S2→S3→S4→S5
    [ ] 波次全量验证 + diff 快照
[ ] W2（并行 3 批）
    [ ] B3 Skill      S1→S2→S3→S4→S5
    [ ] B4 编排/计划   S1→S2→S3→S4→S5
    [ ] B6 工具/会话/可观测 S1→S2→S3→S4→S5
    [ ] 波次全量验证 + diff 快照
[ ] W3 收尾
    [ ] 统一评审子代理（全量 diff 对照 INVENTORY）
    [ ] 全量 pytest + ruff + mypy
    [ ] FINAL-REPORT.md（42 项状态矩阵 + 残留风险 + DEFERRED）
    [ ] 用户审批 → 单次 commit（不 push）
```

---

## 10. 交付物

| 文件 | 内容 |
|---|---|
| `.trae/documents/multi-batch-remediation-plan-2026-11-07.md` | 本计划 |
| `.trae/documents/remediation/INVENTORY.md` | 42 项清单 + 实时状态 |
| `.trae/documents/remediation/<batch>/01-research.md` | 调研方案 |
| `.trae/documents/remediation/<batch>/02-design-review.md` | 方案评审结论 |
| `.trae/documents/remediation/<batch>/03-implementation.md` | 实现记录 |
| `.trae/documents/remediation/<batch>/04-verification.md` | 验证证据 |
| `.trae/documents/remediation/<batch>/05-code-review.md` | 代码评审结论 |
| `.trae/documents/remediation/FINAL-REPORT.md` | 最终状态矩阵与残留风险 |
