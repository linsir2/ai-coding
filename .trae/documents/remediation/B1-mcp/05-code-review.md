# B1 MCP 代码评审（S5）

## 结论：APPROVE

## P0 / P1
无。

## P2（记录，不阻塞）
1. `_retire` 按 server 名定位 owner（`core/mcp.py:1729-1741`）：旧 session 的迟到超时可能把重建后的健康 owner 标死，多一次重连；与既有 `note_transport_death` 同源。
2. `needs_auth` 分支 `_hold` 后 `continue` 未清 `_manual`（`core/mcp.py:1481-1492`）：该 server 之后首次掉线会少等一轮 backoff。
3. `_sandboxed_command` 注释称非 enforce 回退"原始 argv"，实际 `wrap()` 返回 None 时是 `[bash,-c,…]`（`core/mcp.py:1048-1070`）；仍有 Degraded，非静默。
4. `execution=0` 仍原样下传，调用立即 DeadlineExpired（既有语义）。

## 已核对通过
- `sandbox=False` 与旧路径逐行等价；enforce 下 `SandboxGap` 在 spawn 前抛出且拒绝启动；roots 为 `sandbox.live_roots` 且每次重建重取。
- MCP-03 与 `note_transport_death` 共用 `_retire`，dead 幂等，仅 `timeout>0` 接线。
- MCP-04 仅 `_join_text` 一处 `clip_head_tail(50k)`，无二次截断。
- MCP-05 `tools_pages` 被读取；重复 cursor 标不完整；20 页硬停无死循环。
- MCP-07 三码映射，无 NOT_FOUND；`_RETRYABLE` 仅加 `mcp_timeout`。
- MCP-08 手动唤醒不被节流；aclose 能及时退出。
- 测试：多数用例在旧实现上失败；3 个已标注为回归护栏。

## 波次门禁
整改后全量 pytest **exit 0**；ruff 全绿；mypy 0 错误。
