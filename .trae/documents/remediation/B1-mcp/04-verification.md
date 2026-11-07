# B1 MCP 实现验证（S4）

## 结果
- 定向：`tests/test_mcp.py test_mcp_call_tool.py test_mcp_lifecycle.py test_mcp_catalog.py test_mcp_oauth.py test_mcp_progress.py` → 131 passed, 1 skipped（gated 真实 stdio）
- 新增 `tests/test_mcp_config_fields.py` → 1 passed
- 真实子进程（`AI_CODING_MCP_STDIO=1`）：`test_mcp_progress` 1 passed；`test_mcp_stdio` + `test_mcp_end_to_end` 5 passed
- `ruff check ai_coding tests/test_mcp*.py` → 全绿
- `mypy ai_coding` → 186 文件 0 错误（波次末复核）

## 与 01/02 的一致性
- MCP-04 使用 `clip_head_tail`，未自造实现；`_MAX_RESULT_CHARS=50_000`。
- MCP-01 三分支：关闭原样 / enforce 失败拒绝 / 其它回退 + Degraded。
- MCP-07 只增 3 码，未使用 NOT_FOUND。
- MCP-03 仅 `execution>0` 时退役；与 `note_transport_death` 共用重建。
- MCP-05 读取 `tools_pages` 并写入 catalog 声明；重复 cursor 标记不完整。

## 偏离（已接受）
- MCP-08 在 ok 分支补 `_pause(delay)` 与 `_manual` 标记：仅改重置条件无法消除"连上即掉"的风暴，原方案用例也无法成立。
- MCP-05 `incomplete` 载荷改为 `(name, pages)` 以承载页数。

## 已知限制
- bwrap 下 `~/.npm` 可写性与 deny 网络未实机验证。
- 端口回退后 OAuth `client_info` 与旧端口的绑定仅做了清理/说明，未做跨 AS 重注册。

## 波次门禁
W1 全量 pytest：**exit 0**（2026-11-07）。ruff 全绿、mypy 0 错误。
