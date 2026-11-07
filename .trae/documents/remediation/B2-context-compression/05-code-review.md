# B2 上下文工程 + 压缩 代码评审（S5）

## 结论：APPROVE（第一轮 REQUEST-CHANGES，整改后通过）

## 第一轮 P1（已修）
- 快照可能让视图大于原始历史：无 checkpoint 时 L0 只省一点、再注入 8k 快照，实测 45,025→48,696 token。
  → 整改：仅在**有效 checkpoint** 或**实际节省量 ≥ 快照成本**时挂载，并在返回前做直接校验（`context_compression.py:1799-1802,1885-1892`）。

## 第一轮 P2（已修）
1. `live>0` 时仍每迭代全量 `working_history_tokens`（O(n)）→ 改为 `observe_lazy`，仅在读数重复时扫描（`request_assembly.py:317-356,409-420`）。
2. `_no_room_line` 未计入 `used`、显式 @ 无空间用 `continue` → 全部计入并 `break`（`request_assembly.py:981-984,993-996,1010-1013`）。
3. `_l0_view` memo key 缺 session → 补 `session_id`（`context_compression.py:1700-1709`）。

## 复审确认
- 首次压缩成功那一轮仍能挂快照（`out[0]` 保留 note，`read_checkpoint` 可回落）。
- 残留（记录）：门槛按内容字符数、不含 tool_call 参数，CJK 快照可能低估 token。

## 已核对通过
- CMP-01 挂载在 hard_trim 后、仅 out 变化时挂、`/compact` 无变化不挂、无两份。
- CMP-02 key 含 window/protect/anchor/条数/尾 id/session，追加必失效。
- CMP-03 公式与 48k→16k 正确；CMP-04 按真实长度比较；CMP-05 键含 turn_count。
- CTX-01/02/03 无整读；非文本清单不误伤 `.log/.ipynb`。
- CTX-07a/b/c 口径正确，golden 仅 3 行变化且与设计一致。
- 无删测/弱化；越界仅已声明的 `offered_tools=False` 一处。

## 波次门禁
整改后全量 pytest **exit 0**；ruff 全绿；mypy 0 错误。
