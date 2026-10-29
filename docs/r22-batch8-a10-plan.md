# 批次 8-3 方案（R22·A-10 工具时长把审批等待算进去）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 3 项、`docs/r22-feature-matrix.md` §5 A-10、
> `turn-2.jsonl` 里 `ToolFinished{duration_ms: 32534, error: "denied"}`。

## 1. 调研

### 1.1 计时起点在审批之前

`core/dispatch.py::_handle`：

```python
ToolStarted(...)                    # 388
started = time.monotonic()          # 395
result, denied, halted = await self._decide(...)   # 397 —— 审批等待在它里面
ToolFinished(duration_ms=int((time.monotonic() - started) * 1000))  # 407
```

`_decide` 的 WARN 分支会 `await self._ask(...)`（534-540），而 `_ask` 等的是**人**（审批卡 /
REPL 提示），`approval_timeout: 0` 时没有上限。于是 `duration_ms` = 人思考的时间 + 工具真正的执行时间。
实测 `git status` 卡片 `28.9s`，其中 28.5s 是等人；`turn-2.jsonl` 里 32,534ms 的那条同理。

### 1.2 为什么不能直接把 `duration_ms` 改成"执行时间"

`duration_ms` 还有三个消费者，它们要的都是**墙钟跨度**：

| 消费者 | 用法 | 改成执行时间会怎样 |
| --- | --- | --- |
| `core/export_otlp.py:86-125` | `endTimeUnixNano = start + duration_ms` | span 结束时间会早于真实的 `ToolFinished.ts`，轨迹对不上 |
| `app/trace_text.py:373` | `tool_ms` 汇总 | 每轮的工具总耗时被低估 |
| `core/export_html.py:503` | 工具列表 `_seconds(duration_ms)` | 报告里的耗时与轨迹不一致 |

所以：**保留 `duration_ms` 为墙钟总时长，另加一个 `approval_ms`**，卡片把等待部分标出来。

### 1.3 审批只发生在一个地方

- `_ask` 只在 WARN 分支调用；ALLOW 调用不经过 `approval.approve`（`test_deny_does_not_ask_approval` 已钉住）；
- `_decide` 只有一个调用者（`_handle:397`），所以把等待时间从 `_decide` 里带出来只需改一处解包。

## 2. 方案

### 后端

1. `core/events.py::ToolFinished` 增加 `approval_ms: int = 0`（放在 `duration_ms` 之后，带注释：
   "等人按审批的毫秒数；0 表示没有等待，`duration_ms` 仍是墙钟总时长"）。
2. `core/dispatch.py`：现有函数体改名为 `_decide_inner(..., timing)`，`_decide` 变成一层薄壳：

   ```python
   timing = {"approval_ms": 0}
   result, denied, halted = await self._decide_inner(..., timing=timing)
   return result, denied, halted, timing["approval_ms"]
   ```

   - WARN 分支在 `_ask` 前后各取一次 `time.monotonic()` 写进 `timing`；
   - 这样 18 个早退一个都不用改（评审指出逐处加第 4 元素"漏一处就解包炸"），
     审批后的 STOPPED/TIMED_OUT/declined/blocked/reused/最终返回都会自动带上实测值。
3. `_handle` 解包 4 元组，把 `approval_ms` 填进 `ToolFinished`。

### 桥与前端

4. `server/bridge.py::to_wire(ToolFinished)` 只在 `approval_ms > 0` 时写 `"approvalMs"`
   （沿用文件里 `agent`/`readonly` 的"有值才写"惯例：没有审批的旧 payload 逐字节不变）。
5. `state/session.ts` 的 tool part 增加 `approvalMs: number`（注释说明"等人按审批的时间，
   包含在 `durationMs` 里"）；构造点四处都要补：`tool.started`（`session.ts:565`）、
   漏了 start 的兜底（`session.ts:616`）、`replay.ts:222`、测试工厂 `toolcard-render.test.tsx`
   与 `reducer.test.ts` 的期望对象；`tool.finished` 从 `data["approvalMs"]` 读（缺省 0）。
6. `util/toolmeta.ts` 增加纯函数 `formatToolDuration(durationMs, approvalMs)`：
   - 先 clamp `0 <= approvalMs <= durationMs`；
   - `approvalMs < 1000` **或** `< durationMs 的 10%` → 只显示总时长（自动批准的抖动是噪声）；
   - 否则 `${formatDuration(total)}（审批 ${formatDuration(approval)}）`，例如 `28.9s（审批 28.5s）`。
7. `component/toolcard.tsx` 的那一格改用它。括号式保留一个总数可扫读；denied 时同样适用
   （那时"运行"是 0，括号里的等待反而解释了这个数）。

### 不做

- 不改 `ToolStarted`/`ToolFinished` 的 `duration_ms` 语义（见 1.2）；
- 不在 HTML 报告/`/trace` 里拆审批（它们要总时长，且本次没有报障；`approval_ms` 已经进了轨迹，
  将来要拆只需读那个键）；
- 不做审批子 span（会改 OTLP 的 parent 结构，超出 A-10）。

## 3. 测试（先红后绿）

| 层 | 测试 | 断言 |
| --- | --- | --- |
| 后端 | `tests/test_tool_dispatch.py`：WARN 调用的审批回调 `await asyncio.sleep(0.02)` | `ToolFinished.approval_ms >= 20` 且 `duration_ms >= approval_ms` |
| 后端 | 同文件：ALLOW 调用（`read`） | `approval_ms == 0` |
| 后端 | 同文件：审批后 declined / STOPPED / TIMED_OUT | 三例的 `approval_ms` 都 ≥ 实测等待（不是 0，也不是漏带） |
| 桥 | `tests/test_server_bridge.py::test_tool_finished_passes_every_field` | 0 的那例 payload 逐字不变；新例 `approval_ms=42` → `"approvalMs": 42` |
| 前端 | `test/toolmeta.test.ts::formatToolDuration` | `(28900, 28500)` → `28.9s（审批 28.5s）`；`(820, 0)` → `820ms`；`(1500, 900)` → `1.5s`（低于阈值不标） |
| 前端 | `test/toolcard-render.test.tsx` | 带 `approvalMs: 28500` 的帧含 `（审批 28.5s）`；无审批的帧不含 |

## 4. 完成记录（2026-10-29）

- 后端：`ToolFinished.approval_ms`；`_decide` 薄壳 + `_decide_inner(timing)`；`_handle` 填事件。
  测试：YES/NO/STOP/ALLOW/TIMED_OUT 五条（`tests/test_tool_dispatch.py`、
  `tests/test_dispatch_approval_timeout.py`）。
- 桥：只在 `> 0` 时写 `approvalMs`；0 的那条既有 exact-dict 测试原样通过。
- 前端：part 四处构造点补齐（`session.ts` 两处、`replay.ts`、测试工厂）；
  `formatToolDuration`（1s 且 10% 双阈值 + clamp）；卡片那一格 `flexShrink={0} wrapMode="none"`。
  全量 `bun test` 550 例 + `tsc` 干净。
- 真机（帧 `155`）：`✓ 已运行命令 · … git status · 14.1s（审批 13.9s）`；
  同一调用轨迹行 `duration_ms:14145, approval_ms:13918`。
- 评审并入：超时文案改用**等待前**算出的预算（`within 0.5s`，不是 0）；时长文本不参与折行。

## 5. 验收

1. `pytest tests/test_tool_dispatch.py tests/test_server_bridge.py tests/test_loop_events.py -q` 绿；
2. 前端 `bun test test/toolmeta.test.ts test/toolcard-render.test.tsx` + 全量 `bun test` 绿、`tsc` 干净；
3. 真机一帧：bash 审批卡批准后，工具卡片显示 `· X.Xs（审批 Y.Ys）`（Y ≥ 1s 时才出现）；
4. `ruff` / `mypy` 干净；`docs/data-contracts.md` 的事件字段表补 `approval_ms`。
