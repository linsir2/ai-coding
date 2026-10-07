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
| ~~G5-5~~ | ~~压缩 reserve~~ | — | — | ❌ 砍掉 |
| G7-1/3 | 成本核算 + provider 归因 | 高 | 9 文件 | — | ✅ 已做 |
| G5-6 | token 估算改混合口径 | 中-高 | 1 文件 | — | ⏸ 暂缓（实测后降级） |
| G8-1 | 预算压力注入 | 中高 | 1 文件 | — | ⬜ 待做 |
| G5-1 | provider 拒绝时压缩重试 | 高 | 2 文件 ~50 行 | — | ✅ 已做 |
| G4 | 权限 resource 维度（部分） | 高 | 3 文件 | — | ✅ 已做（缩减） |
| G4b-1 | 授权进权限层（REPL 也有） | 高 | 4 文件 | — | ✅ 已做 |
| G4b-2 | always-allow 项目级持久化 | 高 | 2 文件 | G4b-1 | ✅ 已做 |
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

### ~~G5-5｜压缩 reserve~~ ❌ 砍掉

**实测后决定不做的理由**：OpenCode 需要 reserve，是因为它的 ceiling 直接是**真实 provider 窗口**
（`compaction.ts:890-897`）。本项目的 `max_context_tokens=48000` 是**自设预算**
（`docs/data-contracts.md:100` 原文"上下文预算，驱动压缩"），远低于任何已知 provider 窗口——
实测本仓 trace 里真实 prompt 最大 75k 且全部调用成功。0.8 软水位留下的 20% 间隙本身就是余量，
再叠一层 reserve 是重复计算。

---

### G7-1 / G7-3｜成本核算 + provider 维度归因 ✅ 已做

**调研中先确证了三个 bug**（都在 M2 的 `FallbackEngine` 上，是归因的前提）：

| # | 问题 | 证据 |
|---|---|---|
| P1 | `_active` 被设成「**预测的下一个**引擎」且 `step()` 从不重置 → 一回降级之后，后续每步都报告降级落点，即使主模型作答 | 真实代码跑通：回合1 降级到 deepseek，回合2 由 qwen 作答，`model_config.name` 仍报 deepseek。**状态栏据此显示错误的模型** |
| P2 | `/model X`（X 在链中）改了 `_active`，但 `step()` 恒从 index 0 开始 → **命令报成功，什么都没发生** | `set_model("deepseek")` 后仍由 qwen 作答 |
| P3 | `/model` 切到「已配置但不在链中」的模型 → setter 抛 `ValueError`，而 `handle_input` 与 TUI worker **都没有 try/except** | 异常从 worker 冒出 |

**修法**：
- `_active` = 「**最近一次尝试的引擎**」，`step()` 入口重置为 0，每次尝试前设为 index
- `set_model` 命中链成员时**把它移到链头**（语义 = 「先试它，再按原顺序」），不再改一个被忽略的下标
- `/model` 捕获 `ValueError` 并显示成错误行

**归因实现**（9 文件，每处几行，无新概念）：
- `ModelStepFinished.model: str = ""` —— 丢失的那个事实
- `AgentLoop._model_step` **读** `engine.model_config.name` 而非给 `StepResult` 加字段
  （链已维护该事实；加字段要改遍全仓每个 `StepResult(...)` 构造点，只为说一句一个属性已经说过的话）
- `BudgetLedger._by_model` 分桶 + `by_model()`；空 model 进总量不进桶
- `summary.json` 合并 `by_model`（顶层 `update_summary` 是浅合并，嵌套必须自己累加）
- `render_budget(prices=...)` 按模型分行；`price=` 旧形参保留
- `cli._make_recorder` 构建**全模型**价格表，键是 **wire name**

**真实路径实测**（真 factory + 真 AgentLoop + 真 trace + 真 `/budget`）：

```
回合1: qwen 429 → 降级 deepseek 作答
回合2: qwen 恢复并作答
两轮后引擎报告的模型: qwen3.8-2.4t-a95b     ← P1 已修（改前会报 deepseek）

by_model: {deepseek-chat: 1 step, qwen3.8-2.4t-a95b: 1 step}
trace 每步 model: [(1, 'deepseek-chat'), (1, 'qwen3.8-2.4t-a95b')]

/budget:
2 turns · 2 steps · 2,200 tok (2,000 in / 200 out) · ≈ ¥1.5097
  deepseek-chat      1,000 in / 100 out  ≈ ¥1.4910
  qwen3.8-2.4t-a95b  1,000 in / 100 out  ≈ ¥0.0187
```

**这直接回答了 M2 的评估问题**：同样 1000/100 token，deepseek 比 qwen 贵 **79 倍**。

**真实路径抓到的第 4 个 bug**（单测抓不到，因为我测试里两边都写同名）：
`by_model` 用 `ModelConfig.name`（wire name），而价格表按**配置键**构建——`qwen` 与
`qwen3.8-2.4t-a95b` 不同名 → **token 有、钱没有，且静默**（未知价格本来就该什么都不显示）。
已修 + 回归钉子 `test_the_price_map_is_keyed_by_the_wire_name`。

**明确不做**：跨 session 的按模型归因（`aggregate.py`）——降级发生在回合内，session 级已能回答
「降级贵不贵」；`/model` 切到链外模型（需重建引擎：换链、关旧 client、重订阅回调，是独立设计）。

---

### G5-6｜token 估算改混合口径 ⏸ 暂缓

**实测证据（本仓 165 条真实 trace，`estimated_tokens` vs `usage.prompt_tokens`）**：

| 指标 | 值 |
|---|---|
| `real/est` 中位数 | **1.356** |
| 低估 >15% 的样本 | **76%** |
| 高估 >15% 的样本 | **1%** |
| 最坏低估 | **2.08x** |

偏差真实存在且是**有方向的**，不是"不精确"。但**修复它不是纯收益**：

- 当前自设预算（48k）吸收了它——trace 里 75k 真实 prompt 全部成功
- 改准后压缩会更早触发 → **降低 prompt cache 命中率**（`context_compression.py:390-391`
  有实测注释：append-only 92-93% vs 每轮重写 26%）

**决定**：G5-1 已提供安全网（估错时压缩重试而不是死在原地）。是否值得用 cache 命中率换估算精度，
应该等 G5-1 上线后看 `/trace` 的 `degraded` 计数——如果反应式压缩很少触发，说明预算已经够用。

**⚠ 与旧报告的分歧**：差距报告把它标为"中-高，做"。本项基于本仓实测数据降级为"暂缓"，
理由是当时的判断没有实测支撑。

---

### G8-1｜预算压力注入

**问题**：本项目有 6 个独立预算（`turn_token_budget` / `max_context_tokens` / `max_iterations` / `stall_iterations` / 各种 nudge 上限），模型对它们一无所知，只能靠 harness 的 nudge 猜。

**改法**：剩余 10% 时往 tail 注入一句 warning。**纯文本注入，零额外模型调用。**

**与 G8-2 的取舍**：G8-1 零成本，G8-2 多一次调用。先做 G8-1。

---

### G5-1｜provider 拒绝时压缩重试 ✅ 已做

**为什么是「高」**：唯一一个「用户已经付出全部成本，但因为上下文超长而彻底失败」的场景。

**实施结果与报告建议的四处差异**：

| 报告建议 | 实际做法 | 原因 |
|---|---|---|
| 增 `CONTEXT_OVERFLOW` + `PAYLOAD_TOO_LARGE` | **只增 `CONTEXT_OVERFLOW`** | 413 已并入同一分支；单独的 `PAYLOAD_TOO_LARGE` 没有消费者，是冗余 |
| 在 `_model_step` 的 `except` 前加分支 | **循环体捕获后 `continue`** | 重试必须经过正常的 `assemble()` 路径（新 tail 块、新估算）；在 `_model_step` 内重试需要手搓请求 |
| 未提 | **`kind` 从 `FallbackWorthwhile` 上提到 `EngineError`** | 循环层要靠它判断是否压缩，而普通 `EngineError` 原本没有该字段；靠 `str(exc)` 匹配会重犯 M2 修过的错 |
| 未提 | **复用 `Compactor.compact_now`** | 它已经是"强制压缩 + 只在真的变小时写回"，零新方法 |

**M2 安全闸**（报告明确警告的点）：`CONTEXT_OVERFLOW` 不在 `WORTH_ANOTHER_MODEL`，
所以走普通 `EngineError`，`FallbackEngine` 不会白试下一个模型。由
`test_a_context_overflow_never_tries_another_model` 钉住。

**防死循环**：每回合上限一次 + `removed == 0` 立即放弃。

**实测（真实路径）**：首次请求 14 条消息被 DeepSeek 风格 400 拒绝 → 压缩会话 12→4 条 →
重发成功，`Degraded(model_step, provider rejected..., compacted...)` 落 trace。

---

### G4｜权限 resource 维度 ✅ 已做（缩减后）

**核验推翻了差距报告的两个前提**：

| 报告的说法 | 实测 |
|---|---|
| 「allow write = 允许写整个文件系统，包括 `/etc` 和 `~/.ssh/authorized_keys`」 | **错**。`security/sandbox.py:22-34` 已把 write/edit/read/glob 约束在 workspace 内，越界抛 `SandboxViolation`，与权限门无关地生效 |
| 「存进 session 文件」实现持久化 | **机制不成立**。session 是一次对话，存 session 文件重启开新对话照样丢；要跨重启必须项目级存储 |

**真正的缺陷**：授权粒度 ≠ 被展示的粒度。用户在模态里读 `edit src/foo.py`（含 diff），"Always" 记住的是 `edit`——覆盖整个 workspace。与 `guards=None`→恒 ALLOW 同类：一个键看起来像"就这一件"，运行起来是"所有事"。

**实际可授权面极小**：`_warn_tools = {write, edit, bash}`，bash 被策略排除，MCP 工具无资源可提取 → 真正受影响的只有 `write`/`edit`。

**实现**：
- `security/permission_gate.py` 新增纯函数 `grant_key(tool, params)`，返回 `"edit:src/foo.py"` 或 `None`
- `_NEVER_REMEMBERED = {bash}` / `_PATH_TOOLS = {write, edit}` 从 `ui/modals.py` 的 `ALWAYS_EXCLUDED` 迁到权限层——排除 bash 是**权限策略**，不是 UI 布局
- `ui/modals.py` 删 `ALWAYS_EXCLUDED`，按钮与提示按 `grant_key` 判定；提示显示将记住什么（`a always (edit:src/foo.py)`），供用户核对
- `ui/textual_app.py` `_always_allow: set[str]` → `_grants: set[str]`（键）

**明确砍掉**：
- **glob 匹配**：只有授权是"生成的模式"时才需要，而模式**必须让用户看到**否则缺陷下移；"模式该多宽"没有唯一正确答案
- **bash 的精确命令授权**：排除 bash 是刻意安全立场，改它应是独立决定；且精确命令授权实用性低（命令很少完全相同）

**真实路径实测**：
```
edit src/a.py    → key=edit:src/a.py    gate=warn
write ./src/a.py → key=write:src/a.py   gate=warn   （normpath 归一化）
edit src/b.py    → key=edit:src/b.py    gate=warn

grants={edit:src/a.py} 之后：
  edit src/a.py   还会问吗: False
  edit ./src/a.py 还会问吗: False
  edit src/b.py   还会问吗: True
  write src/a.py  还会问吗: True   （write ≠ edit）
  bash 任意命令    还会问吗: True
```

### G4b-1｜授权进权限层 ✅ 已做

**为什么**：授权原本是 `ui/textual_app.py` 里的一个 `set`，导致三件事——REPL 永远无法授权
（它的回调只解析 y/n/s，所以 REPL 用户每次 write 都要手敲 y）、trace 看不到用户授权过什么、
策略层完全不知道有这回事。**授权是权限，就该和权限住在一起。**

**契约变更**：
- `ApprovalResult` 加 `ALWAYS`（不是 YES 上的标志位：前端只回一个字母，把 "a" 压成 "y"
  正是让授权变成 UI 私产、REPL 够不着的原因）
- `PermissionGate.grant(tool, params)` / `gate.grants` / `gate.revoke_all()`
- `check()` 顺序成为契约：**deny → grant → 静态表**。放前面会让将来任何路径级 deny
  被"之前给的 always"绕开
- `dispatch._ask` 处理 ALWAYS → `gate.grant()` → `APPROVED`（`ApprovalVerdict` **不加成员**：
  调用方无需区分"批准且记住"与"批准"）
- `CLIApprovalCallback` 解析 `a`，提示 `[y/N/a/s]`——**两个前端共用的唯一解析器**
- TUI **净删**：`_grants`、短路、`"a"→"y"` 全部消失

**删掉的死代码**：`approve_from_gate_decision` + 其 2 个测试文件里的用例。生产路径从不经过它
（`dispatch._ask` 直接调回调），它的 docstring 还在描述一个已不存在的 SDK 钩子。

### G4b-2｜项目级持久化 ✅ 已做

**存储位置的关键决策**：**不放在 workspace 内**。

`.ai-coding/` 是显而易见的落点（已是 per-workspace runtime 目录、已被 gitignore），
但 `git add -f` 能提交被 ignore 的文件，所以**恶意仓库可以自带
`.ai-coding/grants.json` 预授权 `edit:.git/hooks/pre-commit`** —— 授权会关掉提示，
所以这个攻击是**静默**的，而 hook 在下一次 git 操作时执行。

存在 `~/.config/ai-coding/grants.json`（按 workspace 分键）根除它：clone 的仓库写不进去，
文件工具被 Sandbox 挡、bash 不可授权，所以 agent 也无法静默改它。
`test_a_workspace_local_file_cannot_authorise_anything` 钉住这个攻击面。

**撤销**：`/permissions` 列出授权与文件路径，`/permissions clear` 清空。
一个跨重启的常驻授权如果无法撤销，就是把用户锁在自己过去的决定里。

**实现**：`security/grants.py` 的 `GrantStore`（载入/追加/清空，全部 best-effort：
坏 JSON、只读目录都降级为"无授权"，绝不阻止启动）；`from_config(app_cfg, workspace=...)` 注入。

**真实路径实测**（真 CLI 回调读 stdin、真 gate、真 dispatch、模拟重启）：
```
会话1 edit a.py  → denied=False success=True  用户输入 a → 授权 ('edit:src/a.py',)
会话2 edit a.py  → denied=False success=True  （重启后不问,直接执行）
会话2 edit b.py  → denied=True                （问,答 n 被拒）
落盘            → {"version":1,"workspaces":{"/tmp/.../workspace":["edit:src/a.py"]}}
撤销后          → 内存 () 磁盘 ()
恶意仓库自带文件 → 载入 () ← 攻击面闭合
```

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