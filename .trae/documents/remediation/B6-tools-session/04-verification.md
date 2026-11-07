# B6 工具 / 会话 / 可观测 实现验证（S4）

## 结果
- 定向 343 passed；回归 250 passed
- 全量 pytest → **exit 0**（30 skipped）
- `ruff` 全绿；`mypy ai_coding` → 186 文件 0 错误

## 与 01/02 的一致性
- X-01a：`shell_read` 网络键直接 DENY；plan 模式 `ls` 不受影响。
- X-01b：`read_bytes().decode("utf-8")`；`UnicodeDecodeError → write_error`；`before` 前移；`write_text_atomic(expected_version)`。
- X-01c：`offset < 0` 判定，前缀同样拒绝。
- X-01d：`consume_once()` 在 `_decide` finally（含失败/取消）消费。
- X-02a：`session_id` 为空时直接 return，不再建 `unknown/`。
- X-02b：`trace_overview` 复用 `_trace_root(app_cfg)`；拿不到配置时明确提示默认目录。
- X-02c：`Braid.failures` 聚合；`turn_report` 不改。
- X-03a：7 天过期 + 最新 200；跳过不可解析时间戳；根目录缺失短路。
- X-03b：四个 `mark_*` 走既有 `RLock`。
- X-03c：REPL `rewind -> undo` 别名。

## 偏离（已接受）
- X-01c 额外用 `old.strip()` 比较：旧文件以换行结尾时"原样重交全文"不应被误判为前缀片段；前缀拒绝语义不变。
- `test_job_tools` 的 listing 用例时间戳改为当前时间（2026-01-01 的已结算行会被 7 天策略清理）；断言未弱化。
- 无全局 `--config`：命令内按运行时同一分层配置加载。

## 已知限制
- TUI 侧仍不支持 `/rewind`（`command_names()` 不变量不改）。
- 会话建立前的 trace 事件不落盘。
- observer 内层抛 `BaseException` 仍会穿透（`except Exception` 不变）。
