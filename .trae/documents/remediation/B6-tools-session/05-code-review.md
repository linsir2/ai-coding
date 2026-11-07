# B6 工具 / 会话 / 可观测 代码评审（S5）

## 结论：APPROVE（第一轮 REQUEST-CHANGES，整改后通过）

## 第一轮 P1（已修）
1. **X-01d 回收键不匹配**：rewrite 后用户答 YES，`allow_once` 记改写后 params，finally 用原 params 回收 → 一次性键永久留在 `_once_grants`，`authorized_roots()` 永久放行。
   → `_decide_inner` 记录改写后 `(named, params)`，finally 用同一份消费（`dispatch.py:591-596,782`）；新增"改写 + YES + 第二次仍需审批"用例。
2. **X-02b 未接 `-p`**：`trace overview` 不走 CLI 指定的配置，与 recorder 不同源。
   → 经 `ctx.obj` 取 `-p/--path`，与 recorder 共用 `_trace_root`（`cli.py:320,668`）；新增 `-p custom.yaml` 用例（隔离 XDG 与 `AI_CODING_TRUST_PROJECT`）。

## 第一轮 P2（已修）
1. `network_permissions` 用 `is not None` → 改为**键存在**即 DENY，`null` 不再绕过（`permission_gate.py:487`）。
2. `offset == 0` 前缀拒绝会误判"删尾部行"的合法整体重写 → **回退该分支**，只保留 `offset > 0` 的中间片段检测（`write_tool.py:152`）。
3. 相对 `tracing.dir` 按进程 CWD → 改为按 workspace 解析（`cli.py:335`）。
4. `Braid` 被 `SafeObserver` 包裹时 failures 不可见 → `turn_report` 沿 `_inner` 链累加（`turn_report.py:46`）。
5. `/rewind` 表述改为"别名可用，TUI 命令列表不发布"（`slash.py`）。
6. append 大文件 O(size) 全量重写 → >8MB 走 `ab + flush + fsync + 版本复核`（`write_tool.py:258-275`）；确认 resolve 后不替换用户软链。

## 已核对通过
- X-01a：网络键单键 DENY；plan 模式 `ls` 不受影响；precheck 已调父类。
- X-01b：字节级往返保留 CRLF/BOM；`UnicodeDecodeError → write_error`；写前复核冲突 → `stale_read`。
- X-02a：首轮事件在 `_bind_trace` 之后，正常路径不丢；不再产生 `unknown/`。
- X-02c：直接子项聚合；异常仍吞但有 Degraded。
- X-03a：7 天 → 200；跳过 running / 无时间戳 / 根缺失。
- X-03b：四个 `mark_*` 与 `clear_write_ahead` 均入 `RLock`，锁内无 `save`，无死锁。
- 无删测、无新依赖、无新配置项。

## 已知限制
- TUI 命令列表不发布 `/rewind`（`command_names()` 不变量未改）。
- 会话建立前的 trace 事件不落盘。
- observer 内层抛 `BaseException` 仍穿透。
