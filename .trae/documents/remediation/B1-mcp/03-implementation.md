# B1 MCP 修复实现记录（S3）

基线：`fd5a929`（工作树并行存在其他批次改动；本文件只描述 B1 白名单内文件）。
范围：MCP-01、02、03、04、05、06、07、08、09、11（MCP-10 转出，未改 `agent_loop.py` / `builder.py`）。

## 验证命令与结果

```text
$ .venv/bin/python -m pytest tests/test_mcp.py tests/test_mcp_call_tool.py \
    tests/test_mcp_lifecycle.py tests/test_mcp_catalog.py tests/test_mcp_oauth.py \
    tests/test_mcp_progress.py -q
131 passed, 1 skipped in 13.42s
（skip 为 gated 的真实 stdio 用例：AI_CODING_MCP_STDIO!=1）

$ .venv/bin/python -m pytest tests/test_mcp_config_fields.py -q
1 passed

$ AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_progress.py -q
1 passed   ← 真实子进程跑通，证明 MCP-11 的字段修正确实生效

$ AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_stdio.py tests/test_mcp_end_to_end.py -q
5 passed   ← 真实 stdio 往返；也证明 sandbox 默认关闭时 spawn 行为零变化

$ .venv/bin/ruff check ai_coding tests/test_mcp*.py
All checks passed!

$ .venv/bin/mypy ai_coding
ai_coding/core/scheduler.py:697: error: Cannot infer type of lambda  [misc]
Found 1 error in 1 file (checked 186 source files)

$ .venv/bin/mypy ai_coding/core/mcp.py ai_coding/tools/mcp_call.py \
    ai_coding/domain/error_codes.py ai_coding/config/models.py
Success: no issues found in 4 source files
```

**mypy 的唯一错误不在 B1 白名单**：`ai_coding/core/scheduler.py:697` 属于并行 SUB 批次（`git diff` 显示该行由 SUB-02/SUB-03 新增），B1 未改该文件。B1 自身核心文件 mypy 已清零。

---

## 逐项实现

### MCP-01 本地 server 接入进程沙箱

**改了什么**
- `ai_coding/config/models.py`：`MCPConfig.sandbox: bool = False`（仅此一处，MCP 段）。
- `ai_coding/core/mcp.py`
  - `MCPManager.__init__` 新增只增关键字 `sandbox=(policy, backend)`、`sandbox_roots=()`；保存 `_sandbox_policy/_sandbox_backend/_sandbox_degraded`。
  - 新增 `_sandboxed_command(command, policy, backend, roots, *, emit=None) -> (argv, fallback_reason)`。空 command 抛 `SandboxGap("mcp.sandbox.command.missing")`；`policy is None` → 原 argv + `sandbox.policy.missing`；`mode=off`/`bypass` → 原 argv（显式关闭，不算降级）；backend 缺失/不可用 → enforce 抛 `SandboxGap`，其它模式回退并返回 reason；可用时委托 `spawn_argv(shlex.join(command), ...)`。
  - `_factory_for`：`sandbox=False`、或注入了测试 factory、或 remote server → **原样返回**，零行为变化；只有 local + sandbox=True + 默认 factory 才走 `_stdio_parameters`。
  - `_stdio_parameters`：调用 `_sandboxed_command`，fallback 时按 server 只报一次 `report_degraded("mcp_sandbox", reason, "the MCP server runs without the OS process sandbox")`；`SandboxGap` 直接向上抛，由 owner 记录为 error（不 spawn）。
  - `_server_parameters(server, *, argv=None)`、`_default_session_factory(server, *, parameters=None)`。
- `ai_coding/server/world.py`：`MCPManager(..., sandbox=(sandbox_policy, sandbox_backend), sandbox_roots=sandbox.live_roots)`。

**为什么**：`spawn_argv` 已经是 bash/job 唯一的隔离入口；本地 MCP 是唯一绕过它的子进程。默认关闭保证既有配置零变化；enforce 下必须 fail-closed（裁决 #2），不能“捕获后静默直连”。

**测试证据**（`tests/test_mcp.py`）
- `test_sandbox_is_off_by_default`：`MCPConfig().sandbox is False`，`MCPServerConfig` 未新增字段。
- `test_sandbox_flag_off_keeps_the_exact_original_argv`：factory 恒等返回默认 factory，argv 逐字不变。
- `test_sandboxed_stdio_command_goes_through_the_backend`：逐元素断言 `["--", SHELL_ARGV0, "-c", "<shlex join>"]`（裁决 #9）。
- `test_a_missing_sandbox_backend_falls_back_and_reports_degraded`：warn + 不可用 → 原 argv + 恰好一条 `mcp_sandbox` Degraded，重复调用不重复报。
- `test_enforce_refuses_a_server_it_cannot_wrap`：enforce + 无 backend → `_server_parameters` 从未被调用（未 spawn）、`available()==[]`、行 detail 含 `sandbox.backend.missing`。
- `test_an_empty_sandboxed_command_is_refused`：空 command 拒绝（裁决 #9）。

**与 01 方案的偏离**：无功能偏离。`_sandboxed_command` 用 `shlex.join`（01 已写），args 以列表元素断言。

---

### MCP-02 close 取消后不等待

**改了什么**：`_MCPSessionOwner.close` 在 `task.cancel()` 后追加第二次 `asyncio.wait({task}, timeout=_CLOSE_CANCEL_GRACE)`（1.0s），仍不完成则 warning；用 `asyncio.wait` 而非 `await task`，不把任务自身的 `CancelledError` 抛给调用方。

**为什么**：取消只是信号；`AsyncExitStack` 只有在任务再次被调度时才展开，旧实现让子进程/stdio fd/httpx client 泄漏到进程退出。

**测试证据**（`tests/test_mcp_lifecycle.py`）
- `test_close_awaits_a_cancelled_owner_task`：清理里 `await sleep(0.2)`，`_CLOSE_TIMEOUT=0.01`；close 返回后 `owner._task.done()` 且清理完成（红→绿）。
- `test_close_is_bounded_even_if_a_session_ignores_cancellation`：忽略取消的 session 下 close 仍在 grace 上限内返回（1s 上限）。
- `test_close_is_idempotent_after_a_forced_cancel`：**回归护栏**（修复前即通过），注释已写明。

---

### MCP-03 调用超时后复用会话

**改了什么**
- `invoke_remote_tool(..., on_session_stale=None)`；`DeadlineExpired` 分支先调用它再返回 `mcp_timeout`。
- `MCPManager._retire(name, reason)`：`mark_dead + wake + publish`，`note_transport_death` 与新增的 `note_session_stale` 共用，第二次死亡不覆盖原因。
- `tools/mcp_call.py`：仅当 `handle.execution_timeout is not None and > 0` 时才传 `on_session_stale`（裁决 #5）。

**为什么**：`deadline` 会 abandon 而非等待被取消的 `call_tool`，其响应可能投递给“看起来健康”的同一会话。超时意味着会话不可信。

**测试证据**（`tests/test_mcp.py`）
- `test_an_execution_timeout_retires_the_session_and_reconnects`：超时后 `available()==[]`、行 detail 含“重连”，随后 supervisor 重连成功（attempts==2）。
- `test_a_zero_execution_budget_does_not_retire_a_healthy_session`：**回归护栏**（修复前即通过），固定 `execution > 0` 边界。
- `tests/test_mcp_lifecycle.py::test_a_transport_failure_marks_the_server_and_wakes_a_retry` 继续通过：两条退役路径共用。

**与 01 方案的偏离**：无。共享路径用 `_retire` 私有方法实现（01 写的是同构两份，结果一致且更少重复）。

---

### MCP-04 远端文本 output 无界

**改了什么**：`_MAX_RESULT_CHARS = 50_000`；`_join_text` 末尾调用 `infra/text_clip.py::clip_head_tail(text, _MAX_RESULT_CHARS)`（marker 自带 `[truncated: 省略 N 字符]`）。`structured` 未改动（裁决 #1：`turn_close._bounded_data` 已限制）。

**为什么**：这是唯一没有上限的通道；自造 head/tail 会形成第二套截断实现。

**测试证据**（`tests/test_mcp.py`）
- `test_a_huge_remote_output_is_head_tail_truncated`：300k 文本 → `len <= 50_100`，首 A 尾 B 保留，含 `[truncated`。
- `test_a_small_result_is_byte_identical`：**回归护栏**，小结果逐字不变。

**与 01 方案的偏离**：按裁决删除了 01 中自造的 `_head_tail` / `_bounded_structured` / 200k 常量。

---

### MCP-05 list_tools 只拉一页

**改了什么**
- `_LIST_TOOLS_MAX_PAGES = 20`；owner 新增 `tools_pages`、`tools_incomplete`、`ready_at`。
- `_serve` 抽出 `_list_tools()`：首调用不带参（兼容旧 fake/SDK），之后带 `cursor`；按名称去重；`nextCursor` 为空即完成；`next_cursor == cursor` 或命中已见 cursor → 视为异常、停止并置 `tools_incomplete=True`；页数用尽也置 True。
- `build_mcp_catalog(..., *, incomplete: Iterable[tuple[str, int]] = ())`：把页数写进声明 `(catalog incomplete: fs (20 pages); not every tool is listed)`，并在预算记账开始时预留该行长度。
- `MCPManager.incomplete_servers() -> tuple[tuple[str, int], ...]`；`servers()` 的 connected/filtered 行追加“工具列表仅读取 N 页，可能不完整”。
- `world.py` 的 `catalog_builder` 传入 `incomplete=...`。

**为什么**：分页协议被当成单次 RPC；且“截断”必须可声明（裁决 #6：读 `tools_pages` 写进声明；重复 cursor 必须标不完整）。

**测试证据**
- `tests/test_mcp.py::test_list_tools_follows_next_cursor_across_pages`（3 个工具、2 页、`tools_pages==2`、`incomplete is False`）。
- `tests/test_mcp.py::test_tool_listing_stops_at_twenty_pages_and_declares_it`（20 个工具、`tools_pages==20`、`incomplete_servers()==(("fs",20),)`、行与 catalog 都声明）。
- `tests/test_mcp.py::test_a_repeated_cursor_cannot_loop_forever`（只调用 2 次、`tools_incomplete is True`、行含“不完整”）。
- `tests/test_mcp_catalog.py::test_the_catalog_declares_an_incomplete_tool_listing`、`test_the_incomplete_note_is_counted_in_the_budget`（任意预算 `len(text) <= budget`）。

**与 01 方案的偏离**：`incomplete` 的载荷由 `Iterable[str]` 改为 `Iterable[tuple[str, int]]`，以满足“读 `tools_pages` 写入声明”的裁决；01 文档里的 `("fs",)` 形式不再使用。

---

### MCP-06 remote 无法手动重连；文案错报

**改了什么**
- `request_connect`：`not server.command` → `not _is_connectable_shape(server)`，并加 `self._stop.is_set()` 守卫。
- `unreachable_reason`：owner 为 None 时按 type 报缺 `url` / `command`。
- `_connecting_detail`：remote 追加“若需登录，请在 MCP 面板选择登录，并检查网络与授权服务器”，不再给“本地 server 握手”建议。

**测试证据**（`tests/test_mcp_lifecycle.py`）
- `test_a_remote_server_can_be_reconnected_manually`
- `test_a_remote_server_without_a_url_says_url_not_command`（含 `no url`，不含 `command`）
- `test_a_remote_connecting_row_advises_login_not_a_local_handshake`

**与 01 方案的偏离**：无。

---

### MCP-07 错误码单一 `mcp_error`

**改了什么**
- `domain/error_codes.py`：只新增 `mcp_timeout→DEADLINE_EXCEEDED`、`mcp_transport→UNAVAILABLE`、`mcp_tool_error→UNAVAILABLE`，保留 `mcp_error→UNAVAILABLE`；**未使用 NOT_FOUND**。`_RETRYABLE` 只加 `mcp_timeout`；`is_retryable` 同时认 leaf 与 family（向后兼容）。
- `invoke_remote_tool`：超时→`mcp_timeout`；异常按 `_is_transport_death` → `mcp_transport` / `mcp_tool_error`；远端 `is_error=True` → `mcp_tool_error`。
- `MCPCallTool._error(message, server, tool, code="mcp_tool_error")`：未知 server / 未知 tool → `mcp_tool_error`；已配置但不可达/需登录 → `mcp_transport`。

**为什么**：裁决 #3 明确不用 NOT_FOUND（否则未知名字从“可重试”变成“不可重试”），只允许 3 个新 leaf。

**测试证据**
- `tests/test_mcp.py::test_the_mcp_leaves_map_to_the_families_the_retry_policy_uses`、`test_a_transport_failure_has_its_own_detail`、既有超时用例（`detail == "mcp_timeout"`）、`test_execute_marks_is_error`（`mcp_tool_error`）。
- `tests/test_mcp_call_tool.py`：未知 server / 未知 tool → `mcp_tool_error`；配置了但连不上 → `mcp_transport`；远端 is_error → `mcp_tool_error`。
- 原有 5 处 `detail == "mcp_error"` 断言已同步为更具体的 leaf（不是放宽）。

**与 01 方案的偏离**：01 计划新增 6 个 leaf（含 `mcp_unknown_server/tool`、`mcp_unavailable`）并按 NOT_FOUND 分类；按裁决只保留 3 个。未知名字归入 `mcp_tool_error`（家族仍 UNAVAILABLE，与修复前行为一致），不可达归入 `mcp_transport`。

---

### MCP-08 重连风暴：delay 立即重置

**改了什么**
- `_STABLE_SESSION_S = 60.0`；`_ready_at` 在 `_serve` 置 ready 前记录；`_session_was_stable(owner)`。
- `_supervise` 的 ok 分支：稳定才 `delay = _BACKOFF_MIN`，否则翻倍；失败/唤醒分支删除两处 `delay = _BACKOFF_MIN`（`_pause` 被唤醒不再重置节奏）。
- **补充**：ok 分支在计算 delay 后真正 `await _pause(name, delay)`（manual 唤醒除外），否则“连上即掉”的循环根本没有等待，退避值无处生效。`request_connect` 记录 `_manual`，让手动重连仍然立即尝试。

**为什么**：旧实现的 ok 分支没有 pause，稳定/不稳定都只影响“下一次失败后的等待”，实际形成 1s 级风暴；裁决要求“稳定 ≥60s 才重置”，必须同时让该 delay 真正被等待。

**测试证据**（`tests/test_mcp_lifecycle.py`）
- `test_an_unstable_session_does_not_reset_the_backoff`：每次连上即 note death，间隔 0.04 < 0.08 < 0.16 递增。
- `test_a_session_stable_for_a_minute_resets_the_backoff`：`ready_at` 120s 前 → True，5s 前/None → False。
- `test_a_stable_session_gets_the_minimum_pause_again`：`_STABLE_SESSION_S=0` 时每轮回到最小 pause。
- `test_a_manual_connect_tries_now_without_waiting_out_the_pause`（原 “resets the pause” 已改名，裁决 #8）：手动唤醒仍 <1s 发起。
- 既有 `test_the_pause_grows_and_stays_under_the_cap`、`test_a_failing_server_retries_until_it_comes_back` 继续通过。

**与 01 方案的偏离**：新增“ok 分支真正 pause”与 `_manual` 标记。理由：01 的测试假设 ok 分支会按 delay 等待，但原代码并不等待；不加则“风暴”未被修复、用例也无法成立。手动重连语义保持“立即尝试”。

---

### MCP-09 OAuth 回调端口占用即抛

**改了什么**
- `_CallbackServer.start`：`OSError` 且配置端口非 0 → 用 0 再 bind，记录 `fell_back=True`，实际端口写回 `self.port`（`port == 0` 时二次失败照常抛）。
- 裁决 #7：新增 `_TokenStore.clear_client_info()`；`_remote_session_factory` 在 `callback.fell_back` 时清空缓存的 client_info（保留 tokens），避免动态注册绑定旧 redirect URI。

**测试证据**（`tests/test_mcp_oauth.py`）
- `test_a_busy_callback_port_falls_back_to_an_os_port`：先占端口 → start 成功、端口不同、能收 GET 并拿到 code/state。
- `test_port_zero_still_reports_the_actual_port`（**回归护栏**）。
- `test_the_callback_stops_cleanly_after_a_fallback`：stop 后端口可再次 bind。
- `test_a_fallback_forgets_the_cached_client_registration`：client_info 清空、tokens 保留。

**与 01 方案的偏离**：按裁决 #7 增加了 client_info 清理（01 未写）。

---

### MCP-11 test_mcp_progress 过时字段

**改了什么**：`tests/test_mcp_progress.py::_config` 删除 `connection_timeout` / `enabled`，改用 `MCPTimeout(startup=30_000)`；新增非 gated 的 `tests/test_mcp_config_fields.py`。

**为什么**：`extra="ignore"` 让漂移静默，fixture 实际跑在 60s 默认值上。

**测试证据**：`test_the_progress_fixture_uses_current_config_fields`（`startup==30_000`、`disabled is False`、dump 中无 `connection_timeout`）；`AI_CODING_MCP_STDIO=1` 下真实 stdio 进度用例 1 passed。

**与 01 方案的偏离**：无。

---

## 备注 / 需评审重点看

1. **MCP-08 的补充 pause + `_manual`**（`mcp.py` `_supervise` ok 分支、`request_connect`）：这是相对 01 的行为增量，也是“退避不再被重置”真正生效的前提。
2. **MCP-07 的归类**（`tools/mcp_call.py` 与 `error_codes.py`）：未知 server/tool 用 `mcp_tool_error`（UNAVAILABLE），不可达用 `mcp_transport`；没有 NOT_FOUND。
3. **MCP-05 的 `incomplete` 形状**（`build_mcp_catalog` 签名 + `incomplete_servers()`）：由 server 名改为 `(name, pages)`，预算预留包含该行。

未完成项：无功能项。
**BLOCKED（仅 mypy 全仓门禁）**：`mypy ai_coding` 仍有 1 个错误，位于白名单外、由并行 SUB 批次引入的 `ai_coding/core/scheduler.py:697`（`lambda _task, _key=key: ...`）。按白名单约束 B1 不修改该文件；B1 的 `mcp.py` / `mcp_call.py` / `error_codes.py` / `models.py` 四文件 mypy 通过，`world.py` 单独检查时因导入 `scheduler` 会带出同一错误（非 B1 代码）。
