# I1 实施方案：修复遗留问题 + 真机验收（tmux / 真实任务 / 长任务）

> 状态：v2（v1 经两名评审员只读评审：后端 P0×2/P1×5、真机 P1×5/P2×5，全部核对修入）
> 触发：/workspace/ai-coding-评估报告.md（外部实测，HEAD d1da11a）+ docs/roadmap-milestones.md（32 里程碑）
> 环境实测：tmux 3.4 ✅ / bwrap ✅ / 模型 dashscope `qwen3.7-flash`（`config.local.yaml` 有真 key）

---

## §0 一句话结论

报告 15 项缺陷 **14 项已修**（实测复核）。剩 6 项真实问题，本轮修其中 3 项"结果对不对"的（R-1 quota 进兜底 / R-2 子智能体跟随 / R-3 窗口跟随）+ 2 项零成本验收债（R-4），然后用 tmux 起真 TUI 跑 6 阶段验收，按 32 里程碑判据逐条出表，产物 `docs/eval-08-h3-postmortem.md`。

**v1 的 R-1 写法是错的**：只把 quota 加进兜底集合会**复现已修的"4 次空转"**（非 AUTH 分支会先重试 `max_retries` 次）；而且 `AUTH_ERROR` **本来就是切**的（首次即切），方案写反了。

---

## §1 现状核实（实测）

### 1.1 评估报告 15 项复核（HEAD `f5ce59d`）

| 项 | 状态 | 证据 |
|---|---|---|
| P0-1 `import httpx2` | ✅ | `core/mcp.py:824` |
| P0-2 undo 部分回滚 | ✅ | `agent_loop.py:2584/2608-2617/2660`；`test_r17_defects.py:962` 通过 |
| P0-3 `open_all()` 契约 | ✅ | `mcp.py:1109`；`docs/features.md:705` |
| P1-4 重试分类 | ⚠️ | 分类已有（`openai_engine.py:34/117`），但 **`insufficient_quota` 归 FATAL、不在 `WORTH_ANOTHER_MODEL`（`:89-96`）→ 额度耗尽不切备用 provider**；且 `:94/:375` 显示 **AUTH_ERROR 本来就首次即切** |
| P1-5 收敛判据 | ⚠️ | 是"连续只读调用计数"（`turn_books.py:393-398`），不判断"两次检索结果相同" |
| P1-6 静态检查回喂 | ⚠️ | 默认关（`static_check.py:47`）+ 只在 L2（`agent_loop.py:967-998`） |
| P1-7 恢复粒度 | ✅ | `core/write_ahead.py:38` |
| P1-8 沙箱 | ✅ | `process_sandbox.py:591`；本机 bwrap 真跑 `pytest tests/test_process_sandbox.py` → **53 passed** |
| P1-9 压缩预计算 | ❌ | `_SHRINK_STEPS=(0.7,0.5,0.35)` 只用于 L4 摘要重试；无 0.85、无后台预计算 |
| P1-10 L3 语义 | ✅ | `docs/features.md:247-252` |
| P2-11 ruff/mypy | ✅ | 实测 0 / 0 |
| P2-12 memory 用例 | ✅ | 实测 11/11 |
| P2-13 r7 L2 预算 | ✅ | PASS |
| P2-14 记忆并发写 | ✅ | `store.py:129/182-197` |
| P2-15 Esc/`/review` | ✅ | 双击 esc；`/review` 走普通回合 |

报告 §3.4 六项长时机制：**实测 6/6 仍成立**。

### 1.2 路线图 32 里程碑（实测）

- **已达成 28**：A6 / B1,B2,B3,B5 / C3 / D3 / E1–E4 / F3 / G4 / H2 + N-1/N-2/N-3/N-4/N-4b/N-5/N-6。
- **未采纳 1**：M-B4（路线图自述）。
- **未达成 3**：**M-H1 安装分发（部分：wheel 可构建、前端随包、bun 版本锁定，但无 PyPI 发布/CHANGELOG）**、**M-H3 IDE 集成（全仓无 LSP/editor 代码）**、**M-H4 对外插件市场（无签名校验）**。
- 判据自动化：**24 条可一条命令出结论**；6 条需人看（M-D3 面板、M-E1 文档、M-E3 `/trace`、M-F2 文档、M-G2 面板、M-H2 文档）；M-H3 不可机检。
- 文档不一致：路线图 §10 写 `L1(≥4)/L2(≥8)`，代码 `interventions.py:101/106` 是 **4/6**。

### 1.3 真机入口（实测，v1 描述有误，已更正）

| 项 | 实测 |
|---|---|
| 可执行文件 | **`.venv/bin/ai-coding`**（`ai-coding` **不在 PATH**，tmux 里直接敲会 command not found） |
| `--path config.local.yaml` | ✅ `models={qwen}`、`default_model=qwen`、`build_world` 实测 OK |
| **不带 `--path`** | ❌ 项目层 `config.local.yaml` 里的 `qwen` **整条被丢弃**（`loader.py:258-261`，非用户定义），`default_model` 变 `''`；且用户层 `${DEEPSEEK_API_KEY}` 未设时 **`load_layers_report` 直接抛 `MissingSecret`，TUI 起不来** |
| 沙箱 | `mode=enforce` + bwrap 可用 ✅；但默认 `network.mode=deny` → **沙箱内 bash 无出网**（真机任务若需联网要显式配） |
| 前端 | `ai_coding/frontend/node_modules` 存在 → `launcher._runtime_dir` 走"就地运行"分支，不复制不联网 ✅ |
| `--path` 与 `/connect` | 互斥（`handlers.py:1180-1188` 会拒）；`/connect` 用**分层模式**单独验 |

---

## §2 待拍板分叉（与 v1 相同，附调研依据）

| # | 分叉 | 选项 A（推荐） | 选项 B | 选项 C |
|---|---|---|---|---|
| 1 | 修复范围 | A：R-1/R-2/R-3/R-4（3 个"结果对不对"+ 2 个验收债） | B：A + R-5 验收契约（提示层） | C：A + P1-9 预计算 + P1-5 "无新发现"语义 |
| 2 | 无 bwrap 主机 | A：保持 `enforce`，启动探测 + 一行可执行降级指令 | B：默认 `warn` | C：自动降级（静默放宽，危险） |
| 3 | 真机范围 | A：全量 6 阶段 | B：去掉长时全栈 | C：只 8 场景 + 4 硬断言 |
| 4 | 产物 | A：`docs/eval-08-h3-postmortem.md` | B：追加外部报告 | C：只写结论 |

---

## §3 修复清单（v2 逐条修正）

### R-1（P1-4）额度耗尽切备用 provider —— 但要"首次即切"

- **改法（三处，缺一不可）**：
  1. `openai_engine.py:89-96` 的 `WORTH_ANOTHER_MODEL` 加入新的 `FailureKind.QUOTA`；
  2. `:141` 把 `insufficient_quota` 从 `FATAL` 改成 `QUOTA`；
  3. **`:375-381` 的 AUTH_ERROR 分支旁加 QUOTA 分支，首次即 `raise FallbackWorthwhile`**——否则它会走进通用分支（`:382-396`）先重试 `max_retries=3` 次，**复现 `docs/plan-m-b.md §9.3` 当初修掉的 4 次空转**。
- **不改**：`AUTH_ERROR` **本来就首次即切**（v1 写反了，已更正）；`FallbackEngine` 链尾直接 `raise`（`fallback_engine.py:122-123`），不会多切。
- **改测试（有理由）**：`tests/test_openai_engine.py:769/797` 锁 `quota is FailureKind.FATAL`、`:775` 锁 `calls == 1`。标签变化 FATAL→QUOTA 是本次修复的定义，"不重试"语义由新分支保留 → **同步改这 2–3 条断言，并同步 `docs/plan-m-b.md §9.3/§13.5`**。
- **验收**：429+quota → 切；429 限速 → 不切；401 → 切；链尾 → 不重试。

### R-2（H3 §10.1）子智能体跟随当前模型 —— 组合根是鸡生蛋，用 holder

- **时机已实测**：子智能体的 engine 是**每次起子智能体时**建的（`core/subagent.py:563` 调 `self._engine_factory()`），所以"跟随切换"可行。
- **障碍（v1 没算到）**：`world.py:216` 建 registry（内含 `SubAgentRunner`）时 **loop 还不存在**（`:259` 才建），`lambda: world.loop.…` 会 UnboundLocalError。
- **改法**：组合根传一个可变 holder（`current_key: list[str] = [app_cfg.default_model]`）给 `_build_tool_registry` → `builder` 的 `engine_factory` 读它；`AgentLoop.set_model` 在 `:503` 之后**写 holder**（或回写 runner 的 factory）。
- **反推 key 不新造**：`handlers.py:1118-1131` 已有"identity 优先、name 兜底"的映射，抽成公用函数给两处用；同时 `set_model` 存 `self._current_model_key`（`default_model` 是"配置默认"，`:330-331` 注释已明说不能拿它当当前）。
- **验收**：切换后起子智能体 → 其 engine 的 `base_url` 是新模型（真链路）。

### R-3（H3 §10.2）窗口跟随 —— 字段名是 `_max_context_tokens`

- **改法**：`ContextManager` **没有 `context_window` 属性**，真字段是 `_max_context_tokens`（`context_compression.py:565`）。改成 property/setter，**setter 内重算 `_max_result_bytes`**（`:607` 构造期派生，不重算则 L3 阈值陈旧）。
- **不改 `config.default_model`**：`test_model_switching.py:94` 锁着"配置默认 ≠ 当前模型"，写它会破坏该分离。因此 `handlers.config_summary` 的 `maxContextTokens` 仍报**配置**窗口——这是诚实的（它报的就是配置），`ContextManager` 用的是当前窗口。
- **验收**：切到小窗口模型 → `context._max_context_tokens` 与 `_max_result_bytes` 都变小；突变验证。

### R-4 验收债（2 条）

- `scripts/r7_cases.py:141-159` 的 `books_feed_the_policy` 期望陈旧：实测是 `action=NUDGE` / `reason="repeated tool failures"`（`interventions.py:397-398`），脚本期望 `FINISH / "no progress"` → **改断言**（策略行为是对的）。
- `docs/roadmap-milestones.md` §10 的 `L1(≥4)/L2(≥8)` → `L1=4/L2=6`（代码为准）。

### R-5（分叉 1 选 B 时）验收契约提示层

- **接缝**：来源是 `session.acceptance`（唯一填充点 `plan_runner.py:1018`），**不是** `turn_policy`（它只读）。需要一个普通回合可写的验收工具。
- **不做的事**：**不自动注入契约**。`turn_policy.py:205-210` 承诺"空契约行为与从前完全一样"，注入会让普通回合改走验收门（`interventions.py:328`），并让 `agent_loop.py:912-926` 把"预算停止"改判 COMPLETED。
- **只做**：提示层要求 + `verify` 必须是真 shell 命令（复用 `acceptance.verify_command_problem`，`acceptance.py:276`）。
- **验收（可机检的那一半）**："产出的 verify 非 shell → 拒绝"；**不断言"必须产出"**（模型不产出时门本就不参与，不可机检）。

---

## §4 真机验收设计（v2：命令全部实测可跑）

统一：`cd /workspace/ai-coding`，用绝对路径 `/workspace/ai-coding/.venv/bin/python` 与 `.venv/bin/ai-coding`。

| 阶段 | 命令 | 断言 | 预计 |
|---|---|---|---|
| A 冒烟 | `scripts/integration_smoke.py --config config.local.yaml` | 工具回合 / 多轮回放 / 子代理 ledger / L4+记忆 | ~5 min |
| B 8 场景 | `scripts/sample_tasks.py --config config.local.yaml` | mcp-off / read / todo / write+edit / bash-ok / bash-deny / subagent / watchdog | ~15 min |
| C 长时机制 | `scripts/eval_longtask_mechanics.py --config config.local.yaml` | 后台快返回 / ledger / L3 / 历史有界 / L2 信封 / 看门狗 / 结构化中断 | ~10 min |
| D TUI 真机 | tmux：`/workspace/ai-coding/.venv/bin/ai-coding --path config.local.yaml --workspace /workspace/eval-workspace/tui-demo --skills /workspace/ai-coding/skills` → `capture-pane -p` → `send-keys -l "<真任务>" Enter` → 再 `capture-pane` + 读 `.ai-coding/sessions/*.json` 与 `traces/` | 渲染（Thought/工具块/token/轮次）+ 真产物 | ~15 min |
| E 同上下文对比 | `scripts/eval_comparison.py --config config.local.yaml --repo /workspace/ai-coding` | T1 读代码 / T2 找真实缺陷（**收敛试金石**）/ T3 设计重试阶梯 | T2 最坏 900s |
| F 长时全栈 | `scripts/eval_fullstack_task.py --config config.local.yaml --workspace /workspace/eval-workspace/taskflow-i1 --rounds 14` | 后端 pytest + 前端 build + **功能断言（HTTP 冒烟，脚本不查功能）** | 1h+ |

**两处 v1 的错误已修正**：
- `eval_fullstack_task.py` 不带 `--keep` 会 **`rmtree` 掉工作区**（`:141-142`）→ 用**新目录** `taskflow-i1`，不碰 10-24 的产物。
- 该脚本**没有 acceptance 入口**（grep 0 命中），"门通过≠任务完成"**不能通过它复现** → 功能齐全性用**独立功能测试脚本**判，并在报告里注明"这是脚本侧判据，不是 `session.acceptance` 门"。

### 4.1 里程碑判据对照（32 条）

可机检的 24 条：跑命令出结论；需人看的 6 条（D3/E1/E3/F2/G2/H2 的"面板/文档"部分）；M-H3 不可机检（无实现）。

### 4.2 与 10-24 报告的差距表 + H3 之后的变化（安装分发 / `/connect` / 模型切换）

### 4.3 产物

`docs/eval-08-h3-postmortem.md`：每阶段附**原始输出**（脚本 stdout、`capture-pane` 文本、session/trace 摘要）。

---

## §5 影响范围

| 区域 | 影响 |
|---|---|
| 重试/兜底 | 额度耗尽现在切 provider（首次即切，不重试）；AUTH 行为不变 |
| 子智能体 | `engine_factory` 多一个 holder；组合根接线 |
| 压缩 | `_max_context_tokens` 可写；`_max_result_bytes` 随之重算 |
| 验收门（选 B） | 只加提示层与"verify 必须真 shell"校验；**不**自动注入契约 |
| 沙箱（分叉 2） | 只加启动探测与提示，不改默认 |
| 真机 | 无代码影响 |

---

## §6 RIPR 用例（红相先行）

| 用例 | 断言 |
|---|---|
| `test_quota_exhaustion_falls_back_without_retrying` | 429+quota → 首次即 `FallbackWorthwhile`（`calls == 1`），`FallbackEngine` 切下一个 |
| `test_auth_error_still_falls_back_on_first_attempt` | 401 → 首次即切（**保留原行为**，防回归） |
| `test_rate_limit_still_does_not_fall_back` | 429+限速 → 不切 |
| `test_a_subagent_follows_the_switched_model` | 切换后起子智能体 → 其 engine 的 `base_url` 是新模型 |
| `test_switching_updates_the_context_window` | 切到小窗口模型 → `_max_context_tokens` 与 `_max_result_bytes` 都变小 |
| （选 B）`test_a_non_shell_verify_is_refused` | `verify: "glob '**/*'"` → 拒绝 |
| （选 B）`test_an_absent_contract_changes_nothing` | 无契约时回合行为与从前一致（回归） |

**变异**：quota 改回 `FATAL` / 去掉"首次即切"分支（→ 重试 4 次）/ 去掉 QUOTA 分支 / `active_model` 退回读 `app_cfg.default_model` / 窗口 setter 删掉 `_max_result_bytes` 重算——每条都必须被打红。

---

## §7 本方案不做的事

1. **不做 M-H3（IDE 集成）/ M-H4（对外插件市场）**：路线图里仍 ⬜。
2. **不做 P1-9 压缩预计算**、**不做 P1-5 "无新发现"语义**、**不做 P1-6 默认开启回喂**（除非分叉 1 选 C）——真机验收会**实测**它们还在不在。
3. **不改 `session.acceptance` 的现有填充点**（除非分叉 1 选 B，且只做提示层）。
4. **不把真机验收改成自动化 CI**（需要真网络与真模型）。
5. **不改 `config.default_model`**（会破坏"配置默认 ≠ 当前模型"的既有测试契约）。

---

## §8 修订记录

| 版本 | 变化 |
|---|---|
| v1 | 首版：15 项复核 + 32 里程碑 + 6 项修复 + 6 阶段真机矩阵 + 4 个分叉 |
| v2 | 评审修入：① **R-1 必须加"首次即切"分支**，否则复现 4 次空转；**`AUTH_ERROR` 本来就切**（v1 写反）；要改 `test_openai_engine.py:769/797`（+`:775`）并同步 `plan-m-b.md`。② R-2 的组合根是鸡生蛋（`world.py:216` 时 loop 未建）→ 用 holder；key 映射复用 `handlers.py:1118-1131`；`set_model` 存 `_current_model_key`。③ R-3 字段名是 `_max_context_tokens`，setter 要重算 `_max_result_bytes`；**不改 `config.default_model`**，`config.summary` 仍报配置窗口。④ R-5 接缝是 `session.acceptance` 不是 `turn_policy`；只提示层，不自动注入；"必须产出"不可机检 → 改成"非 shell verify 被拒"。⑤ R-4 写明新断言值（`NUDGE`/`"repeated tool failures"`）。⑥ 里程碑计数更正为 **28 达成 / 1 未采纳 / 3 未达成**；M-H1 记为"部分实现"。⑦ 真机命令全部实测：**`ai-coding` 不在 PATH**（用 `.venv/bin/ai-coding`）；**不带 `--path` 会整条丢弃项目层模型并可能 `MissingSecret`**；**`eval_fullstack_task.py` 不带 `--keep` 会 rmtree** → 换新目录；补 `network.mode=deny` 说明与绝对路径 |
