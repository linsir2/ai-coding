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

**当前门禁**：`1914 passed, 2 skipped` / ruff clean / mypy 115 files clean。

**已知偶发**（非回归，均为无同步点的时序测试）：`test_pty_textual.py::test_approval_and_ask_user_reach_the_tools`、`test_ui_visual.py::test_collapsible_has_no_top_border`。

## 差距来源

`docs/orchestration-explore/2026-10-07-gap-report.md`（1357 行，2026-10-07 重建）。参考对象：OpenCode（TS）、GenericAgent、Hermes（同语言）、gstack/openclaw（0 源文件，仅 markdown 规范）。

## 待办（按投入产出排序）

### P0 —— 已完成

- [x] **子 agent 的 guard 链是空的。** 根因：`core/subagent.py:452-461` 未传 `guards`，`core/dispatch.py:163` 把 `None` 变成 `GuardChain([])`（恒 ALLOW）。修法：`_make_guards` 从 `cli.py` 搬到 `core/guard.py::build_guards`（三处调用点共用：REPL、TUI、子 agent），`SubAgentRunner.__init__` 建一次并传给每个子循环。真实路径已验证 `cat /etc/shadow` 在执行前被拦、拒绝理由回喂给模型。

### P1 —— 结构性差距

- [ ] **B 工具面在循环外冻结，权限只挡执行不挡可见。** `tool_schemas()` 在 `AgentLoop.__init__:215` 算一次，永久发给模型。OpenCode 按 permission ruleset 过滤 snapshot。
- [ ] **C 权限无 resource 维度；always-allow 是内存 set，进程重启即失。** `PermissionGate.check(tool_name, params)`。OpenCode 是 `action`/`resource`/`effect` + glob + 落库。
- [ ] **A 入口无持久化层。** 消息直接进 `process_input`。OpenCode 有 inbox 表 + doorbell + 幂等 admission。注意：OpenCode 那套 Effect DI（750 行）为 5 个投递方设计，本项目只有 1 个，不可直接搬。

### P2 —— 本项目有但更弱

- [ ] 压缩：无 overflow 驱动的收缩重试、无 provider 原生 compaction。
- [ ] 可观测性：无成本核算、无跨 session 成本归因。
- [ ] planning 回合预算 / 重复调用检测。

### 明确不搬

- Hermes `run_agent.py` 9877 行（分层在 `agent/` 里，胶水不可测）
- OpenCode Effect DI 框架
- GenericAgent 的字符预算
- codemode

### 遗留小项

- [ ] `AIConfig` 无 `models` 字段的根因已由 `aa72115` 绕过，但 `AIConfig` 与 `AppConfig` 的职责边界仍值得复查。
- [ ] qwen 价格未配置（无可核验公开价目）
- [ ] 5ce 会话空转放大链路未复现