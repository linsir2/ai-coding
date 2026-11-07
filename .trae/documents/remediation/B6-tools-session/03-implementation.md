# B6 工具 / 会话 / 可观测 —— 实施记录

基线：`fd5a929`（工作区另有其他批次未提交改动，本批次未触碰）。
范围：X-01a ~ X-03c 共 10 项。未 commit、未改默认值、未新增依赖、未联网、未改 `config/models.py`。

## 0. 结果

| ID | 状态 | 生产文件 |
|---|---|---|
| X-01a | 完成 | `tools/shell_read_tool.py`、`security/permission_gate.py` |
| X-01b | 完成 | `tools/write_tool.py` |
| X-01c | 完成 | `tools/write_tool.py` |
| X-01d | 完成 | `security/permission_gate.py`、`core/dispatch.py` |
| X-02a | 完成（最小方案） | `core/trace.py` |
| X-02b | 完成 | `cli.py` |
| X-02c | 完成（只加 `Braid.failures`） | `core/observers.py` |
| X-03a | 完成 | `core/jobs.py` |
| X-03b | 完成 | `service/session_service.py` |
| X-03c | 完成（别名可用；TUI 命令列表不发布） | `app/slash.py`、`app/commands.py` |

## 1. 逐项实现

### X-01a `shell_read` 不得取得宿主网络

- `ShellReadTool._requested_network()` 恒返回 `None`：即使绕过 gate 直调 `execute`，
  `spawn_argv` 也不会收到 `inherit`（执行层保证）。
- `ShellReadTool.precheck()` 先拒绝 `network_permissions` / `justification` 任一键
  （`detail="read_only_shell_required"`），再调用 `super().precheck(params)`（防御父类未来
  新增规则），最后做只读命令校验。
- `PermissionGate.check` 在 bash 红线检查之后新增：`shell_read` 且
  `network_permissions is not None` → 直接 **DENY**（不是 WARN），文案指向 `bash` 审批路径。
- plan 模式普通 `ls` 的 schema 不含这些字段，仍走 `_allow_tools` 的 ALLOW，已用测试钉住。

### X-01b append 原子化 + 版本复核

- `before = file_version(target)` 前移到 stale-read 检查之前。
- append 分支改为：
  - 已存在文件用 `read_bytes().decode("utf-8")` 读旧内容（字节级往返，保留 CRLF/BOM）；
  - `UnicodeDecodeError` → `detail="write_error"`，文案说明"文件非 UTF-8，append 不安全，
    文件未写入"，不改动原文件；
  - 最终 `write_text_atomic(target, old + content, expected_version=before)`，
    复用同目录临时文件 + `os.replace` + 版本复核。
- 新增/覆盖测试：base 版本冲突 → `stale_read` 且文件不变；CRLF 与 mode 位保留；
  非 UTF-8 明确报错且字节不变；新建文件 append 仍可创建（既有用例）。

### X-01c overwrite 前缀截断

- `_mid_file_slice_message`：`if offset <= 0` → `if offset < 0`，offset 0（严格前缀）同样拒绝。
- 同时把长度比较基准从 `len(old)` 改为 `len(old.strip())`：否则"原样重交全文"会因
  文件末尾换行被 `strip` 掉而误判为前缀片段。这是实现 02 文档测试计划
  "正文等于旧文件全文仍成功"的必要条件，未改变中段片段语义。
- 新增测试：`a\nb\nc\n` overwrite `a\n` → `overwrite_would_truncate`、文件不变；
  原样重交全文仍成功。

### X-01d `allow_once` 一次性

- `PermissionGate.consume_once(tool_name, params)`：只 `discard` 本次调用的 key。
- `ToolDispatcher._decide` 用 `try/finally` 包住 `_decide_inner`，finally 内
  `try/except` 调用 `consume_once(_authorized_name(item.tool, item.call), item.call.params)`，
  成功、拒绝、工具异常、取消都会回收。
- 测试：调用内 `path_allowed` 为 True、结束后 `_once_grants == set()`；第二次同调用
  再次进入审批；工具抛异常时同样回收；`grant`（always）不受影响。
- 不在 `_iter_granted_roots` / `path_allowed` 内部消费，避免一次调用内多次路径解析被误伤。

### X-02a 不在会话建立前写盘

- 采用 02 裁决的**最小方案**：`TraceRecorder._record` 在 `not self.session_id` 时直接
  `return`，不建 `unknown/`，不做缓冲、不补写。
- 语义：**会话建立前的事件不落盘**（world 启动期 MCP/lifecycle 事件会被丢弃）。
  `TraceStore.session_dir` 的 `or "unknown"` 保留，供旧数据/其他调用者读取。
- 测试：空 id 写事件后 `current_path is None` 且无 `unknown/`；绑定真实 id 后只写新事件。

### X-02b trace 根目录单一来源

- 新增 `cli._trace_root(app_cfg, workspace)`：`tracing.dir` 为空 → `<ws>/.ai-coding/traces`；
  否则 `Path(dir).expanduser().resolve()`。
- `_make_recorder` 改为调用该 helper。
- `trace_overview` 用与运行时相同的分层配置
  （`ConfigLoader.load_layers_report(Paths.for_workspace(workspace)).config`）解析，再调用
  同一 helper；配置加载失败时向 stderr 打印
  `could not load configuration; using the default trace directory (.ai-coding/traces)`，
  仍展示默认目录，帮助文本也写明了该行为。
- 测试隔离 `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `AI_CODING_TRUST_PROJECT`：
  自定义 `tracing.dir` 时只读到自定义目录（默认目录同时有数据，确保断言可失败）；
  未配置时仍读默认目录；配置抛错时打印回退提示且不隐藏已有 trace；
  `_make_recorder` 对 `~/...` 的解析与 `_trace_root` 一致。

### X-02c `Braid.failures`

- `Braid` 增加 `_failures` 计数与只读属性 `failures`：
  自身吞掉的异常 + 每个子 observer 的 `failures` 之和。嵌套 `Braid` 只计一次。
- `turn_report.py` 未改（`getattr(observer, "failures", 0)` 现在能读到聚合值）。
- `BaseException` 仍穿透（按 02 裁决接受；`except Exception` 形状不变）。
- 测试：自身失败计 1；`SafeObserver` 子项已计数时不双计；嵌套不双计；健康为 0；
  TurnReporter 对 `Braid(SafeObserver(boom))` 在 `finish_turn` 发出 `Degraded(observer)`。

### X-03a 已结算 job 保留上限

- `core/jobs.py` 模块常量：`MAX_SETTLED_JOBS = 200`、`SETTLED_RETENTION_DAYS = 7`
  （未改 `config/models.py`）。
- `JobRegistry.__init__` 在 `_rescan()` 之后调用 `_prune_settled()`：
  - 根目录不存在直接返回 0；
  - 只处理 `settled` 且 `status != running` 的行；
  - 时间取 `finished_at or started_at`，`parse_iso8601` 失败则跳过（不删）；
  - naive 时间统一按 UTC 处理；
  - 先删超过 7 天的，再按时间倒序保留最新 200；
  - 删除 `<id>.json`、`log_path`，锁内 `_jobs.pop`，`OSError` 逐条忽略并 debug。
- 测试：250 条 → 只剩最新 200、最旧 50 的 json/log 被删；8 天前删除、1 天前保留；
  running 行即使 30 天前也保留；无时间戳行保留；根目录不存在正常启动。

### X-03b `mark_*` 走共享 `RLock`

- `mark_pending_approval` / `mark_pending_posture` / `mark_pending_reminder` /
  `mark_write_ahead` / `clear_write_ahead` 的整个 try 块进入既有 `self._lock`（可重入）。
- 测试用代理锁统计 `__enter__`（`threading.RLock` 是工厂函数，不能子类化）；
  另加同 session 8 线程并发写，最终文件是一个完整写入者的 payload。

### X-03c `/rewind` → `undo`

- `app/slash.py` 增加 `"/rewind": "undo"`；`app/commands.py` HELP_TEXT 改为
  `/undo, /rewind  roll the last turn back (history + files)`。
- `command_names()` 未改：TUI 仍只发布 canonical 名。**已知限制：TUI 输入 `/rewind`
  不会当命令，需后续单独立项改前端或 `slash.list` 语义。**
- 测试：`/rewind`、`/rewind now`、大小写与空白均解析为 `undo`；`command_names()`
  仍不含 `rewind`；帮助含 `/rewind`；真实 REPL `handle_input` 返回 `RELOAD` 且历史回滚。

## 2. 测试与门禁（实际运行）

```bash
.venv/bin/python -m pytest \
  tests/test_read_only_shell.py tests/test_permission_gate.py tests/test_dispatch_grants.py \
  tests/test_tool_write.py tests/test_observers.py tests/test_turn_reporter.py \
  tests/test_trace_recorder.py tests/test_trace_text.py tests/test_cli_trace.py \
  tests/test_jobs.py tests/test_session_service.py tests/test_slash.py tests/test_undo_command.py -q
# 343 passed

.venv/bin/python -m pytest tests/test_gate_grants.py tests/test_m1a_grants_posture.py \
  tests/test_bash_effects.py tests/test_tool_bash.py tests/test_write_ahead.py \
  tests/test_write_tool_chunks.py tests/test_boot_resume.py \
  tests/test_dispatch_approval_timeout.py tests/test_trace_wiring.py tests/test_trace_store.py \
  tests/test_tool_dispatch.py tests/test_tool_observer.py -q
# 250 passed

.venv/bin/python -m pytest -q
# exit 0（全量，默认 addopts；30 skipped，均为平台/MCP 门控）

.venv/bin/ruff check ai_coding <本批次测试>   # All checks passed!
.venv/bin/mypy ai_coding                      # Success: no issues found in 186 source files
```

> `tests/test_trace.py` 在仓库中不存在；trace 相关实际文件为
> `test_trace_recorder.py`、`test_trace_text.py`、`test_trace_store.py`、`test_trace_wiring.py`。
> 全量套件在额外用 `-o addopts=""` 运行时出现 2 个非本批次问题：一个是
> `test_strict_markers_is_on`（被该覆盖本身打破），另一个是 job kill 的既有并发 flake
> （单独重复运行 3 次均通过）；默认 addopts 下全量 exit 0。

## 3. 偏离与需说明之处

1. **X-01c 额外一行**：长度比较基准改为 `old.strip()`。02 的"裁决/采纳"只写了
   `offset < 0`，但同一文档的测试计划要求"正文等于旧文件全文仍成功"；不调整会在
   旧文件以换行结尾时误伤合法全文重写。中段片段拒绝语义不变，前缀拒绝已加强。
2. **X-03a 触发一个既有测试的日期假设**：`tests/test_job_tools.py::
   test_listing_stays_bounded_and_says_when_it_truncated` 使用 2026-01-01 的已结算行，
   在 2026-11-07 会被 7 天保留策略清理，导致列表为空。已把该用例的时间戳改为当前时间，
   仍断言"列表最多 20 行 + 提示省略数量"，未弱化 listing 断言。这是保留策略的预期后果，
   已在用例注释中说明。
3. **X-02b 配置来源**：`trace overview` 没有全局 `--config` 选项，按 02 的推荐代码在命令内
   用 `ConfigLoader.load_layers_report(paths)` 加载；`_trace_root` 接收的是已加载的
   `app_cfg`，与 `_make_recorder` 同一入口。配置损坏时打印回退提示而不是静默空表。
4. **X-01d 回收点**：`_decide` 的 finally 会消费"本次授权名 + 本次 params"的 key；
   若某工具把一次性授权带到调用返回后的后台任务，授权会提前消失——这是预期的收紧。
5. **未改 `server/world.py` / `core/turn_report.py`**：前者通过 `_make_recorder`
   自动继承 X-02a/b 行为；后者按 02 裁决不动，`Braid.failures` 已足够。

## 4. 最需评审的 3 处 diff

1. `ai_coding/tools/write_tool.py`
   - `- if offset <= 0` → `offset < 0`，以及 `len(old)` → `len(old.strip())`；
   - append 的 `read_bytes().decode` + `UnicodeDecodeError` → `write_error` +
     `write_text_atomic(..., expected_version=before)`；
   - `before` 前移到 stale 检查之前。
2. `ai_coding/core/dispatch.py` + `ai_coding/security/permission_gate.py`
   - `_decide` 的 `finally: consume_once(...)`（含失败/取消，try/except 包裹）；
   - `consume_once` 只 discard 本次 key；
   - `shell_read` 网络键的硬 DENY，以及 `ShellReadTool` 的双层拒绝。
3. `ai_coding/core/jobs.py`
   - `MAX_SETTLED_JOBS` / `SETTLED_RETENTION_DAYS` 与 `_prune_settled()` 的"先过期、
     再限量、跳过无时间戳、根目录缺失直接返回"顺序；
   - 删除 ledger + log 并锁内出表。

## 5. 已知限制（不在本批次范围）

- `/rewind` 别名本身可用（凡走 `_ALIASES` 的解析都接受）；TUI 的
  `command_names()` 只发布 canonical 名，其客户端命令解析不提供该别名。
- 会话建立前的 trace 事件不落盘（02 裁决的最小方案）。
- append 拒绝非 UTF-8 旧文件（有意的正确性收紧）。
- job 保留清理只在启动时发生；孤儿 `.log`（无 ledger）不清理。

## 6. 评审整改（REQUEST-CHANGES）

只改评审列出的文件与对应测试；未 commit、未改 `config/models.py`、未改默认值、未弱化断言。

### P1

1. **X-01d 一次性键按“实际执行参数”回收**（`core/dispatch.py`）
   - `_decide` 的 `timing` 增加 `"once"` 槽；`_decide_inner` 在用户答
     `APPROVED` 后写入 `(named, dict(call.params))`。此处的 `call` 已是
     guard/hook REWRITE 之后的调用，`named` 也是 `_ask` 实际记录时的授权名。
   - `finally` 只消费该槽里的键，不再使用外层 `item`（原参数）；改写后的一次性
     键不再永久留在 `_once_grants`。
   - 新增 `test_a_one_off_yes_after_a_rewrite_consumes_the_rewritten_key`：
     guard 把 `outside/a.py` 改写为 `outside/b.py`，回答 YES 后
     `_once_grants` 为空，第二次同原参数调用仍需重新审批。
2. **X-02b `-p/--config` 贯通到 overview**（`cli.py`）
   - 父命令 callback 把 `-p/--path` 等写入 `ctx.obj`（Typer 不会把父参数传给
     子命令函数）。
   - `trace_overview` 接收 `typer.Context`，把 `ctx.obj["path"]` 作为
     `ConfigLoader.load_layers_report(paths, config_path)` 的显式层，与
     recorder 的加载入口一致。
   - 新增 `test_the_overview_honours_an_explicit_config_path`：
     `-p custom.yaml trace overview` 只读到该文件指定的 `tracing.dir`；
     自动 fixture 已隔离 `XDG_CONFIG_HOME`/`XDG_DATA_HOME` 与
     `AI_CODING_TRUST_PROJECT`。

### P2

1. **`shell_read` 网络键按“键存在”判定**（`security/permission_gate.py`）
   - `params.get("network_permissions") is not None` → `"network_permissions" in params`，
     与 `ShellReadTool.precheck` 一致；`null` 不再绕过 DENY。
   - 新增 `test_shell_read_with_a_null_network_key_is_still_denied`。
2. **回退 `write` 的 offset==0 前缀拒绝**（`tools/write_tool.py`）
   - `_mid_file_slice_message` 改为 `offset <= 0` 直接放行，只保留严格中间片段；
     注释同步说明“删掉尾部若干行”是合法整体重写。
   - 测试用 `test_dropping_the_tail_of_a_file_is_a_legitimate_rewrite` 替换原前缀
     拒绝用例；中间片段与全文重交用例保留。
3. **相对 `tracing.dir` 按 workspace 解析**（`cli.py`）
   - `_trace_root` 对非绝对路径先拼到已 resolve 的 workspace，再 `resolve()`；
     `~` 仍 `expanduser`。
   - 新增 `test_a_relative_trace_dir_is_resolved_against_the_workspace`：
     进程 CWD 与 workspace 各有一份同名目录，断言只读 workspace 那份。
4. **包装链上的 observer 失败计数**（`core/turn_report.py`）
   - 新增 `_observer_failures`：沿 `_inner` 链累加每个 `failures`（`Braid` 已在
     自身聚合其子节点，链在它处停止），`begin_turn`/`finish_turn` 共用。
   - 新增 `test_a_safe_observer_wrapping_a_braid_still_reports_degraded`：
     `SafeObserver(Braid(SafeObserver(boom)))` 在 `finish_turn` 发出 `Degraded`。
5. **`/rewind` 表述改为准确描述**（`app/slash.py`）
   - 别名注释写明：凡走 `_ALIASES` 解析的面都接受；TUI 的
     `command_names()` 只发布 canonical 名，因此其客户端命令列表不提供
     `/rewind`。同步修正 `tests/test_slash.py` 的用例说明，断言不变。
6. **append 大文件回退 + 软链确认**（`tools/write_tool.py`）
   - 已确认 `target` 来自 `Sandbox.resolve()`，`os.replace` 作用于真实文件，
     不会替换用户软链（已在代码中注明）。
   - 文件 > 8MB 时不再 `old + content` 全量重写：先复核 `file_version`，再
     `open('ab') + write + flush + fsync`，输出追加
     `(large-file in-place append)` 说明；小文件仍走原子替换与 UTF-8 校验。
   - 新增 `test_append_on_a_large_file_does_not_rewrite_the_whole_file`。

### 验证

```bash
.venv/bin/python -m pytest tests/test_tool_write.py tests/test_dispatch_grants.py \
  tests/test_permission_gate.py tests/test_cli_trace.py tests/test_trace_text.py \
  tests/test_turn_reporter.py tests/test_observers.py tests/test_slash.py \
  tests/test_jobs.py -q
# 246 passed

.venv/bin/python -m pytest tests/test_dispatch_rewrite_regate.py tests/test_gate_grants.py \
  tests/test_m1a_grants_posture.py tests/test_m1a_permissions.py \
  tests/test_m1a_session_permissions.py tests/test_permission_gate_equivalence.py \
  tests/test_permission_gate_mcp.py tests/test_write_tool_chunks.py \
  tests/test_write_ahead.py tests/test_write_coordinator.py \
  tests/test_trace_recorder.py tests/test_trace_store.py tests/test_trace_wiring.py \
  tests/test_trace_aggregate.py tests/test_trace_canonical.py \
  tests/test_session_service.py tests/test_undo_command.py -q
# 299 passed

.venv/bin/ruff check ai_coding tests/test_tool_write.py tests/test_dispatch_grants.py \
  tests/test_permission_gate.py tests/test_cli_trace.py tests/test_turn_reporter.py \
  tests/test_slash.py
# All checks passed!
.venv/bin/mypy ai_coding
# Success: no issues found in 186 source files
```

> `tests/test_trace.py` 仍不存在，实际 trace 用例为 `test_cli_trace.py`、
> `test_trace_text.py`、`test_trace_recorder.py`、`test_trace_store.py`、
> `test_trace_wiring.py` 等。

