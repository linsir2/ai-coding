# R14 — overflow 重试策略核验（前提不成立，结论记录）

> 本文原计划给 `agent_loop` 加 `!outputStarted` 闸门。核验后**未加代码**：保护已存在于
> `openai_engine._stream`。以下是核验过程与最终结论，保留作为该项关闭的依据。

> 定位：`docs/compaction-debt.md` §P2-2。**唯一会导致模型输出自相矛盾的压缩路径。**
> 依据：`ai_coding/core/agent_loop.py:527-549`（现状核验）· `openai_engine.py:372-420`（delta 流）· `/home/linsir365/projects/opencode` `step.ts:108-115,148-159`。

---

## 1. 问题陈述

`agent_loop.py:527-549` 的 `_CONTEXT_OVERFLOW` 兜底：provider 拒绝请求 → 压缩历史 → `continue` 重跑本轮迭代。

**缺失**：没有检查模型是否已开始输出。如果 provider 在流式输出中途才报 context overflow（先吐了若干 token，然后 400），我们仍然压缩重跑。后果：

- 前端已经收到了前半段输出
- 重跑会从头再生成一遍
- 用户看到的是**两段矛盾的回答拼接**，或前半段被静默吞掉

OpenCode 的做法（`step.ts:108-115`）：
```ts
if (
  LLMEvent.is.providerError(event) &&
  isContextOverflowFailure(event) &&
  !publisher.record().outputStarted    // ← 关键：输出已开始就不允许压缩
) { overflowFailure = event; return }
```
`step.ts:148-159` 在返回 `Outcome.Compacted()` 前再查一次 `!outputStarted`（双保险）。

**我们的现状**：`openai_engine.py:400-403` 已经在调 `on_delta(str(content))`——这就是"输出已开始"的信号，但信号止步于 engine 内部，没有传到 loop。

---

## 2. 现状核验（数据流追踪）

```
IPC handlers.py:265
  → AgentLoop.process_input(on_delta=bridge.on_delta)
    → _run_turn(on_delta=bridge.on_delta)           ← 可在此层放标志
      → _model_step(on_delta=bridge.on_delta)       :487-495
        → ChatEngine._stream(on_delta)
          → on_delta(str(content))                  :402-403  ← 输出开始的信号
      ← EngineError(kind=_CONTEXT_OVERFLOW)         :527
      → compact_now() + continue                    :530-549  ← 需要检查标志
```

**关键事实**：`on_delta` 从 IPC 一路传到 engine，中间无任何层知道"delta 是否已经发出过"。

**`EngineError` 结构**（`ai/engine.py:34-49`）：
```python
class EngineError(RuntimeError):
    kind: str = ""
    # 空 = 没有 verdict（如流中途断开）
```

**`StepResult`**（`domain/loop.py:29-39`）：`text / tool_calls / finish_reason / usage / reasoning`。无 `output_started`。

---

## 3. 设计：最小侵入的信号通路

### 方案对比

| 方案 | 改动 | 成本 | 风险 |
|---|---|---|---|
| **A. 包装 DeltaSink** | `_run_turn` 开头放 `output_started` 标志，用 wrapper 包 `on_delta`，在 overflow handler 检查 | 1 个文件，~10 行 | 无：wrapper 透明委托 |
| B. EngineError 加字段 | engine 加 `output_started`，通过异常传给 loop | 2 个文件 + domain 改动 | 需改 EngineError 构造点（多处） |
| C. StepResult 加字段 | engine 返回 `output_started`，loop 检查 | 2 个文件 + domain 改动 | EngineError 路径拿不到 StepResult |

**推荐 A**。理由：
- `DeltaSink` 已是现成的"delta 发了什么"通道，wrapper 是零新概念
- 不碰 domain 层（`EngineError` / `StepResult`）
- 不碰 engine（它已经在正确的时间调 `on_delta`）
- 改动集中在 `_run_turn` 一个方法内

### 方案 A 实现细节

**`_run_turn` 开头**（`books = TurnBooks()` 之后）：
```python
# R14: provider 可能在流式输出中途报 context overflow。压缩重跑会让前端
# 看到两段矛盾的回答。跟踪 delta 是否已发出，overflow 时据此决定。
output_started = False

def _tracking_delta(text: str) -> None:
    nonlocal output_started
    output_started = True
    if on_delta is not None:
        on_delta(text)
```

**替换所有 `on_delta=on_delta` 为 `on_delta=_tracking_delta`**：
- `:433`（`_ask_for_closing`）
- `:492`（`_model_step`）
- `:1089` / `:1130`（如适用）

**`except EngineError` 块（`:527-528`）**：
```python
if reactive_compacted or exc.kind != _CONTEXT_OVERFLOW:
    raise
if output_started:
    # 模型已开始输出，压缩重跑会让前端看到两段矛盾的回答。
    # OpenCode 同款：`!publisher.record().outputStarted` (step.ts:108-115)
    raise
```

### 复用的契约

- `DeltaSink` 类型（`agent_loop.py:130`）：`Callable[[str], None]`——wrapper 符合
- `_CONTEXT_OVERFLOW` 常量（`agent_loop.py:138`）
- `reactive_compacted` 已有的一次性守卫

---

## 4. 逐文件改动

| 文件 | 改动 | 量 |
|---|---|---|
| `ai_coding/core/agent_loop.py` | 加 `output_started` 标志 + `_tracking_delta` wrapper + overflow handler 检查 | ~12 行 |

**不改**：`EngineError` / `StepResult` / engine / IPC / domain / 配置 / 前端。

---

## 5. 数据流（改后）

```
engine._stream 收到 content delta
  → on_delta("…")
    → _tracking_delta("…")
      → output_started = True        ← 信号在此设置
      → bridge.on_delta("…")        ← 原样委托给前端

engine._stream 后续收到 provider 400 (context overflow)
  → EngineError(kind=_CONTEXT_OVERFLOW)
    → agent_loop except 块
      → if output_started: raise     ← 新增：已开始输出就不压缩
      → else: compact_now + continue ← 原有逻辑不变
```

---

## 6. RIPR 用例

### R（改前必挂）

| # | 用例 | 断言 | 改前 |
|---|---|---|---|
| R1 | delta 已发出后 overflow → 不压缩 | `output_started=True` 时抛异常，不调 `compact_now` | 🔴 无检查，直接压缩 |
| R2 | delta 未发出时 overflow → 正常压缩重试 | `output_started=False` 时走原有路径 | 🟢（回归护栏） |

### I（行为不变）

| # | 用例 | 断言 |
|---|---|---|
| I1 | `_tracking_delta` 委托到原 `on_delta` | delta 内容原样到达 bridge |
| I2 | `on_delta=None` 时不崩 | wrapper 处理 None |
| I3 | 非 overflow 的 EngineError 行为不变 | 照旧 raise |
| I4 | `reactive_compacted=True` 时二次 overflow 照旧 raise | 一次性守卫不变 |

### P

| # | 用例 | 断言 |
|---|---|---|
| P1 | 正常 turn 无 delta → 无异常 | 回归 |
| P2 | 正常 turn 有 delta → 正常完成 | 回归 |

---

## 7. 架构图

```
改前：
  engine delta → on_delta → 前端
  engine overflow → compact_now → continue（不管 delta 是否已发）
                                        ↑ 矛盾：前端已收到前半段

改后：
  engine delta → _tracking_delta → output_started=True → on_delta → 前端
  engine overflow → except 块
                   ├─ output_started=True → raise（不自相矛盾）
                   └─ output_started=False → compact_now → continue
```

---

## 8. 终局推演

1. **provider 流式输出中途报 overflow**：前端已收到若干 token → `output_started=True` → 不压缩，turn 失败，用户看到明确错误而非矛盾输出
2. **provider 在流式开始前报 overflow**（请求阶段即拒）：`output_started=False` → 正常压缩重试，行为与今天完全一致
3. **非 overflow 的 EngineError**：照旧 raise，不受影响
4. **`on_delta=None`**（非 TUI 环境）：wrapper 仍设置标志，overflow 时保守不压缩——安全的默认

---

## 9. 验收

- 全量测试绿（当前基线 `2387 passed, 2 failed`——两个 `test_loop_digest` 预存失败）
- `ruff check ai_coding/` + `mypy ai_coding/` 干净
- 真机：构造流式输出中途 overflow 的场景（或用 fake engine 驱动），确认不压缩重跑


---

## 10. 核验结论（实施时推翻原方案）

### 10.1 保护在下一层，而且更严

`ai_coding/ai/openai_engine.py:428-439`：

```python
except Exception as exc:  # noqa: BLE001 — engine boundary, re-raised typed
    if text_parts or acc:
        raise EngineError(
            f"stream failed after {len(text_parts)} text chunk(s) and "
            f"{len(acc)} partial tool call(s) had already been delivered: {exc}"
        ) from exc
    raise
```

`text_parts` 与 `on_delta` 在 `_stream` 里是同一处追加：
```python
if content:
    text_parts.append(str(content))
    if on_delta is not None:
        on_delta(str(content))
```
所以「投递过文本」与「摘要器看到输出」等价，且由引擎先判。失败后抛出的 `EngineError`
**没有 kind**，循环里 `exc.kind != _CONTEXT_OVERFLOW` 直接 raise——即本文 §1 想要的行为
已经成立。

它比原方案更严：`acc`（半截工具调用）也触发，而 `on_delta` 对工具调用不触发。

### 10.2 唯一能造出该场景的是假 engine

`ChatEngine` 的实现只有两个：
- `OpenAIChatEngine`（`ai/openai_engine.py:252`）—— 有上述保护
- `FallbackEngine`（`ai/fallback_engine.py:27`）—— 纯委派，`step` 转调下层

写了一个脚本化假 engine：先 `on_delta("前半段")` 再抛 `EngineError(kind="context_overflow")`。
**原代码下它确实会重试**，但这正是问题：没有真实 engine 能产生这个组合。

### 10.3 判决

按「冗余即砍」：一条**没有调用方走到的路径**上的第二份规则拷贝，砍掉。
未改 `agent_loop.py`。

### 10.4 留下的东西

`tests/test_overflow_safety.py`（7 个用例，全部在**未改动的代码**上通过）：

| 用例 | 钉住的行为 |
|---|---|
| `test_an_overflow_before_any_output_still_compacts_and_retries` | 输出前被拒 → 压缩并重试（不能回归） |
| `test_a_failure_that_is_not_an_overflow_is_untouched` | 非 overflow 失败 → 不重试 |
| `test_a_second_overflow_is_still_refused_once_compaction_was_tried` | `reactive_compacted` 一次性守卫仍有效 |
| `test_deltas_still_reach_the_caller_unchanged` / `test_no_delta_sink_does_not_break_the_turn` | delta 管道透明 |
| `test_the_real_engine_streaming_first_blocks_the_retry` | **真 `OpenAIChatEngine._stream`**：流式产出后 provider 报错 → 不重试 |

最后一条是本文唯一不可替代的部分：它驱动真实引擎（而非替身）跨过引擎边界，
因为规则的所有权就在那一层。

### 10.5 教训

债务清单这条是从 `agent_loop.py:528` 单点读出来的，没追 `EngineError` 是谁抛的、带什么 kind。
**"某个地方没有检查 X" 不等于 "X 没被检查"** —— 先确认抛异常那一层的契约，再决定在哪加。
