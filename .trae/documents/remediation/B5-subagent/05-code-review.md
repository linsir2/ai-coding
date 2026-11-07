# B5 子代理 / 后台 代码评审（S5）

## 结论：APPROVE（第一轮 REQUEST-CHANGES，整改后通过）

## 第一轮 P1（已修）
1. **SUB-05 授权回归**：child gate workspace 切到 worktree 后，`GrantStore` 按 workspace 读盘，主树 "always" 授权对子代理整体失效，会对已授权调用重问。
   → 整改：**回退该改动**，gate 仍用主 workspace（`subagent.py:1835-1845`），注释写明已知限制；测试改为断言"worktree 路径仍判 outside"。**SUB-05 标记 DEFERRED**。
2. 退休 task 异常无人取（每次 spawn 重复退休）→ `_retire_done` 消费异常并重置 `idle_since`；`shutdown` await `drain_retirements`（`subagent.py:1424-1436`、`scheduler.py:1256-1264`）。
3. close 不复核状态，可能取消刚转 running 的句柄 → 退休路径在同一同步块复核 `_closing` / resting / `turn_task`（`subagent.py:1152-1164`）。

## 第一轮 P2（已修）
1. shutdown 处理终态行、串行 N×20s → 跳过终态、30s 总时限（`scheduler.py:1225-1264,1289`）。
2. fanout 无 `_closed` 守卫 → 已加（`scheduler.py:161/262/328/419/877/897`）。
3. 预登记 running 未清除 → `_settle` 覆盖 idem 状态并写 result；无 result 的终态改报冲突（`scheduler.py:975-990`）。
4. prompt 预算 `break` 丢后续短行 → `continue`；`agent_loop.py` 无关 surface 重构已回退，仅保留 `build(agents=...)`（`prompt.py:147-148`、`agent_loop.py:3542`）。
5. 两条空断言测试 → 补为可失败断言。

## 复审确认
- SUB-04 `agents=None` 逐字节不变；单条 120 / 总 600 生效；`subagents()` 有 getattr 防御。
- SUB-01 无 branch 时输出与旧完全一致；有 branch 无 commit 写"未提交"。
- SUB-03 预登记在 `create_task` 前，三路清理齐全；`spawn()` 全同步无竞态。

## 残留（P3，不阻塞）
- shutdown 先取消全部 task（含 retire），中途被取消的退休可能漏删 worktree。
- `_retire_done` 异常分支与 retiring 复核缺专门用例。

## 波次门禁
整改后全量 pytest **exit 0**；ruff 全绿；mypy 0 错误。
