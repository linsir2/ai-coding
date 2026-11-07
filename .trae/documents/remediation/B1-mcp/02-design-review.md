# B1 MCP 方案评审

评审对象：`01-research.md`（11 项）
评审方式：只读代码抽查 + 逐项门禁
结论：**CHANGES**（全部阻塞项已裁决为强制修正，直接进入 S3）

## 阻塞项与裁决

| # | 阻塞项 | 裁决（S3 必须照此实现） |
|---|---|---|
| 1 | MCP-04 前提不成立：`structured` 已在 `turn_close._bounded_data` 限到 8k，只有 `output` 无界；且 `infra/text_clip.clip_head_tail` 是现成实现 | 删除自造 head/tail；`_join_text` 末尾调用 `clip_head_tail(text, _MAX_RESULT_CHARS)`，新增 `_MAX_RESULT_CHARS = 50_000`（字符），加 `[truncated]` 说明。不新增第二套截断 |
| 2 | MCP-01 在 `mode=enforce` 下 `spawn_argv` 是 fail-closed，方案却捕获后原样启动 = 静默无沙箱 | 三分支：`sandbox=False` 原样；`sandbox=True` 且 `policy.mode=="enforce"` 且包装失败 → **拒绝该 server**（记 error，不 spawn）；其余模式 → 回退直连 + Degraded |
| 3 | MCP-07 严重度前提错误；把未知 server 映射 NOT_FOUND 会让其从可重试变不可重试 | 只新增 3 个码：`mcp_timeout`→`DEADLINE_EXCEEDED`；`mcp_transport`→`UNAVAILABLE`；`mcp_tool_error`→`UNAVAILABLE`。**不使用 NOT_FOUND**；`_RETRYABLE` 只加 `mcp_timeout` |
| 4 | MCP-02 用例修复前即通过 | 标注为回归护栏（非红转绿），测试注释写明 |
| 5 | MCP-03 共享退役路径用例恒真；`execution=0` 会导致每次调用退役 | 用例改为"仅超时"路径；实现处校验 `execution > 0` 才启用退役 |
| 6 | MCP-05 `tools_pages` 只写不读；重复 cursor 时 `incomplete=False` 属谎报 | 读 `tools_pages` 写入 catalog 声明；重复 cursor 视为异常，`incomplete=True` |
| 7 | MCP-09 端口回退后 `client_info` 可能绑定旧端口 | 回退时清空已缓存的 client_info（若 TokenStore 提供），否则在 `_connecting_detail` 说明；测试只断言"回退后能启动" |
| 8 | MCP-08 现有测试名 "resets the pause" 与新语义不符 | 改名 + 新增"稳定 ≥60s 才重置"用例 |
| 9 | MCP-01 空 command 无测试；args 断言方式 | 新增空 command 拒绝用例；args 按列表元素断言（不是拼字符串） |
| 10 | MCP-10 仍列在文档 §4.2 | B1 **不实现** MCP-10。MCP-10a（warn 判定）转 W2/B4；MCP-10b（builder 注释）转 W1/B5 |

## 非阻塞（记录，S3 可选）

- MCP-03 慢但健康的调用会被退役：已在方案声明，保留。
- MCP-01 bwrap 下 `~/.npm` 可写性与 deny 网络未实跑：留待人工真机验证。
- DeadlineExpired 放弃后 SDK 响应错配未复现：MCP-03 的重建策略即为缓解。

## 通过项

- `mcp.sandbox` 默认 false 时 `_factory_for` 原样返回，零行为变化。
- `roots` 未漏；`world.py` 构造顺序可用。
- MCP-02 与 `note_transport_death` 不冲突。
- MCP-06 用 `_is_connectable_shape` 判定，不引入 force。
- 无新依赖、未改默认窗口。

## 进入 S3 的强制清单

MCP-01、02、03、04、05、06、07、08、09、11（10 项）。MCP-10 已转出。
