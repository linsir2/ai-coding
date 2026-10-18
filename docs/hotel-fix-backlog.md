# hotel 会话修复：已落地与未做

## 已落地

### 第一轮（`2cc1561`）

* `tools/glob_tool.py` — 绝对 pattern 相对化；越界返回 `workspace_escape`
  结构化错误，文案带当前工作区与 `-w` 出路。根因：`Path.glob(绝对)` 抛的
  `NotImplementedError` 不在原 `except` 里，裸透成 `tool_error`。
* `app/commands.py` — `/todo` 改发 `TaskUpdated`（`todo_write` 已在用的卡片
  通道）并打印 `render_display()`。根因：它只走 `console.line`，把 prompt
  格式的 `render()`（带 `<task-state>` 信封）当人类文本打印，卡片永不更新。
* `core/request_assembly.py` — `_environment_block` 对工作区外绝对路径给一行
  警告，复用 `core/plan_runner.py:104 outside_workspace_paths` 而非第二份
  正则。Build 回合只警告不拒绝，拒绝仍归 `/plan` 的两道门。

### 第二轮（D1-a，guard 误拦）

实测依据：12 个 trace 目录共 9 次 `guard_denied`，**全部 `duplicate_call`，
`failed_repeat` 零次**；7e48aeef turn 1 的两次是
`glob {"**/*hotel*"}`（第 2 步已成功）与
`read {"/home/.../requirements.txt"}`（第 2 步 `sandbox_violation` 失败过）。

* **`core/guard.py:DuplicateCallGuard`** — 计数从 `check()` 移到新增的
  `after()`，**只对成功调用计数**，失败删除指纹。理由：拒绝文案是 "reuse the
  earlier result"，而失败调用没有 result 可复用；让模型去复用一个错误，等于
  教它别再问。阈值语义随之从"第 N+1 次调用"变为"第 N 次成功后"。
  防护面不变：重复写文件、重复 bash 这类真攻击都是**成功**调用，仍在计数内。
  文案同步改为 `already succeeded N time(s)`，与新语义一致。

### D1-b 已尝试并撤回（证伪留档）

曾把 `guard_denied` 加进 `core/turn_books.py:_NOT_EVIDENCE_ERRORS`，理由是
"拒绝是判决不是损坏"。**实测证伪，这条路是错的**：

把 4 次相同失败 read 喂进真实 loop，ladder 从 3 次注入 strategy-gate 变成
**11 次**，回合以 `迭代预算用尽（4 轮）` 收尾，终答从 `完成` 变成
`[回合未完成 · 迭代预算用尽]`。机制：`failed_streak` 被清零 → L1 窗口的
helped 信号（`failed_streak == 0`）判为"有进展"→ 同一个 L1 nudge 反复注入
直到预算耗尽。**比原问题更糟。**

正确修法是让守卫别开火（`DuplicateCallGuard`），而不是事后给拒绝打折。
`turn_books.py` 的 `_NOT_EVIDENCE_ERRORS` 因此保持原样，并在注释里写明
`guard_denied` 为何**故意不在**其中，防止有人再犯。

### 因 D1-a 更新的既有测试

* `tests/test_guard.py::test_duplicate_guard_allows_a_retry_then_refuses` 与
  `::test_duplicate_guard_resets_between_turns` — 原先断言"只 check 不 after
  就该拒"，那正是被修的缺陷本身。
* `tests/test_orchestration_wire.py::test_the_ladder_closes_its_loop_on_the_wire`
  — r3 的 `duplicate_call` 拒绝改为断言**不**出现；结束该循环的拒绝来自
  `failed_repeat`（其本职）。ladder 仍完整走通：6 次请求、L1→L2→L3、账本
  `level 3 / helped` 不变。

## 未做

### D3「预算早耗尽」—— 实测推翻，不做

原判断"10 步硬顶导致回合太短"是错的。`.ai-coding/traces/7e48aeef/turn-1.jsonl`
（199 行）实测：

* `IterationStarted` **25 次**，`max_iterations=15`（`config.local.yaml:50`，
  非默认 10）；
* 03:50:46 `index` 从 15 **重置为 1** —— C3 续轮（`core/turn_policy.py:263-290`）
  正常生效，长任务机制在工作；
* 终态 `TurnStatus.ABORTED / reason=cancelled` —— **人工按 Esc**，非预算耗尽；
* 真实用量 `total_tokens=335,005`（cached 135k）；侧栏 `21,299 (8%)` 是单次
  请求上下文，不是累计；
* 13 次失败：`sandbox_violation` 4、`error=None` 3、`readonly_denied` 3、
  `guard_denied` 2、`tool_error` 1。

"感觉没跑多久"的真因是**前 11 步都在试路径**（第 1 步绝对 glob，第 3/4/5 步
连试三种越界写法），有效工作从第 9 步才开始 —— 正是第一轮 P0 修掉的东西。

### D2. Thought 英文与回答脱节

* 位置：`ai/prompt.py:16 BASE_INSTRUCTIONS`（只约束回答语言）、
  `ai/openai_engine.py:382 reasoning_content` 直通、`server/bridge.py:102`
  透传。
* 未做理由：与失败链无因果；改 prompt 影响全量请求前缀缓存
  （`docs/r3-analysis.md` 实测 prefix 92%），需单独评估。
* 入口：`METHODS_SECTION` 加"思考与回答同语言"一句 + 前端 Thought 默认折叠；
  先测缓存命中变化。

### D4. 跨仓库 `-w` 一键切换（TUI/IPC 契约改动）

* 位置：`server/handlers.py:session.create`、`server/world.py`、前端
  `session.ts`。
* 未做理由：P0 文案已指引 `-w` 重启；真一键切要改 IPC 契约，超最小范围。
* 入口：`session.create` 加 `workspace` 参数 + `files.find` 复用，前后端同步。

## 观察项

D1-a 上线后若 trace 仍出现 `error=guard_denied` 且 `data.guard=duplicate_call`，
**先确认该调用此前是否成功过**：成功过就是本守卫的正常职责（结果可复用），
未成功过则说明还有别的路径在计数，需要重新取证而不是继续放宽阈值。
区分方法：`ToolFinished.data.guard` + 该 fingerprint 在本回合的历史结果。
