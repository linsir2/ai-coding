# 编排层对标进度

> 这份文件是里程碑的唯一真相源。之前的对比报告只存在于对话中，`docs/orchestration-explore/` 是个空目录——已重建。

## 已完成

| 里程碑 | 内容 | commit | 测试 |
|---|---|---|---|
| — | 审批 UI：edit 统一 diff、`Always allow`（会话内 per-tool，bash 排除）、按钮宽度修复 | `7c009a4` | — |
| — | 记忆畸形输出修复：`_from_lines` 补守卫，删冗余 parser | `cb7ff9d` | — |
| — | trace scoping：子 agent 事件不再决定父方回合收尾、ToolChunk 按 scope 丢弃 | `34fce7a` `6302624` `b9da977` `72e38c7` | — |
| — | memory 维护可观测性：`MemoryHarvested.evicted`、`MemoryConsolidated`、删 `memory_catalog` 死参数 | `3da4840` 内 | — |
| **M1** | provider 重试可观测：`FailureKind` 五分类、`retry_delay` 退避、`on_retry` 钩子、`ProviderRetried` 事件 | `3da4840` | 已全绿 |
| **M2** | 模型降级链：`FallbackEngine` 组合多引擎、`FallbackWorthwhile`、`fallback_models` 配置、启动时解析链 + 环检测、`ProviderFallback` 事件 | `3da4840` | 已全绿 |
| — | `/model` 死代码修复：`AgentLoop(models=...)`，模型表从 `AIConfig` 移到显式参数 | `aa72115` | 已全绿 |
| **P0 完成** | 6 项根因修复（T1-T6） | 见下表 | 503 passed / 0 failed |

---

## P0 根因修复（2026-10-08 酒店会话）

| 任务 | 根因 | Commit | 核心改动 | 验收 |
|---|---|---|---|---|
| **T1** 工具 precheck + 沙箱错误可行动 | R2/R3 | `a2a8173` | `BaseTool.precheck` 契约，4 个文件工具、dispatcher、sandbox 消息加补救 | 17 次越界→0，6 次假审批→0 |
| **T2** /plan 工作区闸门 | R2 | `2532f08` | `outside_workspace_paths` 纯函数 + 双闸门（topic/items） | 真实酒店 topic 0 次模型调用即止 |
| **T3** 记忆形状门 + 存量清洗 | 记忆 HIGH#3 | `d5d87a7` | `is_plausible_note_text` 拒 JSON 碎片；`_load` 自愈；`.memory` 46→29 | 0 碎片、0 酒店进度态 |
| **T4** 账本回合语义 | R8/A6 | `c0357df` | `begin_turn` 重置 ledger | 同 loop 两回合 usage 互不污染 |
| **T5** 压缩指令锚定 + 事件 | R5 | `4cdeb36` | `_ensure_pinned` 保护当前指令 + `ContextCompacted` 事件 | T15 形状复现、golden 等价测试过 |
| **T6** ledger-gate 退役 | R8/A8/D1 | `da9e33e` | 删 gate 分支，保留 `LedgerUpdated` 观测；清理死代码 | 123 passed，grep 干净 |

---

## 当前门禁

`2024 passed, 2 skipped` / ruff clean / mypy 116 files clean。

## 已知偶发（非回归，均为无同步点的时序测试）

`test_pty_textual.py::test_approval_and_ask_user_reach_the_tools`、`test_ui_visual.py::test_collapsible_has_no_top_border`。

---

## TUI 真机运行发现（2026-10-08）

### 已修

1. **斜杠命令提交后面板不关**：键入首个 `/` 打开命令面板，后续字符落进主输入框，Enter 由主输入框消费 → 命令跑了、面板还盖着 transcript。修法：提交时用面板自己的 `dismiss`（`pop_screen` 会和 app 关停竞态）。

2. **状态栏能把 command worker 打死**：`StatusBar._refresh` 每次按 id 查询子节点，而它从 worker 里被调用；屏幕切换时状态栏短暂 detached → `NoMatches` → worker 死。可达路径：面板开着时点击输入框再回车。修法：`compose` 里持有两个 `Plain`，刷新不再查询。

### 已改（设计）

3. 状态栏从**顶部**移到**输入框上方**（对齐 opencode 的 footer menu：agent/model/status 都在输入区旁）。测试按 region 断言顺序，而不是 compose 调用顺序。

4. 折叠标题带信息（参考 opencode 的 thinking 折叠）：`▶ 思考过程 · The user wants to read...`、`▶ 工具 2 个 · 查找文件 · 读取文件`。

### 已修（根因：日志写进了 TUI 的帧）

5. **输入框中间行右侧残留 2 个字符**（观测到 `a"` / `e"`）。
   - 测试里**不复现**（`run_test` 是 headless 驱动，compositor 里那 2 格是空格）
   - 用 `tmux pipe-pane` 抓 pty 原始字节流，在里面搜到：
     ```
     row=39 col=109  后续可打印='2026-10-08 09:57:20,015 IN'
     ```
   - **根因**：`setup_logging()` 把 `StreamHandler(sys.stderr)` 装在 root logger 上，而 TUI 的 stderr **就是 Textual 正在渲染的终端**。一条 152 字符的 httpx 日志被写在帧中间的任意位置，Textual 之后只重绘它认为脏的区域 → 留下 2 格碎片（只有 resize 能清）。
   - **为什么 httpx 的 INFO 没被滤掉**：原有过滤只压了 `logging.getLogger("httpx")`，而这条日志的 logger 名字是 **`httpx2`**（`%(name)s` 字段直接证据）——是另一个顶层 logger，不受影响。
   - **修法**：`send_logs_to_file(workspace)` 在 TUI 接管终端前把 root 日志改到 `<workspace>/.ai-coding/ai-coding.log`（RotatingFileHandler，2MB×3，可写失败降级为丢弃），并把 `httpx2` 加入压制名单。CLI/REPL 路径保持 stderr（面向行，日志只是交错不破坏画面）。
   - **验证**：真机重跑，多回合，`超 108 列的行: 无`。

### 真机观察（非缺陷）

- 长对话（4+ 回合、多工具、审批 y/n、排队命令）累积正常：折叠汇总、transcript 滚动、`● Working`/`● Ready`、`[queue] 回合结束后执行` 都对。
- **bash 每命令都要审批**（G4 刻意保留 bash 不可授权）。一个探索型回合就弹了 2 次；模型有时用 bash 做 glob/read 能做的事。这是刻意的安全/效率权衡，不是 bug。

---

## 待办

**正式里程碑清单见 `milestones.md`**（含依赖关系、验收口径、明确不做项及理由）。本文件只保留进度摘要。

### P1 —— 编排重构

| 编号 | 任务 | 状态 | 备注 |
|---|---|---|---|
| M1 | plan-only 权限态 + `PLAN_PROMPT` 拆分 | 🟡 规划中 | 需拆分 `PLAN_PROMPT` 为「规划契约」+「执行 seed」 |
| M2 | 步骤种子携带 checkpoint（≤1200 字符） | 🟡 规划中 | 含已完成步骤摘要、当前指令、活跃约束 |
| M3 | 压缩后只重注入"稳定锚点"、恰好一次 | 🟡 规划中 | 压缩锚点：当前指令 verbatim + 活跃约束 + working checkpoint |
| M4 | 错误恢复分类 → 5 种 Outcome + 熔断 | 🟡 规划中 | OpenCode 5 Outcome、Goose 渐进移除 |
| M5 | 收割可见化 | 🟡 规划中 | extractor 走 budget/trace + 换小模型 + 空收割短路 |
| M6 | 审批顺序修正 + 可行动错误 | ✅ Done (T1) | 审批在沙箱判定后、错误文案可行动 |
| M7 | 任务目标与工作区不匹配 → 规划期拦截 | ✅ Done (T2) | `/plan` 闸门 |
| M8 | 验收命令生成约束 | 🟡 规划中 | 验收命令必须黑盒、禁内省框架内部 |
| M9 | 压缩后只重注入"稳定锚点"、恰好一次 | ✅ Done (T5) | `ContextCompacted` 事件 + pin 指令 |
| M10 | 子 agent 回吐不可控 | 🟡 规划中 | 子 agent 完成/失败/取消统一注入 `<subagent state=...>` |
| M11 | 压缩后只重注入"稳定锚点"、恰好一次 | 🟡 规划中 | 摘要模板：Objective/WorkState/NextMove |
| M12 | 账目语义修复 | ✅ Done (T4) | `turnUsages` 回合语义、`begin_turn` 重置 ledger |
| M13 | 记忆层四连 | 🟡 规划中 | L0 约束层 / L1 durable / L2 working / L4 task state |
| M14 | 压缩后只重注入"稳定锚点"、恰好一次 | 🟡 规划中 | 同 M3/M9 |

---

## 证据索引

- Trace：`.ai-coding/traces/6aae97b2182245a080a5cd33930ed05c/turn-*.jsonl`
- 会话账本：`sessions/6aae97b2182245a080a5cd33930ed05c.json`
- 记忆库：`.memory`
- 代码：`ai_coding/{core,memory,ui,tools,infra,security}/`
- 对标：`docs/orchestration-explore/source-research/`、各 peer 源码（opencode/GenericAgent/Hermes/OpenClaw）
- 外部：Claude Code docs、OpenCode v2 source、Anthropic context engineering、Manus context engineering blog、mem0/Letta/Zep