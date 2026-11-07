# B6 工具 / 会话 / 可观测 方案评审

结论：**CHANGES**（阻塞项已裁决，进入 S3）

## 阻塞项与裁决

| # | 阻塞 | 裁决（S3 必须照做） |
|---|---|---|
| 1 | X-01b 非 UTF-8 旧文件 `decode` 抛 `UnicodeDecodeError`（`ValueError`），不被 `except OSError` 捕获 | 显式捕获 `UnicodeDecodeError` 并返回 `detail="write_error"`，文案说明"文件非 UTF-8，append 不安全"；测试断言该错误码 |
| 2 | X-02b `trace overview` 未承接 `--config`，与 recorder 仍不同源 | `trace_overview` 复用同一个 `_trace_root(app_cfg)` 且接受 CLI 已加载的配置；若无法拿到配置，明确打印"使用默认目录"并写进帮助。测试须隔离 `XDG`/`AI_CODING_TRUST_PROJECT` |

## 非阻塞（采纳）

- X-02a 采用**最小方案**：`TraceRecorder._record` 在 `session_id` 为空时直接 return（丢弃 pre-session 事件），不建 `unknown/`，不做缓冲；文档写明"会话建立前的事件不落盘"。
- X-01d `consume_once` 放在 `_decide` 的 finally（含失败/取消），并套 try/except；测试断言第二次同名调用再次需要审批。
- X-01b `before = file_version(target)` 前移到 stale 检查之前，缩小检查到写入的窗口；注释说明 CRLF/BOM 走字节级往返。
- X-03a：跳过时间戳缺失/不可解析的记录（不删）；统一 naive/aware；根目录不存在直接返回。
- X-03b：`threading.RLock` 是工厂函数不能子类化，测试用代理锁；`mark_*` 复用既有 `_lock`。
- X-02c：只加 `Braid.failures` 与内部计数，`turn_report` 不改。
- 弱/恒真用例（X-03b 合法 JSON、X-01d `path_allowed`、X-03a running 不删、X-02b 默认目录）改写为可失败断言。
- X-03c：只加 REPL 别名；TUI 不支持写入已知限制（`command_names()` 不变量不改）。
- observer 内层抛 `BaseException` 仍穿透：接受，注释说明。

## 已确认无问题
- X-01a plan 模式下 `ls` 不受影响（schema 无 network 字段）；gate 直接 DENY 仅在该键出现时触发。
- X-01c `offset < 0` 判定正确。
- X-02c `turn_report` 与 `Braid` 同实例。
- 无新依赖、无删测、无新配置项（阈值用模块常量）。

## 进入 S3 清单
X-01a ~ X-03c（10 项）。
