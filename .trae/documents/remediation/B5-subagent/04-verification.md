# B5 子代理 / 后台 实现验证（S4）

## 结果
- 定向 5 文件（subagent_runner / scheduler / tool_subagent / prompt / subagent_guards）→ 157 passed
- 加 `test_subagent_handles.py` → 183 passed
- 相邻 prompt / agent_loop 9 文件 → 184 passed
- `ruff` 全绿；`mypy ai_coding` 186 文件 0 错误

## 与 01/02 的一致性
- SUB-02：`idle_since` 覆盖 idle 与 interrupted；`shutdown()` 首行 `_closed=True` 拒绝准入；`_closing` 防并发 close；TTL 只读 `idle_since`。
- SUB-03：仅 `run()` 在 `create_task` 前预登记，失败/取消/异常三路清除。
- SUB-04：`<agents>` 置于 `<skills>` 后，单条 ≤120、总 ≤600，`getattr` 防御，`agents=None` 字节不变。
- SUB-05：仅 `workdir` 非空时切 child gate workspace，注释写明"worktree 未提交规则不可见"。
- SUB-01：无 branch 时输出与旧一致；有 branch 无 commit 写"未提交"。
- MCP-10b：`tools/builder.py` 加注释说明子 agent 不注册 MCP 工具。

## 偏离（已接受）
- `_retire_idle_handles` 返回 task 列表供用例显式 await；`_prune_background` 签名保持 `-> None`。
- SUB-05 的边界测试用 `read` 观察（`write` 属 WARN，规则层先返回 confirmation，边界文案不可达）。

## 已知限制
- TTL 剪枝只在 `spawn_background` 时触发，静默会话不会自动退休（有界但不是周期扫描）。
- 20s close 上限在大仓库上可能只留孤儿 worktree（已提交内容不丢）。
- worktree 中未提交的项目规则对子代理不可见（worktree = HEAD）。

## 波次门禁
W1 全量 pytest：**exit 0**。
