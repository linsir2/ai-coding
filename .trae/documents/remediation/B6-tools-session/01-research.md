# B6 调研方案（工具 / 会话 / 可观测）

基线：`fd5a929`。范围：`INVENTORY.md` 中 B6 的 10 项（X-01a、X-01b、X-01c、X-01d、X-02a、X-02b、X-02c、X-03a、X-03b、X-03c）。
约束：只读调研 + 本文档；不改代码、不加依赖、不联网、不 commit/push、不改 `config/models.py`。

## 0. 结论速览

| ID | 问题 | 是否有推荐方案 | 风险级别 |
|---|---|---|---|
| X-01a | `shell_read` 可借 `network_permissions=inherit` 绕过审批 | 有 | 高（安全边界） |
| X-01b | `write(mode=append)` 非原子、无版本复核 | 有 | 中高（数据完整性） |
| X-01c | `overwrite` 前缀截断漏检 | 有 | 高（数据丢失） |
| X-01d | `allow_once` 不是一次性，且会永久扩大沙箱写根 | 有 | 高（权限放大） |
| X-02a | trace 产生 `unknown/` 幽灵目录 | 有 | 中（可观测/磁盘） |
| X-02b | `tracing.dir` 写入与 `trace overview` 读取双源 | 有 | 中（诊断失真） |
| X-02c | `Braid` 吞 observer 异常且不暴露 failures | 有 | 中（可观测失真） |
| X-03a | 后台 job ledger/log 无保留上限 | 有 | 中（磁盘增长） |
| X-03b | `SessionService.mark_*` 绕过 RLock | 有 | 中（并发丢写） |
| X-03c | `/rewind` 不存在 | 有（REPL；TUI 有冲突） | 低（易用性） |

最危险两项：**X-01a**（计划模式只读工具可拿到宿主网络，且审批分支只认 `bash`）、**X-01d**（一次 `yes` 会经 `authorized_roots` 变成进程级可写 bind root）。

---

## 1. 逐项调研

### X-01a `shell_read` 网络升级绕过审批

**现状**
- `ai_coding/tools/shell_read_tool.py:74-82`：`ShellReadTool.precheck` 只调用 `read_only_problem`，**没有**调用 `BashTool.precheck`。
- `ai_coding/tools/bash_tool.py:662-687`：父类 `precheck` 才校验 `network_permissions` 只允许 `inherit` 且必须带 `justification`；该覆盖使它被跳过。
- `ai_coding/tools/shell_read_tool.py:84-114`：`execute` 只拒绝 `run_in_background` 并做路径检查，随后 `return await super().execute(params, ctx)`。
- `ai_coding/tools/bash_tool.py:344-347`：`_requested_network` 只要 `network_permissions == "inherit"` 就返回 `"inherit"`。
- `ai_coding/tools/bash_tool.py:1046`、`:1083`、`:1113`：`execute` 读取该值并传入 job/前台 runner。
- `ai_coding/security/permission_gate.py:542-545`：网络升级 WARN 的条件是 `name == "bash"`；`shell_read` 不在其中。
- `ai_coding/security/permission_gate.py:331-340`：`shell_read` 在 `_allow_tools` 中，常规判定可直接 ALLOW。

**根因**
“只读 shell”只限制了命令文本，没有限制执行环境；工具级 `precheck` 的覆盖又跳过了父类的参数校验，策略层 (gate) 的网络升级分支又只按工具名 `bash` 匹配。三处各自都认为别处会兜底，结果是三处都不兜底。

**候选与取舍**
1. 只在 `ShellReadTool.precheck` 拒绝 `network_permissions`：最小，但直接调用 `execute`（测试、其他装配）仍可继承。
2. 覆盖 `_requested_network` 返回 `None`：执行层彻底不读该参数，安全，但参数照旧被静默忽略，用户看不出被拒。
3. 1+2+gate 硬拒绝：执行层与策略层双重保证，行为明确。
4. 在 gate 中把 `shell_read` 加入 WARN：会先弹审批、再被 precheck 拒绝，产生一次无意义的人工等待，不采用。

**推荐方案**
- `ShellReadTool` 覆盖网络请求，永不继承：

```python
@staticmethod
def _requested_network(params: dict[str, Any]) -> str | None:
    return None
```

- `precheck` 先显式拒绝任何网络键，再保留只读命令校验；并补一次父类校验（防御未来父类新增规则）：

```python
def precheck(self, params: dict[str, Any]) -> ToolResult | None:
    if "network_permissions" in params or "justification" in params:
        return ToolResult(
            success=False,
            output="error: shell_read never requests network",
            error="read_only_shell_required",
        )
    blocked = super().precheck(params)
    if blocked is not None:
        return blocked
    problem = read_only_problem(str(params.get("command") or ""))
    ...
```

- gate 在硬拒绝层增加一条（放在 `bash` 红线检查附近，返回 DENY 而非 WARN，避免“先问后拒”）：

```python
if name == "shell_read" and params.get("network_permissions") is not None:
    return Decision(
        PermissionDecision.DENY,
        "shell_read never requests host network; use bash with approval",
    )
```

**测试计划**
- `tests/test_read_only_shell.py`：新增 `ShellReadTool` 带 `network_permissions="inherit"` + `justification` 时 `precheck` 失败、`error == "read_only_shell_required"`；`execute` 直调也不会把 `network` 传给 backend（用记录型 backend 断言 `networks == []`）。
- `tests/test_permission_gate.py`：`gate.check("shell_read", {"command": "ls", "network_permissions": "inherit"})` 为 DENY；普通 `shell_read` 仍 ALLOW。
- 回归：`tests/test_bash_effects.py`、`tests/test_tool_bash.py`。

**风险**
- 若模型把 `justification` 当普通说明传给 `shell_read`，会被拒；schema 从未声明该字段，可接受。
- gate 用 DENY 是比方向文字（“禁用网络”）更硬的语义；若希望保留“拒绝原因可读”，DENY 的 reason 已给出替代路径。

---

### X-01b `write(mode=append)` 非原子、无版本复核

**现状**
- `ai_coding/tools/write_tool.py:226-234`：先取 `before = file_version(target)`，但 `mode == "append"` 走 `with target.open("ab") as handle: handle.write(...)`，既没有临时文件/rename，也没有把 `before` 交给任何复核。
- `ai_coding/tools/write_tool.py:236`：只有 overwrite 分支使用 `write_text_atomic(..., expected_version=before)`。
- `ai_coding/tools/write_tool.py:196-209`：append 与 overwrite 一样先做 `FileState` 陈旧读检查，但检查与写入之间仍有窗口。
- `ai_coding/infra/file_utils.py:31-68`：`write_text_atomic` 已支持 `expected_version` 与同目录临时文件 + `os.replace`。
- `ai_coding/tools/write_tool.py:328-334`：计划模式的 append 走 fd + `lseek(SEEK_END)`，且 `check_stat` 基于已打开的 fd；不属于本项。

**根因**
append 被当成“追加字节”而不是“生成新版本”。裸 `ab` 写入在进程崩溃/并发写时可能留下半行；`before` 被计算后从未使用，等于是死代码，也说明作者原本就想复核。

**候选与取舍**
1. 保留裸 append，加 `fcntl` 文件锁 + fstat 复核：锁跨平台性差，且崩溃仍可能半写。
2. 读旧内容 + `write_text_atomic(old + content, expected_version=before)`：完全复用既有原子写与冲突语义，崩溃只可能留下临时文件。
3. 先把旧内容复制到临时文件再追加：多一次大文件拷贝，复杂度更高。

推荐 2。

**推荐方案**
签名不变，仅替换 append 分支：

```python
if mode == "append":
    if target.exists():
        old = target.read_bytes().decode("utf-8")   # 不用 read_text：避免 CRLF 被换行转换
    else:
        old = ""
    write_text_atomic(target, old + content, expected_version=before)
else:
    write_text_atomic(target, content, expected_version=before)
```

保留现有 `except FileConflict -> stale_read` 与 `tracker.record` / `note_effect`。

**测试计划**
- `tests/test_tool_write.py` / `tests/test_write_tool_chunks.py`：
  - 已读文件 append 后内容为 `old + content`，且文件 mode 位保持；
  - 在 `before` 之后、写入之前修改文件（monkeypatch `file_version` 或写回旧版本）→ `detail == "stale_read"`，目标文件仍是并发写入者的内容；
  - append 到不存在文件可创建；
  - 非 UTF-8 旧文件 → `write_error` 且文件不变（明确拒绝，不做有损替换）；
  - CRLF 旧文件 append 后原有 `\r\n` 不被改写。
- 回归：`tests/test_infra_file_utils.py` 的 `write_text_atomic` 用例。

**风险**
- 大文件 append 需整文件读入内存；`MAX_CHUNK_CHARS` 只限本次 content，旧文件上限由 `_DEFAULT_MAX_CHARS=2_000_000` 的历史写入约束，可接受。
- 非 UTF-8 文件行为从“可追加”变为“明确报错”，是有意的正确性收紧。
- 原子 replace 会换 inode，和 overwrite 现状一致；已打开该文件的进程看到旧 inode，属既有语义。

---

### X-01c `overwrite` 前缀截断漏检

**现状**
- `ai_coding/tools/write_tool.py:121-146`：`_mid_file_slice_message` 用 `body = content.strip()`，`old.find(body)` 取偏移。
- `ai_coding/tools/write_tool.py:138-140`：`if offset <= 0: return None` —— 偏移 0（正文是旧文件的严格前缀）被放过。
- `ai_coding/tools/write_tool.py:217-224`：仅 overwrite 且目标存在时调用该检查。
- `ai_coding/tools/write_tool.py:136-137`：`len(body) >= len(old)` 已排除整文件重写。

**根因**
检查的意图是“正文是旧文件的一个片段”，但把 0 当成了“没找到/正常重写”。前缀正是最常见的事故形态：模型只提交文件开头一段，overwrite 把尾部全部截掉。

**候选与取舍**
1. `offset < 0` 才算未找到：一行改动，符合意图。
2. 用 `old.startswith(body)` 单独判定：等价，但会多一条分支，不必要。
3. 对 offset 0 只告警不拒绝：无法阻止数据丢失，不采用。

推荐 1。

**推荐方案**
```python
offset = old.find(body)
if offset < 0:
    return None
line = old.count("\n", 0, offset) + 1
```
偏移 0 时 `line == 1`，现有提示“第 1 行起的片段”成立。

**测试计划**
- `tests/test_tool_write.py`：
  - 旧文件 `a\nb\nc\n`，overwrite `a\n` → `detail == "overwrite_would_truncate"`，文件不变；
  - 旧文件多段，overwrite 中间片段（现有用例）仍拒绝；
  - 真正更短但非前缀的重写仍成功（现有 `test_a_legitimate_shorter_rewrite_still_passes`）；
  - 正文等于旧文件全文（strip 后等长）仍成功。

**风险**
- “有意的截断尾部”会被拒绝。提示已要求先 read 全文再提交完整内容；与现有中段片段规则一致。
- `strip()` 忽略首尾空白的语义不变，只扩大命中范围。

---

### X-01d `allow_once` 非一次性

**现状**
- `ai_coding/security/permission_gate.py:300`：`self._once_grants: set[str] = set()`。
- `ai_coding/security/permission_gate.py:866-880`：`allow_once` 只 `add(key)`。
- `ai_coding/security/permission_gate.py:926`：`_iter_granted_roots` 遍历 `(*self.grants, *self._once_grants)`，**从不删除**。
- `ai_coding/security/permission_gate.py:882-912`：`path_allowed` 用该遍历判断沙箱是否放行。
- `ai_coding/security/permission_gate.py:938-958`：`authorized_roots` 也用该遍历，返回写工具的 bind 根。
- `ai_coding/core/dispatch.py:947-963`：`YES` 时调用 `allow_once`，之后没有任何清理。
- `ai_coding/server/handlers.py:1178`：只读取 `_once_grants` 做展示，不消费。

**根因**
`allow_once` 的注释把它描述为“session-scoped 的一次性集合”，但实现只有写、没有消费点。更严重的是 `authorized_roots` 与 `path_allowed` 共用它：一次针对越界路径的 `yes` 会永久进入写根集合，后续任意 bash 调用都可能以该路径为可写 bind root，而 gate 的 `check()` 本身并不读取 `_once_grants`，审批语义与沙箱能力脱节。

**候选与取舍**
1. 在 `path_allowed` 命中后 `discard`：一次工具调用可能多次解析同一路径（`Sandbox.resolve` 多处调用），会把同一次调用误伤。
2. 在 `_iter_granted_roots` 内部消费：`authorized_roots` 可能被进程沙箱构建多次调用，同样有中途丢失风险。
3. 在 dispatcher 的“一次调用”边界消费：工具执行期间（含其内部多次 path 检查、子进程绑定）授权始终有效，调用结束立即删除。
4. 给 once 加 TTL：仍不是“一次性”，且引入时间语义，不采用。

推荐 3。

**推荐方案**
- `PermissionGate` 新增：

```python
def consume_once(self, tool_name: str, params: Mapping[str, Any]) -> None:
    """Discard the one-off approval for exactly this call, if any."""
    key = self.grant_key(tool_name, params)
    if key is not None:
        self._once_grants.discard(key)
```

- `ToolDispatcher._handle` 在一次调用的外围回收：

```python
try:
    result, denied, halted, approval_ms = await self._decide(...)
finally:
    consume = getattr(self.gate, "consume_once", None)
    if callable(consume):
        consume(_authorized_name(item.tool, call), call.params)
```

`_authorized_name` 已在 `dispatch.py:720` 使用，保证 `mcp_call -> server__tool` 的键与 `allow_once` 写入时一致。

**测试计划**
- `tests/test_permission_gate.py`：`allow_once("read", {...})` 后 `path_allowed` 为 True；`consume_once` 后为 False；`grant`（always）不受影响。
- `tests/test_dispatch_grants.py`：注册一个探针工具，在 `execute` 内读取 `gate.path_allowed(outside/x.py)`（应为 True），调用结束后再断言 False；并断言 `gate._once_grants == set()`。
- 回归：`tests/test_m1a_grants_posture.py`、`tests/test_gate_grants.py`。

**风险**
- 若某个工具把“一次调用”拆成异步后台任务并在调用返回后继续使用路径授权，授权会提前消失。后台 job 本就不应继承一次性审批（与 B5 的 job 无 workspace 持有语义一致），属预期收紧。
- 只消费匹配键；不同路径的 once 不受影响。

---

### X-02a trace 幽灵目录

**现状**
- `ai_coding/server/world.py:268`：`recorder = _make_recorder(app_cfg, str(paths.workspace), None)`，`session=None`。
- `ai_coding/cli.py:357-362`：`TraceRecorder(store, session_id=getattr(session, "session_id", "") if session is not None else "", ...)`，此处得到空串。
- `ai_coding/core/trace.py:399-400`：`session_dir` 把空 id 映射为 `"unknown"`。
- `ai_coding/core/trace.py:696-699`：`_turn_open` 为 False 且 `last_path is None` 时，任意首个事件都会 `begin_turn(self.session_id)` 并创建目录。
- `ai_coding/core/trace.py:684-699`：这段逻辑位于 `_record`，在 session id 被真正赋值之前也可能到达。
- `ai_coding/core/agent_loop.py:2198-2200`：真实 id 在首轮 `_bind_trace` 时才写入 recorder。
- MCP 管理器 (`world.py:269-276`) 直接持有 recorder，启动期事件会先到。

**根因**
recorder 的构造时机（world 启动）早于会话绑定；而 `_record` 把“空 session”当成合法会话，用 `unknown` 兜底并立即落盘。空 id 不是会话，只是“尚未绑定”。

**候选与取舍**
1. 改 `session_dir` 让空 id 抛错：会影响其他直接调用者与既有测试，且把策略塞进存储层。
2. 在 `begin_turn` 前由调用者判断空 id：只修 `_record` 一处，集中。
3. recorder 缓冲事件，拿到首个真实 id 后补写：保留启动期事件，且不建 `unknown/`，符合方向。
4. 直接丢弃空 id 事件：实现最简单但会丢 MCP 启动诊断，不采用。

推荐 3。

**推荐方案**
```python
_PENDING_EVENT_LIMIT = 500

# __init__
self._pending: list[Any] = []

def _record(self, event: Any) -> None:
    if not self.session_id:
        if len(self._pending) < _PENDING_EVENT_LIMIT:
            self._pending.append(event)
        return
    if self._pending:
        pending, self._pending = self._pending, []
        for pending_event in pending:
            self._record(pending_event)
    ...  # 现有逻辑
```
`TraceStore.session_dir` 的 `or "unknown"` 保留不动（其他调用者/旧数据仍可读），但 recorder 路径不再产生它。缓冲上限用于“会话始终未绑定”的极端情况，超出丢弃最旧事件。

**测试计划**
- `tests/test_trace_recorder.py`：
  - `TraceRecorder(store, session_id="")` 写 2 个事件后 `current_path is None`，`store.root` 下无 `unknown`；
  - 设置 `recorder.session_id = "sess-2"` 后再发一个事件，`sess-2/turn-1.jsonl` 同时包含缓冲事件与新生事件，顺序正确，且仍无 `unknown`；
  - 缓冲超过 500 条时内存有界（断言 `len(recorder._pending) <= 500`）。
- `tests/test_cli_trace.py` / `tests/test_trace_wiring.py` 回归。

**风险**
- 启动期事件会被并入“第一个真实 turn”，该 turn 文件比过去略大；但事件本就发生在首轮之前，语义上属于首轮前史。
- 若进程全程没有真实会话，启动期事件被丢弃（而非写入 `unknown/`），这是方向要求的结果。

---

### X-02b `tracing.dir` 双源

**现状**
- `ai_coding/cli.py:324-325`：`default_root = Path(workspace).expanduser().resolve() / ".ai-coding" / "traces"`，`root = getattr(tracing, "dir", "") or default_root`；配置值没有 `expanduser/resolve`。
- `ai_coding/cli.py:326-331`：`TraceStore(root, ...)` 直接使用上述值；相对路径与 `~` 取决于进程 CWD/HOME。
- `ai_coding/cli.py:631-633`：`trace overview` 固定 `ws / ".ai-coding" / "traces"`，完全不读 `tracing.dir`。
- `ai_coding/config/models.py:577-587`：`TracingConfig.dir` 默认为 `""`（只读证据，B4 拥有，不改）。
- `ai_coding/paths.py:122-133`：`state/traces` 仍是工作区默认布局。

**根因**
“写 trace 的 root”和“读 trace 的 root”各写了一遍，且只有一处读了配置；配置值还缺少统一归一化。配置了 `tracing.dir` 的用户，`trace overview` 永远看默认目录（空表），诊断结论因此失真。

**候选与取舍**
1. 只给 recorder 加 `expanduser().resolve()`：写入路径变了，overview 仍是默认目录，双源未解。
2. 抽一个共享解析函数，recorder 与 overview 共用：一处定义，不可能再漂移。
3. 删除 `tracing.dir` 配置：破坏兼容，且 B4 拥有配置模型，不采用。

推荐 2。

**推荐方案**
在 `cli.py` 增加模块级 helper（不新增配置项）：

```python
def _trace_root(tracing: Any, workspace: str | Path) -> Path:
    """The one resolved trace root both the recorder and `trace overview` use."""
    base = Path(workspace).expanduser().resolve()
    configured = str(getattr(tracing, "dir", "") or "").strip()
    return (
        Path(configured).expanduser().resolve()
        if configured
        else base / ".ai-coding" / "traces"
    )
```

- `_make_recorder`：`root = _trace_root(tracing, workspace)`。
- `trace_overview`：按运行时同样的分层加载配置，再调用同一 helper：

```python
from ai_coding.config.loader import ConfigLoader
from ai_coding.paths import Paths

paths = Paths.for_workspace(workspace)
app_cfg = ConfigLoader.load_layers_report(paths).config
ws = paths.workspace
root = _trace_root(getattr(app_cfg, "tracing", None), ws)
report = build_report(TraceStore(root, workspace=ws), since_days=since)
```

**测试计划**
- `tests/test_cli_trace.py`：
  - 在临时工作区写 `config.yaml`：`tracing: {dir: <ws>/custom-traces}`，把 session 放进该目录，`trace overview --workspace ws` 能列出；
  - 未配置时仍读 `<ws>/.ai-coding/traces`（现有用例保持）；
  - 测试内隔离 `XDG_CONFIG_HOME`/`XDG_DATA_HOME`，避免读取宿主用户配置。
- `tests/test_trace_text.py`：`_make_recorder` 在 `tracing.dir="~/..."` 时 `store.root` 等于 `Path(...).expanduser().resolve()`。

**风险**
- `trace overview` 新增配置读取（IO）；配置损坏由 `ConfigLoader` 既有容错处理。
- 相对 `tracing.dir` 仍按进程 CWD 解析（`resolve()` 的语义），与改动前一致；建议文档写明优先绝对路径/`~`。
- 项目层配置若把 `dir` 指到工作区外会被配置层过滤为空（现状规则），helper 无需感知。

---

### X-02c `Braid` 吞 observer 异常且不暴露 failures

**现状**
- `ai_coding/core/observers.py:74-93`：`Braid.on_event` 对每个 observer `try/except`，只写 debug 日志，没有计数、没有 `failures` 属性。
- `ai_coding/core/observers.py:51-71`：`SafeObserver` 自己维护 `failures`。
- `ai_coding/core/agent_loop.py:2178-2182`：实际装配是 `Braid(SafeObserver(base), SafeObserver(trace), tool_obs)`。
- `ai_coding/core/turn_report.py:112-114`：`begin_turn` 读 `getattr(observer, "failures", 0)`。
- `ai_coding/core/turn_report.py:222-227`：`finish_turn` 用同一计数判断是否报告 `Degraded(observer)`。
- `ai_coding/core/turn_report.py:206` 附近是 `report_step` 的记账；失败判定的真实位置是 112-114 与 222-227。

**根因**
子 observer 的失败被 `SafeObserver` 计数，但 `Braid` 是 TurnReporter 看到的“那个 observer”，它没有把子计数聚合出来，也没有记录自己吞掉的异常。`getattr(..., 0)` 把“没有这个属性”当成“0 次失败”，于是退化事件永远不会发出。

**候选与取舍**
1. TurnReporter 遍历 Braid 的 `_observers`：破坏封装，且 TurnReporter 不该知道 fan-out 结构。
2. Braid 暴露 `failures` 聚合属性：接口与 `SafeObserver` 对齐，TurnReporter 现有 `getattr` 直接生效。
3. Braid 在第一次失败时直接发 `Degraded`：需要 Braid 持有 emit，职责膨胀。

推荐 2。

**推荐方案**
```python
class Braid:
    def __init__(self, *observers: LoopObserver) -> None:
        self._observers = list(observers)
        self._failures = 0

    @property
    def failures(self) -> int:
        """Own swallows plus every child's count; TurnReporter reads this."""
        return self._failures + sum(
            int(getattr(observer, "failures", 0) or 0)
            for observer in self._observers
        )

    def on_event(self, event: LoopEvent) -> None:
        for observer in self._observers:
            try:
                observer.on_event(event)
            except Exception:
                self._failures += 1
                _log.debug(...)
```
`turn_report.py:112-114`、`:222-227` 的 `getattr(observer, "failures", 0)` 保持即可（现在能读到 Braid 聚合值）；可选加一个 `_observer_failures(observer)` 私有 helper 复用，不改变行为。

**测试计划**
- `tests/test_observers.py`：
  - `Braid(SafeObserver(Boom()), Sink())` 发一次事件后 `braid.failures == 1`；
  - 子 observer 自身已有计数时聚合正确、不重复计数（嵌套 Braid 只算一次）；
  - 全成功时 `failures == 0`。
- `tests/test_turn_report.py`（若存在）/ `tests/test_loop_events.py`：`begin_turn` 后制造一次失败，`finish_turn` 发出 `Degraded(component="observer")`。

**风险**
- `Braid` 现在有公开 `failures`，与 `SafeObserver` 同名同义；调用方若把它当“本轮失败数”读取，语义是累计值（与 `SafeObserver.failures` 一致），TurnReporter 用差值判定，兼容。
- 嵌套 Braid 会继承子 Braid 的聚合值，父层只加一次，不双计。

---

### X-03a 后台 job 无保留上限

**现状**
- `ai_coding/core/jobs.py:267-273`：`_jobs` 字典 + `RLock`，构造末尾 `self._rescan()`。
- `ai_coding/core/jobs.py:792-849`：`_rescan` 把 `job_*.json` 全部读回 `_jobs`，不按时间/数量清理。
- `ai_coding/core/jobs.py:719-736`：`discard` 只在“前台调用在等待期内完成”时删除日志与 ledger。
- 后台 job 结算后（`Job.finished_at`，`:153`/`:474`）ledger 与 `.log` 永久保留；`MAX_RUNNING_JOBS=10`（`:85`）只限并发，不限历史。
- `MAX_JOB_LOG_BYTES=20_000_000`（`:92`）限制单文件，不限制文件数量。

**根因**
保留策略只考虑了“运行中”与“前台已取走结果”两种生命周期，没有“已结算的后台历史”这一档；`_rescan` 的职责是恢复，不是清理。

**候选与取舍**
1. 每次结算后立即清理：会删掉用户还没读的结果，不可接受。
2. 启动时按“最近 200 条 + 7 天”清理已结算历史：符合方向，且不干扰当前进程内的新结果。
3. 增加配置项：被禁止（`config/models.py` 属 B4），改用模块常量。
4. 只限数量或只限时间：单用任一条都可能在“少量但极老”或“短期大量”场景失效。

推荐 2 + 模块常量。

**推荐方案**
```python
# jobs.py 模块常量
MAX_SETTLED_JOBS = 200
SETTLED_RETENTION_DAYS = 7

def _prune_settled(self) -> int:
    """Drop settled history older than the window, then cap the rest. Startup only."""
```

实现要点：
- 在 `__init__` 的 `self._rescan()` 之后调用 `self._prune_settled()`。
- 只处理 `job.settled` 且 `status != STATUS_RUNNING` 的条目；`unknown` 由 `_rescan` 置 `settled=True`，可清理。
- 时间取 `parse_iso8601(job.finished_at or job.started_at)`（`ai_coding/infra/time_utils.py:26-44`），失败按 `0` 处理（视为最旧）。
- 先按 7 天删除；剩余按时间倒序保留前 200，删除其余。
- 删除 `self._root/f"{job.id}.json"` 与 `job.log_path`，并在锁内 `self._jobs.pop(job.id, None)`；`OSError` 逐条忽略并 debug。
- 返回删除数量，便于测试与 debug 日志。

**测试计划**
- `tests/test_jobs.py`：
  - 构造 250 条已结算 ledger（`finished_at` 递增），实例化第二个 registry，剩余恰为 200，最旧 50 条 `.json`/`.log` 被删；
  - 8 天前的已结算行被删，1 天前保留；
  - `running` 行不被删（即使 `started_at` 很老）；
  - 损坏/无时间戳的行不会导致启动失败；
  - 现有 `_rescan` 用例（`test_a_rescan_adopts_a_job_that_is_still_running` 等）保持。

**风险**
- 清理发生在启动时，若用户 7 天未开工具，旧的 job 输出会消失；这是保留策略的代价，常量可在模块内调整。
- 只清理“有 ledger 的已结算 job”，孤儿 `.log`（无对应 `.json`）不在本项范围，见“明确不做”。

---

### X-03b `mark_*` 旁路锁

**现状**
- `ai_coding/service/session_service.py:127`：`self._lock = threading.RLock()`，注释明确“job watcher 线程与正常 turn save 共享”。
- `ai_coding/service/session_service.py:328-330`、`:344-360`、`:445-446`：`save` / `mutate` / `delete` 都持锁。
- `ai_coding/service/session_service.py:151-167`：`mark_pending_approval` 无锁，写 `path.with_suffix(".tmp")` 后 `os.replace`。
- `ai_coding/service/session_service.py:192-211`：`mark_pending_posture` 同样无锁。
- `ai_coding/service/session_service.py:233-247`：`mark_pending_reminder` 同样无锁。
- `ai_coding/service/session_service.py:275-293`：`mark_write_ahead` 同样无锁。
- `ai_coding/service/session_service.py:311-312`：`clear_write_ahead` 直接 `unlink`，也无锁。

**根因**
四个 marker 是后加的侧车文件，作者只考虑了“单写者、原子 replace”，没有把它们的临时文件名纳入共享锁。同一 session 的两个线程同时写同一 marker 时，共用的 `tmp` 路径会互相覆盖，`os.replace` 可能抛 `FileNotFoundError` 并被 `except OSError: pass` 吞掉，表现为静默丢写。

**候选与取舍**
1. 每个 marker 用唯一临时名（`tempfile.mkstemp`）：解决碰撞，但不解决“读改写”序列的丢更新，且改动模式不一致。
2. 所有 `mark_*` 走同一 `RLock`：与 `save/mutate/delete` 一致，最小且可解释。
3. 新增细粒度锁：过度设计，且与既有锁顺序可能死锁。

推荐 2。

**推荐方案**
四个 `mark_*` 与 `clear_write_ahead` 的整个 try 块包进 `with self._lock:`；签名不变：

```python
def mark_pending_approval(self, session_id: str, request: dict[str, Any] | None) -> None:
    with self._lock:
        path = self._pending_path(session_id)
        try:
            ...
        except OSError:
            pass
```

`RLock` 可重入，未来若某个 `mark_*` 被 `mutate` 内部调用也不会自锁。

**测试计划**
- `tests/test_session_service.py`：
  - 用一个记录 `__enter__` 次数的 `RLock` 子类替换 `svc._lock`，逐个调用四个 `mark_*` 与 `clear_write_ahead`，断言均进入锁；
  - 线程池并发对同一 session 写 `mark_pending_approval` 不同 payload，最终文件是合法 JSON 且无异常；
  - 现有 marker 读写用例保持。
- 回归：`tests/test_boot_resume.py`、`tests/test_dispatch_approval_timeout.py`。

**风险**
- 锁范围包含文件 IO，会让 marker 写入与 turn save 串行；这些写都很小，且本来就共享同一 session 文件状态。
- 读路径 `_read_pending_*` 不加锁仍安全：写入使用 `os.replace`，读到的要么旧文件要么新文件。

---

### X-03c `/rewind` 不存在

**现状**
- `ai_coding/app/slash.py:22-51`：`_ALIASES` 有 `/undo`、`/redo`，没有 `/rewind`。
- `ai_coding/app/commands.py:23-47`：`HELP_TEXT` 只列 `/undo`、`/redo`。
- `ai_coding/app/commands.py:212-236`：`cmd.name == "undo"` 的实际处理（`loop.undo_last_turn`）。
- `ai_coding/app/slash.py:54-63`：`command_names()` 只返回 `_ALIASES.values()` 的 canonical 名。
- `ai_coding/server/handlers.py:1360-1364`：TUI 的 `slash.list` 直接来自 `command_names()`。
- `ai_coding/frontend/src/util/submission.ts:39-46`（只读证据，不在白名单）：客户端只有在 `slash.list` 含该名字时才把行发给 `session.command`。

**根因**
用户习惯用 `/rewind`，但命令表只有 `/undo`；REPL 与 TUI 的命令判定都以 `_ALIASES`/`command_names()` 为准。

**候选与取舍**
1. 只加 `_ALIASES["/rewind"] = "undo"`：REPL 立即生效；TUI 不会把它当命令（见下“冲突”）。
2. 同时修改 `command_names()` 返回别名：会让 `/h`、`/q`、`/exit` 也进入发布列表，破坏
   `tests/integration/test_r17_defects.py:1156-1178` 的“发布名必须等于 canonical 名”不变量，不采用。
3. 把 `rewind` 做成独立 canonical 命令再映射到 undo：需要新增分支或让 `parse_slash` 返回别名，改动面大。
4. 改前端 `SLASH_COMMANDS`：前端不在白名单，且 `slash.list` 才是权威来源的设计会被绕过。

推荐 1，并明确 TUI 限制。

**推荐方案**
```python
# app/slash.py
_ALIASES: dict[str, str] = {
    ...
    "/undo": "undo",
    "/rewind": "undo",
    "/redo": "redo",
}
```
```python
# app/commands.py HELP_TEXT
"  /undo, /rewind  roll the last turn back (history + files)\n"
```

**测试计划**
- `tests/test_slash.py`：`parse_slash("/rewind").name == "undo"`，`parse_slash("/rewind foo")` 同样解析（现有 `parse_slash` 只取首个 token）。
- `tests/test_repl.py` / `tests/test_plan_command.py`：`/rewind` 走 undo 分支并返回 `RELOAD`。
- `tests/integration/test_r17_defects.py:1156-1178` 保持通过（`command_names()` 未改）。
- 帮助文本断言（若有）包含 `/rewind`。

**风险 / 与方向的冲突**
- **TUI 中输入 `/rewind` 仍会被当成普通 prompt**：`slash.list` 只发布 canonical 名，客户端据此判定。要让 TUI 识别，必须改前端 `SLASH_COMMANDS`（白名单外）或改 `command_names()` 语义（会破坏上述不变量）。本批次按方向只保证 REPL 别名与帮助文案；TUI 需后续单独立项。

---

## 2. 批次内顺序

按“安全边界 → 数据完整性 → 可观测 → 易用性”，并让同文件改动相邻：

1. **X-01d**（gate + dispatch）：先阻止一次性授权变成持久写根。
2. **X-01a**（shell_read + gate）：关闭网络绕过；与 X-01d 同为安全项，先于其他。
3. **X-01c**（write 前缀）：单行条件修正，先落地数据丢失防护。
4. **X-01b**（write append）：同文件，改为原子 + 版本复核；与 X-01c 一起评审。
5. **X-03b**（session_service）：独立的小锁修复。
6. **X-03a**（jobs）：启动清理，需新增常量与测试，独立。
7. **X-02c**（observers + turn_report）：先让失败可见，后续 trace 问题才有观测手段。
8. **X-02a**（trace recorder 缓冲）：与 trace 语义相关。
9. **X-02b**（cli trace root）：依赖 recorder 的 root 概念，放 X-02a 之后。
10. **X-03c**（slash 别名 + 帮助）：纯文案/别名，最后做，冲突项单列。

每步独立跑对应测试；批次收口跑全量 `pytest`、`ruff`、`mypy`（与 W1 门禁一致）。

---

## 3. 明确不做

- 不改 `ai_coding/config/models.py`（B4 拥有）；job 保留策略用 `jobs.py` 模块常量。
- 不改 `ai_coding/infra/file_utils.py`；append 复用现有 `write_text_atomic`。
- 不新增依赖、不联网、不 commit/push、不改默认值。
- 不修改 `TraceStore.session_dir` 的 `or "unknown"` 兜底（保留给旧数据/其他调用者），只在 recorder 侧保证不产生 `unknown/`。
- 不清理没有 ledger 的孤儿 `.log`（超出“已结算 job”范围）。
- 不改变计划模式 `_write_plan_file` 的 fd 追加语义（已有 `check_stat` 与单 fd 截断）。
- 不为 retention 增加配置项或 TTL 参数。
- 不修改前端 `SLASH_COMMANDS`，不把别名塞进 `command_names()`；`/rewind` 的 TUI 可发现性列为后续项。
- 不改 `_DEPRECATED`（`/rewind` 是别名，不是被吸收的旧命令）。
- 不重写 `PermissionGate.check` 的授权链，只在 `shell_read` 网络与 `consume_once` 两处做点状修改。

---

## 4. 文件清单

### 生产代码（均为修改，不新增文件）

| 文件 | 涉及项 | 改动摘要 |
|---|---|---|
| `/workspace/ai-coding/ai_coding/tools/shell_read_tool.py` | X-01a | 覆盖 `_requested_network`；`precheck` 拒绝网络键并调用父类校验 |
| `/workspace/ai-coding/ai_coding/security/permission_gate.py` | X-01a、X-01d | `shell_read` 网络硬拒绝；新增 `consume_once()` |
| `/workspace/ai-coding/ai_coding/core/dispatch.py` | X-01d | `_handle` 在调用结束时消费 once 授权 |
| `/workspace/ai-coding/ai_coding/tools/write_tool.py` | X-01b、X-01c | append 改为 `read_bytes().decode + write_text_atomic(expected_version)`；前缀 `offset < 0` 判定 |
| `/workspace/ai-coding/ai_coding/core/observers.py` | X-02c | `Braid` 记录自身异常并暴露 `failures` 聚合属性 |
| `/workspace/ai-coding/ai_coding/core/turn_report.py` | X-02c | 保持 `getattr(observer, "failures", 0)`（读取聚合值）；可选抽 helper |
| `/workspace/ai-coding/ai_coding/core/trace.py` | X-02a | `TraceRecorder` 空 `session_id` 缓冲事件，绑定后补写 |
| `/workspace/ai-coding/ai_coding/cli.py` | X-02b | 新增 `_trace_root()`；`_make_recorder` 与 `trace_overview` 共用 |
| `/workspace/ai-coding/ai_coding/core/jobs.py` | X-03a | 新增 `MAX_SETTLED_JOBS`/`SETTLED_RETENTION_DAYS` 与 `_prune_settled()`，启动时调用 |
| `/workspace/ai-coding/ai_coding/service/session_service.py` | X-03b | 四个 `mark_*` 与 `clear_write_ahead` 进入 `self._lock` |
| `/workspace/ai-coding/ai_coding/app/slash.py` | X-03c | 新增 `"/rewind": "undo"` |
| `/workspace/ai-coding/ai_coding/app/commands.py` | X-03c | `HELP_TEXT` 注明 `/rewind` |

### 测试（修改既有文件；不新增测试文件）

| 文件 | 涉及项 |
|---|---|
| `/workspace/ai-coding/tests/test_read_only_shell.py` | X-01a |
| `/workspace/ai-coding/tests/test_permission_gate.py` | X-01a、X-01d |
| `/workspace/ai-coding/tests/test_dispatch_grants.py` | X-01d |
| `/workspace/ai-coding/tests/test_tool_write.py` | X-01b、X-01c |
| `/workspace/ai-coding/tests/test_write_tool_chunks.py` | X-01b |
| `/workspace/ai-coding/tests/test_observers.py` | X-02c |
| `/workspace/ai-coding/tests/test_trace_recorder.py` | X-02a |
| `/workspace/ai-coding/tests/test_cli_trace.py` | X-02b |
| `/workspace/ai-coding/tests/test_trace_text.py` | X-02b |
| `/workspace/ai-coding/tests/test_jobs.py` | X-03a |
| `/workspace/ai-coding/tests/test_session_service.py` | X-03b |
| `/workspace/ai-coding/tests/test_slash.py` | X-03c |
| `/workspace/ai-coding/tests/test_repl.py` | X-03c |

### 只读证据（不在白名单内，不修改）
- `/workspace/ai-coding/ai_coding/tools/bash_tool.py`（父类网络参数与 precheck 现状）
- `/workspace/ai-coding/ai_coding/infra/file_utils.py`（`write_text_atomic` 语义）
- `/workspace/ai-coding/ai_coding/domain/message.py`、`ai_coding/infra/time_utils.py`
- `/workspace/ai-coding/ai_coding/server/handlers.py`（`slash.list` 与 `_once_grants` 展示）
- `/workspace/ai-coding/ai_coding/frontend/src/util/submission.ts`（TUI 命令判定，说明 X-03c 限制）
