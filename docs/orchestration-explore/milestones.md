# 里程碑进度

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
| — | `/model` 死代码修复：`AgentLoop(models=...)` + `default_model=...`，模型表从 `AIConfig` 移到显式参数；`repl.py` 改读 `loop.models` | `aa72115` | 已全绿 |
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

**当前门禁**：`2024 passed, 2 skipped` / ruff clean / mypy 116 files clean。

---

## 已知偶发（非回归，均为无同步点的时序测试）
- `tests/test_pty_textual.py::test_approval_and_ask_user_reach_the_tools`（单独 3/3 通过）
- `tests/test_ui_visual.py::test_collapsible_has_no_top_border`（负载相关）

---

## 待办（P1/P2/P3，按 redesign-brief 优先级）

| 编号 | 任务 | 状态 | 备注 |
|---|---|---|---|
| M1 | plan-only 权限态 + `PLAN_PROMPT` 拆分 | 🟡 规划中 | 需拆分 `PLAN_PROMPT` 为「规划契约」+「执行 seed」 |
| M2 | 步骤种子携带 checkpoint（≤1200 字符） | 🟡 规划中 | 含已完成步骤摘要、当前指令、活跃约束 |
| M3 | 压缩后只重注入"稳定锚点"、恰好一次 | 🟡 规划中 | 压缩锚点：当前指令 verbatim + 活跃约束集 + working checkpoint |
| M4 | 错误恢复分类 → 5 种 Outcome + 熔断 | 🟡 规划中 | OpenCode 5 Outcome、Goose 渐进移除 |
| M5 | 收割可见化 | 🟡 规划中 | extractor 走 budget/trace + 换小模型 + 空收割短路 |
| M6 | 审批顺序修正 + 可行动错误 | ✅ Done (T1) | 审批在沙箱判定后、错误文案可行动 |
| M7 | 任务目标与工作区不匹配 → 规划期拦截 | ✅ Done (T2) | `/plan` 闸门 |
| M8 | 验收命令形态门 | 🟡 规划中 | 验收命令必须黑盒、禁内省框架内部 |
| M9 | 压缩后只重注入锚点、恰好一次 | ✅ Done (T5) | `ContextCompacted` 事件 + pin 指令 |
| M10 | 子 agent 回吐不可控 | 🟡 规划中 | 子 agent 完成/失败/取消统一注入 `<subagent state=...>` |
| M11 | 压缩锚点恰好一次重注入 | 🟡 规划中 | 摘要模板：Objective/WorkState/NextMove |
| M11 | 指令/技能增量下发（diff，取 min） | 🟡 规划中 | OpenCode v2 `instruction-discovery.ts:127-148` |
| M12 | 账目语义修复 | ✅ Done (T4) | `turnUsages` 回合语义、`begin_turn` 重置 ledger |
| M13 | 记忆层四连 | 🟡 规划中 | L0 约束层 / L1 durable / L2 working / L3 task state |
| M14 | 压缩后只重注入"稳定锚点"、恰好一次 | 🟡 规划中 | 同 M3/M9 |

---

## 已知风险与对策

| 风险 | 对策 |
|---|---|
| `MemoryNote.__post_init__` 抛错可能触及 deprecated 入口 | 跑记忆全套验证；这些入口无产品调用方，若测试喂碎片则修 fixture |
| `_ensure_pinned` 的「同 role+同内容」判重可能在用户重复发送同一句时把第二条视为已存在 | 影响仅是少一次重注入，无正确性风险 |
| 新事件进入 golden 事件序列测试 | 仅当该场景真的压缩时出现；步骤 8 已要求先 diff 再决定重录 |
| plan 闸门误报 | 正则只认文件系统根、负向环视排除 URL；误报时报告会列出具体路径 |
| 审批体验（bash 79 次） | 安全边界决策，需用户拍板（D6） |

---

## 实施顺序

T1 → T2 → T3 → T4 → T5 → T6（已完成） → M1 → M2 → M3/M9 → M4/M5 → M8 → M10/M11 → M13/M14