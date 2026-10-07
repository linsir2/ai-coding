# 编排层里程碑

> 唯一真相源。差距来源：`2026-10-07-gap-report.md`（1357 行，四个参考系统的源码级对标）。
> 历史调研原文：`source-research/`。
>
> **排序原则**：先做「零设计成本的纯搬」，再做「单文件可验证的补缺」，最后才碰跨文件的结构性改造。
> **依赖不可跳过**：G3 依赖 G4，因为「按资源 deny」是「工具面裁剪」的前提，没有它就没有裁剪依据。

---

## 状态总览

| ID | 名称 | 影响 | 成本 | 依赖 | 状态 |
|---|---|---|---|---|---|
| G1-1 | turn 重入闸门提升到编排层 | 中 | 1 文件 ~30 行 | — | ✅ 已做 |
| G5-5 | 压缩 reserve | 低 | 1 行 | — | ⬜ 待做 |
| G7-1/3 | 成本核算 + provider 归因 | 高 | 1-2 文件 | — | ⬜ 待做 |
| G5-6 | token 估算改混合口径 | 中-高 | 1 文件 | — | ⬜ 待做 |
| G8-1 | 预算压力注入 | 中高 | 1 文件 | — | ⬜ 待做 |
| G5-1 | provider 拒绝时压缩重试 | 高 | 2 文件 ~50 行 | G5-6 | ⬜ 待做 |
| G4 | 权限 resource 维度 + glob + always 持久化 | 高 | 2-3 文件 | — | ⬜ 待做 |
| G3-1 | deny 的工具不出现在 schema | 中 | 1-2 文件 | **G4** | ⬜ 待做 |
| G8-2 | 达上限时让模型自己收尾 | 中 | 1-2 文件 | G8-1 | ⬜ 待做 |
| G5-3 | L4 摘要形状校验 | 中 | 1 文件 ~20 行 | — | ⬜ 待做 |
| G1-2 | 消息幂等 | 中-高 | 1 文件 | — | ⬜ 待做 |

---

## 已完成（不在此列表）

- **M1** provider 重试可观测（`3da4840`）
- **M2** 模型降级链（`3da4840`）
- **`/model` 死代码修复**（`aa72115`）
- **P0 子 agent guard 链**（`9113fd4`）

---

## 逐项详述

### G1-1｜turn 重入闸门提升到编排层 ✅ 已做

**实施时的发现（与报告描述不同）**：`input.disabled` **早已被移除**（`ui/textual_app.py:88-92`），换成了 `_queued` 队列 + `_turn_active` 标志 + Textual 的 `@work(exclusive=True)`。功能等价，但那是**单个 app 实例内**的保护。REPL（`ui/repl.py:79/179`）和 plan step（`core/plan_runner.py:484/496`）只靠「调用方恰好 await 串行」的约定。

所以本项不是「修一个已知的洞」，是**把前端已有的不变量上移，让它对所有入口生效**。

**实测对照**（真实 `AgentLoop` + `SessionService`）：
- 无闸门（直调 `_run_turn`）：`['user', 'user', 'assistant', 'assistant']`
- 有闸门（`process_input`）：`['user', 'assistant', 'tool', 'assistant', 'user', 'assistant']`

**改法**：`AgentLoop` 持一个 `asyncio.Lock` 或 `_running` 标志，`process_input` 进入时若已在跑，按 steering 语义排队而非并发。

**为什么第一个做**：唯一一个**零设计成本、纯搬**的项目。不需要新概念，只是把已有语义上移一层。

**验收**：两个并发 `process_input` 必须串行执行，且第二个的 tool_call 不与第一个混淆。

---

### G5-5｜压缩 reserve

`max_context_tokens - max(0.1*W, 16000)`，一行。给压缩留出余量，避免压到边界后又溢出。

---

### G7-1 / G7-3｜成本核算 + provider 维度归因

**为什么重要**：本项目把 M1/M2 挂在重试和降级上，那么「这次降级省了钱还是多花了钱」就是核心问题。`core/budget.py::cost()` 已有纯函数骨架，缺的是 ①实际记账 ②`/trace` 汇总 ③降级到不同模型时按各自单价分账（G7-3 与 M2 直接相关）。

**注意**：`config.local.yaml` 里 qwen 无可核验公开价目，`Pricing.cost()` 未配置时返回 `None`（不假装 0）。所以这一项的 UI 展示在 qwen 上仍是「无估算」——**这是正确行为，不是 bug**。

---

### G5-6｜token 估算改混合口径

**为什么在 G5-1 之前**：估算偏低 → 压缩触发太晚 → provider 先拒绝 → G5-1 才有事可做。**它是 G5-1 的放大器**，先修估算，G5-1 的效果才测得准。

本项目已有真实 usage（`domain/usage.py`），可学 OpenCode 的「最近一次实测 + 之后按字符估」（`compaction.ts:859-883`）。

---

### G8-1｜预算压力注入

**问题**：本项目有 6 个独立预算（`turn_token_budget` / `max_context_tokens` / `max_iterations` / `stall_iterations` / 各种 nudge 上限），模型对它们一无所知，只能靠 harness 的 nudge 猜。

**改法**：剩余 10% 时往 tail 注入一句 warning。**纯文本注入，零额外模型调用。**

**与 G8-2 的取舍**：G8-1 零成本，G8-2 多一次调用。先做 G8-1。

---

### G5-1｜provider 拒绝时压缩重试

**为什么是「高」**：唯一一个「用户已经付出全部成本，但因为上下文超长而彻底失败」的场景。

**改法**：
1. `classify_failure` 增 `CONTEXT_OVERFLOW` / `PAYLOAD_TOO_LARGE`
2. `AgentLoop._model_step` 在 `EngineError` 落到 `except` 之前，若 kind 是这两者 → 强制压缩一次并重放当前 step

**⚠ 风险（报告明确标注）**：这会动 M1/M2 的异常分层。必须确认不破坏 `FallbackWorthwhile` 的语义——**上下文超长对任何模型都一样，所以它应该是普通 `EngineError` 而非 `FallbackWorthwhile`**（否则降级链会去白试其他模型）。这一点实施时必须写成测试。

---

### G4｜权限 resource 维度 + glob + always 持久化

**三个子项，同一批做**（报告建议的最小方案，不是照搬 OpenCode）：

| 子项 | 内容 | 影响 |
|---|---|---|
| G4-1 | `always_allowed` 持久化到 session，**按资源模式而非工具名** | 高。用户以为记住了，其实没有 |
| G4-2 | `PermissionGate` 增资源维度：`write`/`edit`/`read` 提取 `path`，`bash` 提取命令模式 | 高。安全边界实质缺失 |
| G4-3 | glob 匹配（新增 ~14 行 `wildcard.match`） | 中。当前只能子串 |

**不做完整版 `Action/Resource/Effect` 三元组**：OpenCode 的三元组是为「多 agent × 多 session × 多工具集」设计的，本项目是单 agent 单 session。完整版会引入 ruleset 合成、优先级、agent 级配置三个不需要的概念。

**`ALWAYS_EXCLUDED` 的变化**：从「按工具名排除 bash」变成「按资源粒度不足时排除」。

---

### G3-1｜deny 的工具不出现在 schema

**问题**：模型会反复尝试一个必然被拒的工具，浪费 iteration 并诱发困惑。「已知但被 deny」和「完全未知」给模型的信息量不同。

**依赖 G4**：没有「按资源 deny」，就没有裁剪依据。

**⚠ 不做 G3-4（schema 运行时动态化）**：冻结是 prompt cache 稳定的前提（`tools/schema.py:33-38` 注释），动态化会摧毁缓存命中。

**明确不搬 codemode**（OpenCode `tool.ts:235-250`）：把工具集折成一个 `execute` 工具 + 目录，让模型用代码调用工具。这需要 code-execution 能力、独立安全边界、重写全部输入校验，是**新编程范式**不是编排层差距。本项目工具数不多（~15 内建），收益不足以抵消复杂度。

---

### G8-2｜达上限时让模型自己收尾

**问题**：本项目 handoff 是 harness 生成的，质量必然低于模型自己写一句总结。

**⚠ 需要先测**：多花一次模型调用，省下的 handoff 质量是否值得。**必须和 G8-1 一起设计**，只在「还有 token 预算」时做。

---

### G5-3｜L4 摘要形状校验

`SUMMARY_TEMPLATE` + `filled()` 风格检查 + 一次 nudge。摘要质量直接决定压缩后的 agent 能力。便宜，先做。

---

### G1-2｜消息幂等

**只做最薄一层**：不需要 inbox 表。给 `SessionData` 一个单调递增的 `message_seq`，`process_input` 接可选 `input_id`，重复时返回缓存的上一轮 outcome。与 OpenCode 的 `reconcile`（`inbox.ts:155`）同一思路。

**触发场景**：UI 双击、网络重试、脚本重放。

---

## 明确不做（附理由）

| 不做 | 理由 |
|---|---|
| G1-3 steering 持久化 | steering 语义就是「这一轮内的临时插话」，重启丢掉是正确的 |
| G1-4 inbox 表 + doorbell | OpenCode 的 750 行 Effect DI + 550 行 inbox 是为 server/TUI/desktop/cron/gateway **五个投递方**设计的。本项目单进程单投递方。**纯复杂度增加。** |
| G3-4 schema 动态化 | 摧毁 prompt cache |
| G4-4 完整三元组 | 单 agent 单 session 不需要 ruleset 合成与优先级 |
| G4-5 reject 联动整批 | 串行模式一次只有一个 pending |
| G4-6 `CorrectedError` | 本项目没有「用户给纠正信息」这个交互 |
| G5-2 shrink 阶梯 | G5-1 做完收益递减 |
| G5-4 provider 原生 compaction | `openai_engine.py` 是纯 chat-completions，没有该协议位 |
| G7-4 事件溯源 | 同步单进程，事件投影是为多投影付的复杂度 |
| G7-5 smart routing | 不是编排层改进，M2 降级链已覆盖「换个便宜的」需求 |
| G8-3 单轮去重 | 已有 `DuplicateCallGuard` |
| Hermes `run_agent.py` 9877 行 | 分层在 `agent/` 里，胶水依赖 self 的十几个可变字段，不可测 |
| codemode | 见 G3-1 说明 |

---

## 遗留小项

- [ ] `AIConfig` 与 `AppConfig` 的职责边界复查（`aa72115` 已绕过根因，但边界仍值得审视）
- [ ] qwen 价格未配置（无可核验公开价目）→ G7-1 的 UI 在此模型上仍显示「无估算」，**这是正确行为**
- [ ] 5ce 会话空转放大链路未复现
- [ ] M2 遗留：`set_model` 现在能用了，但降级后不粘滞（已拍板「仅本回合内切换」）