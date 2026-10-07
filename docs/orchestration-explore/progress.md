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

**当前门禁**：`1951 passed, 2 skipped` / ruff clean / mypy 115 files clean。

**已知偶发**（非回归，均为无同步点的时序测试）：`test_pty_textual.py::test_approval_and_ask_user_reach_the_tools`、`test_ui_visual.py::test_collapsible_has_no_top_border`。

## 差距来源

`docs/orchestration-explore/2026-10-07-gap-report.md`（1357 行，2026-10-07 重建）。参考对象：OpenCode（TS）、GenericAgent、Hermes（同语言）、gstack/openclaw（0 源文件，仅 markdown 规范）。

## 待办

**正式里程碑清单见 `milestones.md`**（含依赖关系、验收口径、明确不做项及理由）。
本文件只保留进度摘要。

### P0 —— 已完成

- [x] **子 agent 的 guard 链是空的。** 根因：`core/subagent.py:452-461` 未传 `guards`，`core/dispatch.py:163` 把 `None` 变成 `GuardChain([])`（恒 ALLOW）。修法：`_make_guards` 从 `cli.py` 搬到 `core/guard.py::build_guards`（三处调用点共用：REPL、TUI、子 agent），`SubAgentRunner.__init__` 建一次并传给每个子循环。真实路径已验证 `cat /etc/shadow` 在执行前被拦、拒绝理由回喂给模型。
