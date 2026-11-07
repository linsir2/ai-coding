# B4 编排 / 计划 代码评审（S5）

## 结论：APPROVE（第一轮 REQUEST-CHANGES，整改后通过）

## 第一轮 P1（已修）
1. **ORC-04 收尾状态硬编码**：转换后一律 `MAX_ITERATIONS`，token 预算/墙钟收尾会被误标为"步数上限"，且策略账本丢失 exhausted。
   → 新增 `terminal_status()`（`turn_close.py:65-78`），按 `next_step.reason` 映射 TIMEOUT / BUDGET / MAX_ITERATIONS，`terminal_outcome` 与 POST→FINISH 共用（`agent_loop.py:1450,1469-1473`）。
2. **ORC-03 stale L3 不可达**：`_triggered` 在 stale 判定前 `return False`，`threshold=0` 时 stale L3 永不触发。
   → stale L3 判定提到 `if not threshold` 之前（`interventions.py:187-196`），保留 repeat/stale 的 L1/L2 关闭语义；新增两条用例。

## 第一轮 P2（已修）
1. `_strategy_due` 收尾恒报 L4 → 返回实际 level（`interventions.py:437`），cap=2 收尾为 L2。
2. `max_strategy_nudges<2` 时 `strict_repeats` 不武装 → 写入 `config/models.py:572-576` 字段注释。

## 已核对通过
- ORC-04 预测链正确：只有下一步 FINISH 才转换；C3 返回 NUDGE 不转换；转换在 `_note_intervention` 之前。
- ORC-02：`cap=min(max(0,n),3)`；`n=0` 关闭；四处默认 3 一致；`armed()` 实现未改。
- ORC-03：`threshold=0` 关闭 repeat/stale 建议档，L3 保留；plan/readonly 静默有测试说明。
- ORC-05：仅 strategy/search 加**后缀**，`turn_digest` 的 `startswith` 不受影响；task/fatigue/token 未动。
- MCP-10a：加性统计无重复；告警后不再构建 catalog。
- ORC-01：仅注释 + 钉扎测试（护栏）。
- 未改 `tests/golden/**`，无弱化断言。

## 已知限制
- `max_strategy_nudges=1` 时 L2 不出现、`strict_repeats` 不武装（已在配置注释声明）。
- token 已满时 `_ask_for_closing` 返回空，该角落注入的 nudge 可能无人读（已记录）。
- `mcp_chars` 含 catalog、`tools_chars` 仅 schema，口径差异已记录。
