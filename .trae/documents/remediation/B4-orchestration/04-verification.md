# B4 编排 / 计划 实现验证（S4）

## 结果
- 定向（turn_policy / interventions / escalation / agent_loop_react / turn_close / plan_codex）→ 全绿
- 扩展相关 464 项 → 全绿
- 全量 pytest → **exit 0**
- `ruff` 全绿；`mypy ai_coding` → 186 文件 0 错误

## 与 01/02 的一致性
- ORC-01：只加注释（无步数/墙钟上限，仍受窗口、阶梯、模型自停、goal 轮次约束）+ 钉扎测试。
- ORC-02：`cap = min(max(0, n), L3)`；默认 3；`models.py` 与 `config.example.yaml` 注释同步。
- ORC-03：阈值 guard 提到 repeat 之前；`threshold=0` 连 repeat 一起关闭，保留 L3。
- ORC-04：改用"预测下一步 `decide_next_iteration`"，`FINISH` 时不注入、写入 `note`（`note or reason` 兜底）。
- ORC-05：strategy / search 后缀 `[<gate> ladder L<n>]`；task / fatigue 与 token 预算 unaffected。
- MCP-10a：schema + catalog 加性统计。

## 偏离（需代码评审确认）
- ORC-04 对 `iteration budget exhausted` 与 `search unconverged` **不转换**：G8-2 的收尾调用会读到该 nudge，转换会让 `test_strict_policy_ignores_the_nudge_cap` 的 5 次调用断言失效。
- 额外编辑 `tests/test_tool_surface_warning.py`（MCP-10a 必需，不在白名单但属对应测试）。

## 已知限制
- 显式设 `max_strategy_nudges=1/2` 的用户会看到比过去更少的 nudge（过去是死配置）。
- `docs/features.md` 等三处文档仍写默认 1，W3 统一更新。
