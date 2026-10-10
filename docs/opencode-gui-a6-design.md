# A6 设计：provider 返回空回复时，回合不再以裸异常收场

> 状态：**待拍板**（未写实现代码）
> 前置：A4 `f457bf5`、A5 `cf0283d`
> 触发：真机 E2E 中整个回合报废，屏幕出现 `[turn] 错误：ValueError: assistant messages must carry content or tool_calls`

## 0. 范围

| 项 | 内容 |
|---|---|
| 修 | 一个回合因「模型只回了思考、没有正文也没有工具调用」而死亡，且用户看到裸 Python 异常串 |
| 不修 | 不动 `ChatMessage` 的不变式（它是对的）；不加重试；不改引擎的 FailureKind 分类 |

后端改动集中在 3 个文件；**前端、IPC、wire 零改动**。

## 1. 现状核准（事实，带行号）

| 位置 | 事实 |
|---|---|
| `domain/message.py:139-141` | assistant 消息既无 content 也无 tool_calls → `ValueError` |
| `domain/message.py:122-127` | 文档字符串**明写这是有意的**：「reasoning without speech or action is not a message — a reasoning-only step is a loud ValueError here, not silent corruption downstream」 |
| `core/agent_loop.py:1119-1121` | 先 `_emit(AssistantMessage(...))`，再 `session.add_message(assistant_message(step, ...))`——异常在**发射之后**抛出 |
| `core/agent_loop.py:582` | 回合级只接 `except (DeadlineExpired, EngineError)` → ValueError **不被接住** |
| `server/handlers.py:234-241` | IPC 兜底 `except Exception` 把它变成 `turn.finished{status:"error", reason:"ValueError: assistant messages must carry content or tool_calls", iterations:0}` |
| `core/turn_close.py:91-104` | `failed_outcome()`：`DeadlineExpired` 有专门的**人类可读** reason 分支，其余一律 `reason=str(error)` |
| `ai/openai_engine.py:441-447` | 引擎返回 `StepResult(text="".join(text_parts) or None, tool_calls=..., finish_reason=...)`——空回复在这里就已成形 |
| `core/agent_loop.py:508` | 若不变式不触发，`if not step.tool_calls:` 会直接结束回合 → **无声的空回复** |
| `core/plan_runner.py:595` | 另一个 assistant 构造点，但 `summary` 必带 step statement（`:634`），**不是第二个踩点** |
| `grep tests/` | 没有任何测试钉住这个 ValueError——**没有被固化的契约要维护** |
| 频次 | 今天十几次真机回合撞到 1 次（dashscope/qwen3.7-flash 返回 `finish_reason=stop` 且正文为空） |

## 2. 根因

`ChatMessage` 的不变式是**对的**，它复刻了 provider API 的硬要求：一个既无 content 也无 tool_calls 的 assistant 消息发回去会被上游拒绝。**问题不在不变式，在于循环把「不变式被违反」当成了不可处理的意外**——它一路冒泡到 IPC 兜底，在那里被降级成 `reason=str(error)`，用户看到 Python 异常文本，而回合没有走正常收尾（`_close_turn` 的持久化、`iterations` 计数都丢了，屏幕上就是 `iterations: 0`）。

**对照 `DeadlineExpired`**：它同样是「中途失败」，但 `failed_outcome` 为它写了一个人类可读的 reason。空回复缺的就是这一份。

## 3. 设计

### 3.1 新增一个异常类型（唯一的新契约）

```
ai_coding/domain/loop.py
  class EmptyStepError(Exception)
      """模型只回了思考内容，既没有正文也没有工具调用。"""
      finish_reason: str | None      # 供应商给的理由，如 "stop"
```

**为什么放 `domain/loop.py`**：`StepResult` 与 `TurnStatus` 都在这里，异常描述的正是「一个 StepResult 无法成为消息」。`turn_close.py` 已从该模块导入 `TurnStatus`，不新增依赖方向。

**为什么不放引擎**：空回复在 `openai_engine.py:441` 就已成形，但那是**一个引擎**的判断；`FallbackEngine` 与测试替身同样能产出空 step。循环侧一处检查覆盖全部引擎。

**为什么不加重试**：`r2-analysis.md` 记的既有原则是「响亮失败优于静默恢复」；无差别重试可能在内容过滤这类确定性场景上白花钱。回合终止 + 明确说明是诚实的处置。

### 3.2 循环：在唯一那一行判断

```
core/agent_loop.py:1119-1121（保持发射在前）
    self._emit(AssistantMessage(...))          # 不动：思考内容仍然上屏，信息不丢
+   if not step.text and not step.tool_calls:
+       raise EmptyStepError(step.finish_reason)
    session.add_message(assistant_message(step, now_utc()))
```

**发射在前**是有意的：模型确实思考了，屏幕上有 `+ Thought` 可看，再跟一句「它没有回答」，比什么都不显示更有信息量。

### 3.3 回合收尾：人类可读的 reason

```
core/turn_close.py: failed_outcome()
+   if isinstance(error, EmptyStepError):
+       return TurnOutcome(
+           status=TurnStatus.ERROR, text=None, iterations=iterations,
+           reason=f"模型只返回了思考内容，没有回答（finish_reason={error.finish_reason or 'unknown'}）",
+       )
```

沿用 `DeadlineExpired` 已有的分支形状，不新增机制。

### 3.4 循环必须接住它

```
core/agent_loop.py:582
-   except (DeadlineExpired, EngineError) as exc:
+   except (DeadlineExpired, EngineError, EmptyStepError) as exc:
```

**这是最关键的一行**：不接住的话异常还是会冒到 IPC 兜底，等于没修。

## 4. 执行范围（3 个文件）

| 文件 | 改动 |
|---|---|
| `ai_coding/domain/loop.py` | +1 个异常类（含 `__all__`） |
| `ai_coding/core/agent_loop.py` | +3 行判断（含 except 元组加一项） |
| `ai_coding/core/turn_close.py` | `failed_outcome` +5 行分支 |

新增依赖 0；不改 `ChatMessage`；不改引擎；不改前端；不改 wire。

## 5. 数据流影响

```
改前  provider → StepResult(text=None, tool_calls=(), reasoning="…", finish_reason="stop")
      → add_message → ValueError → 逃出循环 → handlers.py:234 兜底
      → turn.finished{status:"error", reason:"ValueError: assistant messages must carry…", iterations:0}
      → 屏幕：[turn] 错误：ValueError: assistant messages must carry content or tool_calls

改后  provider → 同上
      → _emit(AssistantMessage) 照旧（屏幕仍能看到 + Thought）
      → 抛 EmptyStepError → 被 582 接住 → failed_outcome 给出人话
      → 正常 _close_turn：会话落盘、iterations 正确、TurnFinished(status=error)
      → 屏幕：[turn] 错误：模型只返回了思考内容，没有回答（finish_reason=stop）
```

**事件序列完全不变**（还是 `turn.finished{status:"error"}`），前端一行不用改——`turnEndMarker` 早就按 `status==="error"` 渲染 reason。

## 6. TDD 用例（RIPR）

| # | 用例 | R 可达 | I 交互 | P 预测 | R 显明 | 层 |
|---|---|---|---|---|---|---|
| T1 | 空 step 结束回合且 reason 是人话 | `AgentLoop.process_input` + 假引擎返回 `StepResult(text=None, tool_calls=(), reasoning="想了一下", finish_reason="stop")`（复用 `tests/test_loop_events.py:325` 的假引擎写法） | 构造 session + loop，跑一回合 | outcome.status is ERROR；reason 含「没有回答」与 `finish_reason=stop` | 断言失败时打印实际 reason | pytest |
| T2 | 会话里**没有**空 assistant 消息 | 同上 | 检查落盘消息 | 无 content 为空且无 tool_calls 的 assistant | — | pytest |
| T3 | 思考内容仍然上屏 | 同上，订阅事件 | 收事件流 | 有 `AssistantMessage` 且带 reasoning | — | pytest |
| T4 | `failed_outcome(EmptyStepError("stop"))` 直测 | 纯函数 | 直接传 | reason 含中文与 `stop`；text is None | — | pytest（`tests/test_turn_close.py`） |
| T5 | **回归**：正常 step 不受影响 | 假引擎返回 `text="ok"` | 跑一回合 | COMPLETED，无异常 | — | pytest |
| T6 | **回归**：工具步（text=None + tool_calls）不受影响 | 假引擎返回 `text=None, tool_calls=[...]` | 跑一回合 | 不抛 EmptyStepError | — | pytest（这条最关键：`text=None` 在工具步是合法的） |
| T7 | 真机：屏幕不出现 `ValueError` | — | tmux | grep 无 | — | E2E |

> T6 是本设计的生死线：空判定必须同时要求「无正文**且**无工具调用」，否则会误杀所有工具步。

## 7. 架构图

```
provider ──► OpenAIChatEngine._stream
              └─► StepResult(text=None, tool_calls=(), reasoning=…, finish_reason="stop")
                     │
                     ▼
              AgentLoop._one_step (agent_loop.py:1119)
                ├─ _emit(AssistantMessage)          ← 思考仍上屏
                ├─ if not text and not tool_calls:  ← 新增判断
                │     raise EmptyStepError(finish_reason)
                └─ session.add_message(...)        ← 不变式仍在，未被绕过
                     │
                     ▼
              except (…, EmptyStepError)  (agent_loop.py:582)   ← 接住
                     │
                     ▼
              failed_outcome (turn_close.py:91)
                └─ EmptyStepError → status=ERROR, reason=人话
                     │
                     ▼
              _close_turn → 落盘 + TurnFinished(status=error)
                     │
                     ▼
              前端 turnEndMarker（不改）→ [turn] 错误：模型只返回了思考内容…
```

## 8. 明确不做

| 不做 | 理由 |
|---|---|
| 放宽 `ChatMessage` 不变式 | 它复刻上游 API 的硬要求，放宽会让**下一轮**请求被拒；且文档明确写了要响亮 |
| 加一次自动重试 | 白花钱风险；项目原则是响亮失败优于静默恢复 |
| 加 `FailureKind.EMPTY_RESPONSE` | 要连带决定是否属于 `WORTH_ANOTHER_MODEL`（重试语义），是把一次小修变大 |
| 改 `handlers.py:234` 的兜底 | 它是**故意的**最后防线（"a bug must not hang the UI"），本次不动 |
| 改前端 | 事件形状没变，`turnEndMarker` 已按 error 渲染 reason |

## 9. 待你拍板

| # | 事项 | 我的建议 |
|---|---|---|
| ① | reason 文案 | `模型只返回了思考内容，没有回答（finish_reason=stop）`。带上 finish_reason 是为了让人一眼看出是上游行为而非本项目 bug |
| ② | status 用 `ERROR` | 沿用现有枚举，不新增 `TurnStatus` 值——它确实是错误，不是"完成"也不是"预算耗尽" |
| ③ | 不加重试 | 若你更看重"用户不丢回合"，可改为有界重试一次（照抄 `reactive_compacted` 的形状），代价是多一处控制流与一次模型调用 |

## 10. 实施后自查

| 现象 | 结果 |
|---|---|
| 模型只思考不回答 | `[turn] 错误：模型只返回了思考内容，没有回答（finish_reason=stop）`，`+ Thought` 仍在 |
| 回合收尾 | 正常落盘，`iterations` 不再是 0 |
| 工具步 | 不受影响（T6 钉住） |
| 正常回合 | 不受影响（T5 钉住） |
| 前端 | 零改动 |