# B1 MCP 调研方案

> 范围：MCP-01 ~ MCP-11。基线 commit `fd5a929`，只读调研，本文件之外不改任何代码。
> 证据均来自当前工作树（非推测）；锚点行号以本文件写作时读取到的内容为准。
> 目标：越界执行可开关、生命周期确定性关闭、结果有界、错误可分辨。

---

## 1. 逐项分析

### MCP-01 stdio server 直接 spawn，绕过进程沙箱

**现状（file:line + 关键片段）**

- `ai_coding/core/mcp.py:869-879`：`_server_parameters` 把 `server.command` 拆成 `StdioServerParameters(command=command[0], args=command[1:], env=..., cwd=...)`，没有任何 argv 包装。
- `ai_coding/core/mcp.py:882-908`：`_default_session_factory` 直接 `stdio_client(_server_parameters(server), ...)`；`stdio_client` 内部由 anyio `open_process`，没有经过 `security/process_sandbox.py`。
- `ai_coding/server/world.py:214-218` 已构建 `sandbox_policy, sandbox_backend`；`world.py:269` 才 `MCPManager(app_cfg.mcp, observer=recorder)`，两者毫无连接。
- `ai_coding/security/process_sandbox.py:401-409` 的 `spawn_argv(command, policy, backend, roots, *, emit=None, network=None)` 是 bash/job 共用的唯一包装入口；`process_sandbox.py:1028-1090` 的 `build_process_sandbox` 产出 `(ProcessSandboxPolicy, SandboxBackend)`。
- `ProcessSandboxPolicy` 的 docstring 甚至已经点名 “`off` -- do not isolate (an MCP child, an explicit bypass)”（`process_sandbox.py:237`），但 mcp 从未接上。

**根因**

MCP 的 local server 走 `mcp` SDK 的 `stdio_client`，而沙箱是“把 argv 改写成 `bwrap ...`”的纯 argv 变换；两条路径从未交汇。`MCPManager` 也拿不到 composition root 已经建好的 policy/backend。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 保持直接 spawn | 零风险、零改动 | 本地 MCP server 仍可越界写/读/联网；与 `process_sandbox.mode=enforce` 的承诺矛盾 |
| B 全局开关 + argv 包装（推荐） | `MCPConfig.sandbox: bool = False`；开启时把 `command` 交给 `spawn_argv`，再拆回 `StdioServerParameters` | 复用已有唯一包装入口；默认关闭，行为零变化；backend 不可用可回退 | 需要把 policy/backend/roots 注入 manager；网络模式沿用 `process_sandbox.network`，可能影响 npx |
| C 每个 server 单独 `sandbox` 字段 | `MCPServerConfig.sandbox: bool` | 粒度更细 | 配置面变大；与“批次统一开关”的验收口径不一致 |

**推荐方案**

1. `ai_coding/config/models.py`（MCP 段）：`MCPConfig` 增 `sandbox: bool = False`（位于 `enabled` 之后）。

2. `ai_coding/core/mcp.py`：`MCPManager.__init__` 增两个关键字参数（保持只增不改）：

```python
sandbox: tuple[Any, Any] | None = None,   # (policy, backend)
sandbox_roots: Any = (),                  # 可调用对象或 Path 序列
```

并保存 `self._sandbox_policy, self._sandbox_backend = sandbox or (None, None)` 与 `self._sandbox_degraded: set[str] = set()`。

3. 新增纯函数：

```python
def _sandboxed_command(
    command: Sequence[str], policy: Any, backend: Any, roots: Sequence[Path],
    *, emit: Callable[[str], None] | None = None,
) -> tuple[list[str], str | None]:
    """返回 (可直接交给 StdioServerParameters 的 argv, 回退原因或 None)。"""
```

逻辑：

- `command` 为空 → `([], "mcp.sandbox.command.missing")`；
- `policy is None` 或 `getattr(policy, "mode", "off") == "off"` 或 `policy.bypass` → 原样返回 `list(command), None`；
- `backend is None` → 原样返回 + `"sandbox.backend.missing"`；
- `not backend.available()` → 原样返回 + `backend.gap()`；
- 否则 `spawn_argv(shlex.join(command), policy, backend, roots, emit=emit)`，`SandboxGap` 时原样返回 + `exc.reason`。

4. `_server_parameters(server, *, argv: Sequence[str] | None = None)`；`_default_session_factory` 增 `*, parameters: Any | None = None`，有则直接用。

5. `MCPManager._factory_for` 的 local 分支：

```python
if server.type == "remote":
    return lambda _s: _remote_session_factory(...)
if not self._config.sandbox or self._session_factory is not _default_session_factory:
    return self._session_factory
return lambda s: _default_session_factory(s, parameters=self._stdio_parameters(s))
```

`_stdio_parameters(server)` 解析 `roots = self._sandbox_roots() if callable(...) else tuple(...)`，调用 `_sandboxed_command`；失败时按 server 只报一次：

```python
report_degraded(self._observer, "mcp_sandbox", reason,
                "the MCP server runs without the OS process sandbox")
```

`sandbox=True` 但 policy 为 None 时同样直连并报 `"sandbox.policy.missing"`。

6. `ai_coding/server/world.py:269`：

```python
mcp_manager = MCPManager(app_cfg.mcp, observer=recorder,
    sandbox=(sandbox_policy, sandbox_backend), sandbox_roots=sandbox.live_roots)
```

> 与既定方向的差异：方向只写 `(policy, backend)`，但 `spawn_argv` 必须拿到可写面 `roots`，否则 `wrap()` 无法 bind workspace。因此签名额外增加 `sandbox_roots`（不推翻开关方案，只是补齐必要输入）。

**测试计划**

`tests/test_mcp.py`（不 spawn 真进程）：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_sandbox_is_off_by_default` | `MCPConfig().sandbox is False`；`MCPServerConfig` 不受影响 | `MCPConfig` 无 `sandbox` 属性 → AttributeError |
| `test_sandboxed_stdio_command_goes_through_the_backend` | 用 fake `backend.wrap` 返回 `["bwrap", "--", *argv]`，`manager._stdio_parameters(server).command == "bwrap"` 且 args 含 `/bin/bash -c` | 无 `_stdio_parameters`/`sandbox` 参数 |
| `test_a_missing_sandbox_backend_falls_back_and_reports_degraded` | backend.available()=False 时 command 仍是原 argv；`RecordingObserver.of_type(Degraded)` 有且仅有一条 `component == "mcp_sandbox"` | 无 Degraded，且无该入口 |
| `test_sandbox_flag_off_keeps_the_exact_original_argv` | command/args 与 `_server_parameters` 逐字相同 | 同上 |

**风险与回退**

- bwrap 默认 `process_sandbox.network.mode="deny"`（`config/models.py:201`），会切断本地 MCP 的 npx/npm 下载与远端拉取。开启 `mcp.sandbox` 的用户必须同时配置 `network.mode: inherit` 或 `proxy`。本批次**不**改默认网络模式。
- 用 `shlex.join` 后经过 `bash -c`，多一层 shell；参数含 NUL 等非法字符时 `shlex.join` 可能抛错 → 捕获后回退原 argv 并报 Degraded。
- 回退：删除 `sandbox` 字段并把 `world.py` 的两个实参去掉即可回到当前行为；flag 默认 False 时连代码路径都不会进入。

---

### MCP-02 close 超时后 cancel 但不 await

**现状**

- `ai_coding/core/mcp.py:503-512`：

```python
async def close(self) -> None:
    self._stop.set()
    task = self._task
    if task is None or task.done():
        return
    budget = _CLOSE_TIMEOUT if self.ready.is_set() else _CLOSE_TIMEOUT_NEVER_CONNECTED
    done, _pending = await asyncio.wait({task}, timeout=budget)
    if not done:
        task.cancel()
```

`task.cancel()` 之后立即返回。anyio 的 cancel scope / `AsyncExitStack` 退出需要任务真正跑到取消点；不 await 会让子进程、stdio fd、`httpx.AsyncClient` 的关闭被推迟到事件循环下一次调度，进程退出时甚至完全不执行。`_MCPSessionOwner._serve` 的 `async with AsyncExitStack()`（`mcp.py:452-462`）正是需要被 await 的清理。

**根因**

把 `cancel()` 当成“已关闭”。`asyncio.wait` 超时分支只发信号，不收敛任务。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 保持 cancel 后返回 | 关闭路径永不阻塞 | 句柄泄漏、子进程残留、测试/trace 出现 “Task was destroyed” |
| B cancel 后二次 `asyncio.wait`（推荐） | `task.cancel()` + `await asyncio.wait({task}, timeout=_CLOSE_CANCEL_GRACE)` | 不传播任务的 CancelledError；有硬上限；实现与现有风格一致 | 最坏多等 1s |
| C `task.cancel(); await task` 捕 CancelledError | 语义最直接 | 调用方被取消时可能被误吞 | 需要区分“自己取消”和“外部取消”，易写错 |

**推荐方案**

```python
_CLOSE_CANCEL_GRACE = 1.0

async def close(self) -> None:
    self._stop.set()
    task = self._task
    if task is None or task.done():
        return
    budget = _CLOSE_TIMEOUT if self.ready.is_set() else _CLOSE_TIMEOUT_NEVER_CONNECTED
    done, _pending = await asyncio.wait({task}, timeout=budget)
    if done:
        return
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=_CLOSE_CANCEL_GRACE)
    if not done:
        _log.warning("MCP session %r ignored cancellation", self.server.name)
```

用 `asyncio.wait` 而不是 `await task`：不会把任务的 `CancelledError` 抛给调用方，避免把外部取消与内部取消混为一谈。

**测试计划**

`tests/test_mcp_lifecycle.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_close_awaits_a_cancelled_owner_task` | session 的 `__aexit__` 在收到取消后 `await asyncio.sleep(0.2)`；monkeypatch `_CLOSE_TIMEOUT=0.05`、`_CLOSE_CANCEL_GRACE=1.0`；`await owner.close()` 返回后 `owner._task.done() is True` | 当前 close 在 0.05s 就返回，task 仍 pending → 断言失败 |
| `test_close_is_idempotent_after_forced_cancel` | 连续两次 close 不抛异常，第二次立即返回 | 二次 wait 可能对已 done 任务正常，新增保护 |

**风险与回退**

- `_CLOSE_CANCEL_GRACE` 过大拖慢 `aclose`（多 server 串行关闭）。1s 是硬上限；`MCPManager.aclose` 仍是逐个 owner 顺序关闭，若实测慢可在后续改成 `gather`（本批次不做）。
- 回退：删除二次 wait 即恢复现状。

---

### MCP-03 调用超时后会话仍复用

**现状**

- `ai_coding/core/mcp.py:962-980`：`except DeadlineExpired` 只返回 `ToolResult(error="mcp_error")`，不碰 owner/session。
- `ai_coding/core/mcp.py:1503-1506`：`available()` 仍为同一 `owner.session` 生成 handle，只要 `owner.error is None and owner.dead is None and owner.ready`。
- `infra/deadline.py:112-124,178-180`：超时会 `cancel()` 并 **abandon** 内部 task，不等它结束。被放弃的 `call_tool` 仍可能在后台完成并把响应投递回 `ClientSession`；下一次调用复用一个“有过未收敛请求”的会话，轻则响应错配，重则传输层已半死却仍被当作可用。

**根因**

超时只被当成“这一次调用失败”，没有把它当成“这个 session 不再可信”。`DeadlineExpired` 分支不碰 `owner.dead`，`available()` 只看 `error/dead/ready`。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 复用会话 | 无额外重连成本 | 超时后继续用不确定的会话；错误会以“随机下一次调用失败”出现 |
| B 标记会话失效并唤醒 supervisor（推荐） | 超时回调 → `owner.mark_dead("调用超时，正在重连")` + `_wake_event.set()`；`available()` 立即剔除 | 与 `note_transport_death` 同一条路径；catalog 当步即消失；重连由 supervisor 统一负责 | 一次超时触发一次重连 |
| C 下次调用前原地重建 | 在 `MCPCallTool.execute` 里检测超时并 `await manager.reconnect(name)` | 重连时机可控 | 工具层持有连接生命周期，违反“一个 owner 只由一个 supervisor 管”的设计 |
| D 每次调用新开 session | 无复用 | 最干净 | 握手成本极高，破坏 progress/流式体验 |

**推荐方案**

1. `invoke_remote_tool` 末位增 `on_session_stale: Callable[[], None] | None = None`；`except DeadlineExpired` 分支先调它再返回。

2. `MCPManager` 新增（与 `note_transport_death` 同构）：

```python
def note_session_stale(self, name: str, reason: str = "调用超时，正在重连") -> None:
    owner = self._owner(name)
    if owner is None or owner.dead is not None:
        return
    owner.mark_dead(reason)
    self._wake_event(name).set()
    self._publish(name)
```

3. `ai_coding/tools/mcp_call.py:123-136` 传 `on_session_stale=lambda: self._manager.note_session_stale(server)`；`MCPToolAdapter.execute` 不传（它没有 manager）。

**测试计划**

`tests/test_mcp.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_an_execution_timeout_retires_the_session` | `call_delay=30`、`execution_timeout=0.05`；`MCPCallTool.execute` 返回失败后：`manager.available() == []`，`manager.servers()[0]["detail"]` 含“重连”；随后 supervisor 重新连接后 `available()` 恢复 | 当前超时后 `available()` 仍有 handle，detail 为 connected |
| `test_a_transport_death_and_a_timeout_share_one_retire_path` | 两种原因都只置一次 `dead`，`dead` 不会被第二次覆盖 | 当前无超时路径 |

**风险与回退**

- 若用户把 `timeout.execution` 设得很小，正常慢工具会反复重连；`MCP-08` 的“稳定 60s 才重置 delay”会同时限制风暴。
- 超时重连会丢失该 server 的进行中 progress；这是“会话不可信”的必然代价。
- 回退：去掉 `on_session_stale` 传参即可恢复。

---

### MCP-04 远端文本 output 无裁剪

**现状**

- `ai_coding/core/mcp.py:911-918`：`_join_text` 把 `content` 里的文本块 `"\n".join(parts)`，没有任何长度上限。
- `ai_coding/core/mcp.py:993-1010`：文本直接进入 `ToolResult.output`；`structured_content` 原样放进 `data["structured"]`。
- 远端一个 `read_file`/`search` 返回几 MB 文本时，这一整块会进入下一轮请求，直接挤爆上下文预算；`structured` 同理。

**根因**

MCP 边界没有“工具结果大小上限”概念，只有 catalog 预算和 execution 超时。`turn_close._bounded_data` 只约束 `<data>` 的收尾展示，约束不到 `ToolResult.output`。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 原样返回 | 不丢信息 | 单次远端结果可打爆上下文 |
| B 只截断文本 | `_join_text` 加 head+tail | 改动最小 | `structured` 仍无界 |
| C 文本 + structured 都截断（推荐） | 常量 `_RESULT_MAX_CHARS=200_000`；文本 head+tail + 标记；structured 超限换成说明 | 两条通道都有界；标记可读 | 截断后模型可能看不到中段 |
| D 拒绝超限结果 | 返回 `RESOURCE_EXHAUSTED` | 不产生半截事实 | 模型拿不到任何内容，体验差 |

**推荐方案**

```python
_RESULT_MAX_CHARS = 200_000       # 200k ≈ 50k tokens at 4 chars/token
_TRUNCATION_MARK = "\n…[truncated {omitted} chars]…\n"

def _head_tail(text: str, limit: int = _RESULT_MAX_CHARS) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head, tail = limit // 2, limit - limit // 2
    return text[:head] + _TRUNCATION_MARK.format(omitted=len(text) - limit) + text[-tail:]
```

`_join_text(content, max_chars=_RESULT_MAX_CHARS)` 在 join 之后调用 `_head_tail`。

structured（`mcp.py:1004-1009`）用同构的 `_bounded_structured(value, limit)`：`json.dumps` 超过 limit 时返回 `{"truncated": True, "chars": N, "note": "structured data exceeded N chars"}`；不超过或无法序列化则原样返回。与 `core/turn_close.py:150-167` 的 `_bounded_data` 同构，不直接 import 私有函数以避免 core 内耦合。

**测试计划**

`tests/test_mcp.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_a_huge_remote_output_is_head_tail_truncated` | 文本 `"A"*150_000 + "B"*150_000`；`len(result.output) <= _RESULT_MAX_CHARS + 100`；开头 `A`、结尾 `B` 都在；含 `[truncated` | 当前 output 长 300k，断言失败 |
| `test_a_huge_structured_payload_is_replaced_with_a_marker` | `structured={"blob": "x"*300_000}` → `result.data["structured"]["truncated"] is True` 且 `chars` 存在 | 当前原样返回 |
| `test_a_small_result_is_byte_identical` | 小文本 / 小 structured 完全不变 | 通过（防回归） |

**风险与回退**

- 200k 对 128k 窗口模型仍偏大，但这是上限而非目标值；真正的窗口约束由上下文预算负责。把常量调到更小即可。注意 `_join_text` 是先 join 后截断，峰值内存仍是 O(全部文本)；要消掉这一点需按块累加并在达到上限时停止，本批不做。
- head+tail 会让模型读到不连续内容；标记必须显式，避免它误以为文件只有这些。
- 回退：`limit=0` 即关闭。

---

### MCP-05 list_tools 只拉一页

**现状**

- `ai_coding/core/mcp.py:459-460`：`listed = await self.session.list_tools()` 后 `self.tools = list(listed.tools or [])`，只取第一页。
- MCP `tools/list` 是分页协议：`ListToolsResult.nextCursor` 非空时必须带 `cursor` 再请求。第 2 页起的工具直接不存在——catalog 里看不到，模型也无法调用。
- `ai_coding/core/mcp.py:321-399` 的 `build_mcp_catalog` 只接收 `(name, handles)`，没有“列表不完整”的输入，所以即使截断也无法声明。

**根因**

`_serve` 把 `list_tools` 当成一次性 RPC；catalog 的输入模型没有“完整性”字段。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 只取第一页 | 零成本 | 大 server 的工具永久不可见 |
| B 循环 + 20 页硬上限 + 不完整声明（推荐） | 按 `nextCursor` 拉取，环形/重复 cursor 停止；超页数置 `tools_incomplete`，catalog 与 panel 都声明 | 覆盖绝大多数 server；有硬上限；失败可见 | 20 页后仍截断 |
| C 无上限循环 | 直到 cursor 为空 | 完整 | 恶意/故障 server 可无限翻页，绕过 catalog 预算 |
| D 只提示用户加 `tools` 白名单 | 不改代码 | 无 | 白名单也选不到未列出的工具 |

**推荐方案**

1. 常量 `_LIST_TOOLS_MAX_PAGES = 20`；`_MCPSessionOwner` 增 `tools_incomplete: bool = False`、`tools_pages: int = 0`。

2. `_serve` 改为按 cursor 循环：

```python
cursor, pages, tools, seen = None, 0, [], set()
while pages < _LIST_TOOLS_MAX_PAGES:
    listed = (await self.session.list_tools() if cursor is None
              else await self.session.list_tools(cursor=cursor))
    pages += 1
    for tool in getattr(listed, "tools", None) or []:
        name = str(getattr(tool, "name", ""))
        if name and name not in seen:
            seen.add(name); tools.append(tool)
    next_cursor = getattr(listed, "nextCursor", None)
    if not next_cursor or next_cursor == cursor:
        cursor = None; break
    cursor = next_cursor
self.tools, self.tools_pages = tools, pages
self.tools_incomplete = bool(cursor)     # 耗尽页数才是 True
self.ready.set()
```

（首调用保持无参，现有 fake `list_tools(self)` 不受影响；只有第二页起的 fake 才接收 `cursor`。）

3. `build_mcp_catalog` 增加只增关键字参数 `incomplete: Iterable[str] = ()`。预算记账：在计算 `used` 时先加上 `len(note) + 1`（仅当 `incomplete` 非空），`lines` 非空时再 append `(catalog incomplete: fs; only the first 20 pages were read)`，保证 `len(text) <= budget_chars`。

4. `MCPManager.incomplete_servers() -> tuple[str, ...]`；`available()` 不变。`world.py:295` 改为 `build_mcp_catalog(mcp_manager.available(), incomplete=mcp_manager.incomplete_servers())`。

5. `servers()` 的 connected 行：`owner.tools_incomplete` 时 `detail` 追加“工具列表仅读取前 20 页，可能不完整”。

**测试计划**

`tests/test_mcp.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_list_tools_follows_next_cursor_across_pages` | fake session 第一页 2 个工具 + `nextCursor="p2"`，第二页 1 个 + `nextCursor=None`；`available()` 含全部 3 个 | 当前只第一页的 2 个 |
| `test_tool_listing_stops_at_twenty_pages` | 25 页、每页 1 个且 cursor 一直非空；`len(manager.available()[0][1]) == 20` 且 `owner.tools_incomplete is True` | 当前只有 1 个 |
| `test_a_repeated_cursor_cannot_loop_forever` | 第二页返回相同 cursor；调用次数 ≤ 2，不挂起 | 当前只 1 次调用，不进入循环，但该用例在实现循环后才有意义 |

`tests/test_mcp_catalog.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_the_catalog_declares_an_incomplete_tool_listing` | `build_mcp_catalog([...], incomplete=("fs",))` 文本含 `catalog incomplete` 与 `fs`；`len(text) <= budget` | 当前无 `incomplete` 参数 → TypeError |
| `test_the_incomplete_note_is_counted_in_the_budget` | 小预算循环，任何输入 `len(text) <= budget` | 新路径 |

**风险与回退**

- 某些 server 可能把 cursor 当状态机，重复请求不稳定；`seen` 集合只记录工具名，不记录 cursor；用 `next_cursor == cursor` 防死循环即可。
- 20 页可能仍不够；声明“不完整”比静默截断诚实。
- 回退：把循环改回单次调用，`incomplete` 默认空即恢复。

---

### MCP-06 remote 无法手动重连；失败文案错报 “no command”

**现状**

- `ai_coding/core/mcp.py:1446`（`request_connect`）：

```python
if server is None or server.disabled or not server.command:
    return False
```

remote server 的 `command` 永远为空（`config/models.py:758-764` 明确两类 server 字段互斥），所以面板的“重新连接”对 remote 永远返回 False。

- `ai_coding/core/mcp.py:1551-1556`（`unreachable_reason`）：

```python
if owner is None:
    if cfg.disabled:
        return f"MCP server '{server}' is disabled."
    if not cfg.command:
        return f"MCP server '{server}' has no command configured."
```

remote 缺 url 时被误报成“没有 command”。

- `ai_coding/core/mcp.py:1574-1592` 的 `_connecting_detail` 在超时后追加“本地 server 通常数秒内完成握手…”，对 remote 也是错误建议。

**根因**

`request_connect` / `unreachable_reason` 用了 local 专属字段 `command`，而不是已有的 `_is_connectable_shape`（`mcp.py:1719-1728`）。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 保持 | 无 | remote 失败后只能等自动重试；文案误导 |
| B 用 `_is_connectable_shape` + remote 文案（推荐） | 两处判定统一；`_connecting_detail` 按 type 分支 | 与 `adopt`/`open_all` 的判定一致；改动小 | 无 |
| C 给 `request_connect` 加 `force=True` | 面板显式强制 | “强制”语义清晰 | 仍需修文案；且 `force` 会绕过 disabled 检查，危险 |

**推荐方案**

```python
def request_connect(self, name: str) -> bool:
    if not self._config.enabled or self._stop.is_set():
        return False
    server = next((s for s in self._config.servers if s.name == name), None)
    if server is None or server.disabled or not _is_connectable_shape(server):
        return False
    self._wake_event(name).set()
    return True
```

`unreachable_reason` 的 `owner is None` 分支：

```python
if not _is_connectable_shape(cfg):
    missing = "url" if cfg.type == "remote" else "command"
    return f"MCP server '{server}' has no {missing} configured."
```

`_connecting_detail`：

```python
if server.type == "remote":
    detail += "；远程 server 若需登录，请在 MCP 面板选择登录"
else:
    detail += "；本地 server 通常数秒内完成握手，超时多半是命令或协议不兼容"
```

**测试计划**

`tests/test_mcp_lifecycle.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_a_remote_server_can_be_reconnected_manually` | `MCPServerConfig(name="r", type="remote", url="https://x")`；`manager.request_connect("r") is True` | 当前 `not server.command` → False |
| `test_a_remote_server_without_a_url_says_url_not_command` | `manager.unreachable_reason("r")` 含 `no url`，不含 `command` | 当前返回 `no command configured` |
| `test_a_remote_connecting_row_advises_login_not_local_handshake` | 先把 `_attempt_started["r"]` 置为 40s 前；`_connecting_detail(remote)` 含“登录”，不含“本地 server” | 当前含“本地 server” |

**风险与回退**

- remote 唤醒后若仍无 url，`_is_connectable_shape` 已挡住，不会进入 supervisor 空转。
- 回退：恢复 `not server.command` 判定。

---

### MCP-07 错误码单一 `mcp_error`

**现状**

- 生产者全部写 `mcp_error`：
  - 执行超时：`ai_coding/core/mcp.py:973-980`
  - 任意异常：`ai_coding/core/mcp.py:981-991`
  - 远端 `is_error=True`：`ai_coding/core/mcp.py:1001-1011`
  - 未知 server / 未知 tool：`ai_coding/tools/mcp_call.py:104-121,138-145`
- 映射表只有一条：`ai_coding/domain/error_codes.py:214` → `ToolError.UNAVAILABLE`；严重度 `UNAVAILABLE` 固定 5（`error_codes.py:264`）；可重试集合包含 `UNAVAILABLE`（`error_codes.py:286-293`）。

后果：超时（应 `DEADLINE_EXCEEDED`）、模型写错 server 名（应 `NOT_FOUND`）、传输死亡、远端工具自己报错，在 trace/重试策略/UI 上完全同质。

**根因**

历史 leaf 词表只有 `mcp_error`，且 MCP 内核把所有失败在一个 `except` 里压平。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 保持 | 无 | 重试/严重度/文案全错 |
| B 新增 6 个 leaf 并保留 `mcp_error`（推荐） | `mcp_timeout` / `mcp_transport` / `mcp_tool_error` / `mcp_unknown_server` / `mcp_unknown_tool` / `mcp_unavailable` | 向后兼容旧 trace；语义精确；`ToolResult.__post_init__` 自动归一化 | 需更新 5 处既有测试断言 |
| C 只细化超时 | 只加 `mcp_timeout` | 改动最小 | 未知 server/tool 仍不可分辨 |
| D 删除 `mcp_error` | 强制所有生产者改新码 | 词表干净 | 旧会话/旧 trace 变成 UNKNOWN |

**推荐方案**

`ai_coding/domain/error_codes.py` 的 `LEGACY_TO_FAMILY` 增加：

```python
# -- MCP (B1) ------------------------------------------------------------
"mcp_timeout": ToolError.DEADLINE_EXCEEDED,
"mcp_transport": ToolError.UNAVAILABLE,
"mcp_tool_error": ToolError.UNAVAILABLE,
"mcp_unknown_server": ToolError.NOT_FOUND,
"mcp_unknown_tool": ToolError.NOT_FOUND,
"mcp_unavailable": ToolError.UNAVAILABLE,
"mcp_error": ToolError.UNAVAILABLE,          # 保留：旧 trace / 第三方
```

生产点：

- `invoke_remote_tool` `DeadlineExpired` → `error="mcp_timeout"`
- 异常分支：`_is_transport_death(exc)` → `error="mcp_transport"`；否则 `error="mcp_tool_error"`
- `is_error=True` → `error="mcp_tool_error"`
- `MCPCallTool._error(message, server, tool, code)`：未知 server → `mcp_unknown_server`；未知 tool → `mcp_unknown_tool`；server 不连通/需登录 → `mcp_unavailable`（新增映射 `UNAVAILABLE`）

> 说明：`mcp_call.py` 的“server 不在 connected 列表”分支既可能是未知 server，也可能是连接失败；用 `unreachable_reason` 是否返回 None 区分：有配置但不可用 → `mcp_unavailable`，完全没配置 → `mcp_unknown_server`。

**测试计划**

`tests/test_mcp.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_a_deadline_is_deadline_exceeded_not_unavailable` | 超时结果 `result.error == "DEADLINE_EXCEEDED"`、`result.detail == "mcp_timeout"` | 当前 `UNAVAILABLE/mcp_error` |
| `test_a_transport_failure_has_its_own_detail` | `ClosedResourceError` → `result.error == "UNAVAILABLE"`、`detail == "mcp_transport"` | 当前 `mcp_error` |
| `test_a_remote_tool_error_is_not_a_connection_error` | `is_error=True` → `detail == "mcp_tool_error"` | 当前 `mcp_error` |

`tests/test_mcp_call_tool.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_an_unknown_server_is_not_found` | `detail == "mcp_unknown_server"`、`error == "NOT_FOUND"` | 当前 `mcp_error/UNAVAILABLE` |
| `test_an_unknown_tool_is_not_found` | `detail == "mcp_unknown_tool"`、`error == "NOT_FOUND"` | 当前 `mcp_error` |
| `test_a_configured_but_disconnected_server_is_unavailable` | `detail == "mcp_unavailable"`、`error == "UNAVAILABLE"` | 当前 `mcp_error` |

**风险与回退**

- 现有 5 处 `assert result.detail == "mcp_error"`（`test_mcp.py:271/283/484`、`test_mcp_call_tool.py:130/230`）需要同步改为新 leaf；这是“更具体”，不是放宽。
- `severity_of` 对 leaf 仍走关键词表（`error_codes.py:359-371`），`mcp_timeout` 含 “timeout” → 5；`mcp_unknown_server` 含 “not found” 不匹配 → 1。这是期望值。
- 回退：保留 `mcp_error` 映射，把生产点改回即可。

---

### MCP-08 重连风暴：delay 立即重置

**现状**

`ai_coding/core/mcp.py:1205-1260` 的 supervisor 有 3 个重置点：

```python
if ok:
    ...
    self._mark_dead(server.name, "连接已断开，正在重连")
    self._publish(server.name)
    delay = _BACKOFF_MIN                      # :1234
else:
    ...
    if _is_auth_failure(owner.error):
        await self._hold(server.name)
        ...
        delay = _BACKOFF_MIN                  # :1254
        continue
    woken = await self._pause(server.name, delay)
    if woken:
        delay = _BACKOFF_MIN                  # :1258
    else:
        delay = min(delay * 2, _BACKOFF_MAX)
```

只要 server “连上后立刻掉线”（`ok=True`，随后 `_hold` 立即被 wake），每次都会在 1234 重置为 1s；手动 connect / transport death 唤醒也会在 1258 重置。结果是稳定的 1s 重连风暴，`_MAX_CONCURRENT_CONNECTS=4` 只能限制并发，不能降低频率。

**根因**

把“曾经连上”当成“健康了很久”。重置条件里没有时间维度。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 每次成功/唤醒重置 | 恢复最快 | 崩溃循环时持续打满 CPU/进程数 |
| B 稳定 ≥60s 才重置（推荐） | `_STABLE_SESSION_S = 60.0`；按 `ready_at` 计算本次会话时长；不稳定则继续翻倍 | 保留“恢复快”；崩溃循环指数退避 | 一个 59s 后掉线的 server 会被当成不稳定 |
| C 只加 jitter | 在 delay 上加随机抖动 | 缓解同步 | 不解决每个 server 自身 1s 循环 |
| D 固定上限次数后停用 | N 次失败后不再重试 | 彻底防风暴 | 与“forever retry”设计承诺冲突 |

**推荐方案**

1. `_MCPSessionOwner` 增 `ready_at: float | None = None`；`_serve` 在 `self.ready.set()` 处记录 `time.monotonic()`。

2. 模块级：

```python
_STABLE_SESSION_S = 60.0

def _session_was_stable(owner: _MCPSessionOwner, now: float | None = None) -> bool:
    started = owner.ready_at
    if started is None:
        return False
    return ((now if now is not None else time.monotonic()) - started) >= _STABLE_SESSION_S
```

3. `_supervise`：

```python
if ok:
    ...
    await self._hold(server.name)
    ...
    self._mark_dead(...)
    self._publish(server.name)
    if _session_was_stable(owner):
        delay = _BACKOFF_MIN
    else:
        delay = min(delay * 2, _BACKOFF_MAX)   # 短命会话也要退避
```

4. 删除 `:1254` 与 `:1258` 的 `delay = _BACKOFF_MIN`；`_pause` 被 wake 只负责提前结束等待，不再重置节奏。

**测试计划**

`tests/test_mcp_lifecycle.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_an_unstable_session_does_not_reset_the_backoff` | monkeypatch `_BACKOFF_MIN=0.02`、`_BACKOFF_MAX=0.4`、`_STABLE_SESSION_S=3600`；工厂每次连上后立刻 `manager.note_transport_death("fs")`；记录 3 次连接间隔，`gaps[1] > gaps[0]` 且 `gaps[2] > gaps[1]` | 当前每次重置为 0.02，gaps 基本相等 |
| `test_a_session_stable_for_a_minute_resets_the_backoff` | 直接构造 owner，`ready_at = time.monotonic() - 120`，`_session_was_stable(owner) is True`；`ready_at = now - 5` → False | 当前无 `ready_at`/函数 |
| `test_a_manual_connect_still_tries_immediately` | 保留现有 `test_a_manual_connect_tries_now_and_resets_the_pause` 的行为：wake 后 <1s 发起下一次尝试 | 现有测试通过（证明退避不阻塞手动） |

**风险与回退**

- 一个“连上 59s 就掉”的 server 会退避到 60s；这是有意的稳定优先。
- `_hold` 被 manual connect 唤醒也会走不稳定分支翻倍，手动重连后若仍失败，后续等待会变长；符合“只有稳定才重置”。
- 回退：恢复 3 个重置点即可。

---

### MCP-09 OAuth 回调端口占用即抛

**现状**

- `ai_coding/core/mcp.py:613-620`：

```python
async def start(self) -> None:
    self._code = asyncio.get_running_loop().create_future()
    self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
    # 注释说 "OS assigns one"，但只有 self.port == 0 时才会
    if self.port == 0:
        self.port = self._server.sockets[0].getsockname()[1]
```

`callback_port` 默认 19876（`config/models.py:746-748`）。当端口被另一个登录、另一个程序占用时，`asyncio.start_server` 抛 `OSError(EADDRINUSE)`，被 `_remote_session_factory` 的 `_cm` 向外抛，最终用户只看到 “sign-in failed: [Errno 98]”。

**根因**

“端口可配置”与“端口可占用”没有分开：配置值被当成必须成功，注释里的回退逻辑只覆盖了 `port == 0`。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 抛错 | 无 | 常见场景直接失败 |
| B 先试配置端口，失败回退 0（推荐） | `except OSError` 后用 `0` 再 bind 一次，实际端口写回 `self.port` | 保留固定端口偏好；总能登录；redirect URI 由实际端口生成 | 极端情况下第二次也失败（继续抛） |
| C 永远用 0 | 不监听配置端口 | 最简单 | 每次登录 redirect URI 都变；已注册 client 若绑定固定 redirect 会失败 |

**推荐方案**

```python
async def start(self) -> None:
    self._code = asyncio.get_running_loop().create_future()
    try:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
    except OSError:
        if self.port == 0:
            raise
        _log.warning("MCP OAuth callback port %s is busy; using an OS-assigned port", self.port)
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
    self.port = self._server.sockets[0].getsockname()[1]
```

`redirect_uri` 在 `start()` 之后才读取 `callback.port`（`mcp.py:793-796`），所以回退自动生效。

**测试计划**

`tests/test_mcp_oauth.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_a_busy_callback_port_falls_back_to_an_os_port` | 先 `asyncio.start_server` 占住一个端口；`cb = _CallbackServer(port); await cb.start()`；`cb.port != port` 且能完成一次 GET | 当前 `start()` 抛 `OSError` |
| `test_port_zero_still_reports_the_actual_port` | `_CallbackServer(0)` 后 `cb.port > 0` | 通过（防回归） |
| `test_the_callback_stops_cleanly_after_a_fallback` | `await cb.stop()` 后端口可再次 bind | 新路径 |

**风险与回退**

- 回退端口后，某些 AS 可能拒绝未注册的 redirect URI（动态注册的 client 通常接受）；这是“先可用”的取舍。若 AS 强制固定 redirect，用户可把 `callback_port` 改成一个空闲端口。
- 两次 bind 都失败仍抛 `OSError`，由上层转成 `needs_auth`/失败行。
- 回退：删除 `except OSError`。

---

### MCP-10 `mcp_chars_warn` 永不触发；子 agent 无 MCP

**现状**

- 警告实现 `ai_coding/core/agent_loop.py:1833-1878`：

```python
tools, schemas = self._surface(session)
self._tools_text = short_json(schemas)
self._tools_chars = len(self._tools_text)
self._tools_by_origin = _tools_by_origin(tools)
mcp_threshold = int(getattr(self.settings, "mcp_chars_warn", 0) or 0)
mcp_chars = sum(
    chars for origin, chars in self._tools_by_origin.items() if origin != "local"
)
if not self._tools_warned and mcp_threshold > 0 and mcp_chars > mcp_threshold:
    ...
```

- `_tools_by_origin`（`agent_loop.py:247-259`）只遍历**注册的 BaseTool**。M2 之后远端工具已经不再注册成 BaseTool；生产里唯一 origin 非 local 的是 `MCPCallTool`（固定 schema，约 500 字符，`tools/mcp_call.py:26-34`）。因此 `mcp_chars` 永远远小于默认 2000，警告**结构性永不触发**。远端工具的真实成本在 catalog 文本里（`request_assembly.py:560` 每步注入 `catalog_builder()`）。
- 子 agent：`ai_coding/tools/builder.py:134-153` 的 `build_tools` 只调用 `build_tool_registry(...)`，没有 MCP manager，也没有 `MCPCallTool`；这是“子 agent 没有 MCP”的事实，但代码里没有说明。

**根因**

警告的计量口径停留在“每个远端工具一个 BaseTool”的旧架构；M2 换成 catalog 后没有同步。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 保持 | 无 | 配置项是死代码 |
| B `mcp_chars = max(旧口径, len(catalog_builder()))`（推荐） | 保留旧口径兼容测试；catalog 才是真实成本 | 改动 2 行；现有 `test_tool_surface_warning.py` 全部继续通过（无 catalog_builder 时取 0） | 需要 `agent_loop.py`（不在本批白名单） |
| C 只改 catalog 口径 | 删除 origin 统计 | 最干净 | 现有 `test_tool_surface_warning.py` 断言会失败，需要大改测试 |
| D 删除 `mcp_chars_warn` | 不警告 | 去掉死配置 | 失去唯一可行动的预算提示 |

**推荐方案**

`agent_loop.py:_warn_tools_chars_once`：

```python
catalog_chars = len(self.catalog_builder()) if self.catalog_builder is not None else 0
mcp_chars = max(
    sum(chars for origin, chars in self._tools_by_origin.items() if origin != "local"),
    catalog_chars,
)
```

`tools/builder.py:134-153` 的 `build_tools` 增注释（不改变行为）：

```python
# MCP is deliberately not exposed to sub-agents: a child has no MCP manager
# and no human to answer OAuth.  The parent's catalog is not inherited.
```

**测试计划**

| 文件 | 用例 | 断言 | 修复前失败点 |
|---|---|---|---|
| `tests/test_tool_surface_warning.py`（**不在白名单**） | `test_an_mcp_catalog_over_the_threshold_warns` | 传入 `catalog_builder=lambda: "x"*3000` 时 `mcp_chars == 3000` 且发一次 `ToolSurfaceWarning` | 当前 `mcp_chars` 只算 origin，为 0 |
| `tests/test_tool_surface_warning.py`（**不在白名单**） | `test_the_local_surface_alone_never_warns_with_a_catalog_builder` | 实际 catalog 为空时仍不警告 | 防误报（修复后应继续通过） |
| `tests/test_mcp_catalog.py`（辅助，不单独验收） | `test_the_manager_exposes_the_catalog_size` | `len(build_mcp_catalog(...)) > 2_000`（30 个长描述工具）；`incomplete_servers()` 可读 | 仅证明 catalog 大，不能证明警告 |

**风险与回退**

- `catalog_builder` 每轮多调用一次；它是纯函数（`build_mcp_catalog`），成本可忽略。
- 警告阈值仍是 `AIConfig.mcp_chars_warn` 默认 2000；catalog 默认上限 8000，因此正常大 server 一定触发，这是期望。
- **冲突**：本批次文件白名单不含 `ai_coding/core/agent_loop.py` 与 `ai_coding/tools/builder.py`，而这两处正是修复点；`tests/test_tool_surface_warning.py` 也不匹配 `tests/test_mcp*.py`。若不扩白名单，MCP-10 只能写方案不能落地。
- 回退：恢复原 `mcp_chars` 求和。

---

### MCP-11 test_mcp_progress.py 传过时字段

**现状**

- `tests/test_mcp_progress.py:43-54`：

```python
def _config() -> MCPConfig:
    return MCPConfig(
        enabled=True,
        connection_timeout=30,          # 当前 MCPConfig 没有该字段
        servers=[
            MCPServerConfig(
                name="progress",
                command=[sys.executable, str(SERVER)],
                enabled=True,            # 当前 MCPServerConfig 没有该字段
            )
        ],
    )
```

- `ai_coding/config/models.py:754-756`（`MCPServerConfig`）与 `789-793`（`MCPConfig`）都设了 `extra="ignore"`，所以这两个过时字段被**静默丢弃**，测试仍然能跑（已用 `.venv` 验证：`timeout.startup` 变成默认 60000，`disabled` 为 False）。
- 当前正确字段是 `MCPTimeout(startup=30_000)`（`models.py:707-720`）与 `disabled: bool`（`models.py:775-776`）。

**根因**

模型改过名/改过极性（`enabled` → `disabled`，单 `connection_timeout` → `MCPTimeout`），测试没有跟着改；`extra="ignore"` 让问题不可见。

**候选方案**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 不做 | 继续忽略 | 无 | 测试对预算的假设是假的；默认 60s 可能掩盖回归 |
| B 更新为当前字段（推荐） | `timeout=MCPTimeout(startup=30_000)`；删掉 `enabled`/`connection_timeout` | 与 `test_mcp_stdio.py:44-54` 一致 | 无 |
| C 把模型改成 `extra="forbid"` | 老旧配置立刻报错 | 防止未来的静默漂移 | 破坏用户配置兼容性，远超本项范围 |

**推荐方案**

```python
from ai_coding.config.models import MCPConfig, MCPServerConfig, MCPTimeout

def _config() -> MCPConfig:
    return MCPConfig(
        enabled=True,
        servers=[
            MCPServerConfig(
                name="progress",
                command=[sys.executable, str(SERVER)],
                timeout=MCPTimeout(startup=30_000),
            )
        ],
    )
```

**测试计划**

`tests/test_mcp_config_fields.py`（新文件，不受 gated `pytestmark` 影响；从 `test_mcp_progress` 导入 `_config`）：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_the_progress_fixture_uses_current_config_fields` | `cfg = _config()`；`cfg.servers[0].timeout.startup == 30_000`；`cfg.servers[0].disabled is False`；`"connection_timeout" not in cfg.model_dump()` | 当前 `startup == 60_000` → 失败 |

**风险与回退**

- gated 用例仍需要 `AI_CODING_MCP_STDIO=1` 才有意义；配置断言在任何环境都能跑。
- 回退：无（纯测试修正）。

---

## 2. 批次内顺序与依赖

| 顺序 | 项 | 依赖 | 理由 |
|---|---|---|---|
| 1 | MCP-11 | 无 | 纯测试卫生，先让基线断言真实 |
| 2 | MCP-02 | 无 | close 语义是后续生命周期测试的地基 |
| 3 | MCP-08 | MCP-02 | 重连退避测试依赖 owner 能被可靠关闭 |
| 4 | MCP-03 | MCP-08 | 超时退役走同一条 `mark_dead + wake` 路径；退避稳定后再引入新的重连源 |
| 5 | MCP-09 | 无 | 独立，可在任意位置 |
| 6 | MCP-04 | 无 | 独立 |
| 7 | MCP-05 | 无 | 改 `_serve` 与 catalog 签名；在 MCP-03 之后避免同时改 `_serve` |
| 8 | MCP-06 | 无 | 两处判定与文案，独立 |
| 9 | MCP-07 | MCP-03/05 | 错误码要在所有新失败路径定型后统一命名 |
| 10 | MCP-01 | 无（但 world.py 与 MCP-05 都改构造处） | 默认关闭，放在功能项之后便于回归对比 |
| 11 | MCP-10 | MCP-01/05（catalog 稳定后） | 依赖 `catalog_builder` 与 catalog 文本形态；且需要白名单扩展 |

共享文件冲突提示：

- `core/mcp.py` 被 01/02/03/04/05/06/07/08/09 同时修改。建议按上表顺序单次成组提交（本批不 commit，仅规划）。
- `server/world.py` 只在 01（构造参数）与 05（catalog_builder）各改一次，放在一起改。
- `config/models.py` 只有 01 新增 `MCPConfig.sandbox`。

---

## 3. 明确不做的事

1. **不做 `tools/list_changed` 订阅**：MCP-05 只加注释说明未支持；动态刷新 catalog 需要 supervisor 重连协议，超出本批。
2. **不做 per-server sandbox 开关**：只做全局 `mcp.sandbox`；`MCPServerConfig` 不新增字段。
3. **不改 `process_sandbox.network` 默认值**：MCP-01 只沿用现有网络策略；`deny` 下本地 MCP 需要用户自行改 `inherit`/`proxy`。
4. **不改 OAuth 默认 `callback_port`**：仍是 19876，只做占用回退。
5. **不删除 `mcp_error`**：保留为旧 trace/第三方的兼容 leaf。
6. **不给子 agent 接入 MCP**：MCP-10 只写注释说明“刻意不接”；不新增 `MCPCallTool` 到子 registry，也不引入 manager 注入。
7. **不做 resources/prompts 分页**：本项只覆盖 `tools/list`。
8. **不做跨 server 的 catalog 完整性统计**：只按 server 名声明 `incomplete`。
9. **不改 `MCPToolAdapter` 的生产地位**：它仍是真实连接测试的 kernel，不重新注册为生产工具。
10. **不新增依赖、不改默认模型/上下文窗口、不联网**。
11. **本批次不 commit / 不 push**。

---

## 4. 文件清单（只列将改的文件）

### 4.1 白名单内、按方案会修改

| 文件 | 涉及项 | 改动要点 |
|---|---|---|
| `ai_coding/core/mcp.py` | 01/02/03/04/05/06/07/08/09 | 常量、argv 包装、close 等待、超时退役、结果裁剪、分页、文案、错误码、退避、端口回退 |
| `ai_coding/tools/mcp_call.py` | 03/07 | `on_session_stale` 回调；`_error` 带具体 code |
| `ai_coding/config/models.py`（仅 MCP 段） | 01 | `MCPConfig.sandbox: bool = False` |
| `ai_coding/server/world.py`（仅 MCP 构造/关闭） | 01/05 | 传入 `sandbox=(policy, backend)` 与 `sandbox_roots`；catalog_builder 带 `incomplete` |
| `ai_coding/domain/error_codes.py` | 07 | 新增 6 个 MCP leaf 映射，保留 `mcp_error` |
| `tests/test_mcp.py` | 01/03/04/05/07 | 新用例；同步 3 处 `mcp_error` 断言到新 leaf |
| `tests/test_mcp_lifecycle.py` | 02/06/08 | close 等待、remote 重连、退避 |
| `tests/test_mcp_catalog.py` | 05 | 不完整声明与预算 |
| `tests/test_mcp_oauth.py` | 09 | 端口占用回退 |
| `tests/test_mcp_call_tool.py` | 07 | 未知 server/tool、不可用 server 的 error/detail |
| `tests/test_mcp_progress.py` | 11 | 改用 `MCPTimeout`，删过时字段 |
| `tests/test_mcp_config_fields.py`（新） | 11 | 不受 gated `pytestmark` 影响的配置字段断言 |

### 4.2 白名单外、MCP-10 必需（需批准后才能改）

| 文件 | 涉及项 | 改动要点 |
|---|---|---|
| `ai_coding/core/agent_loop.py` | 10 | `mcp_chars = max(原 origin 统计, len(catalog_builder()))` |
| `ai_coding/tools/builder.py` | 10 | `build_tools` 上方注释：子 agent 刻意不接 MCP |
| `tests/test_tool_surface_warning.py` | 10 | 新增 catalog_builder 触发警告的用例（不改旧断言） |

> 如果白名单不扩展，MCP-10 只能停留在本方案文档，不能进入实现。
