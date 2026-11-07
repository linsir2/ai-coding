# B5 子代理 / 后台 方案评审

评审对象：`01-research.md`（5 项）
结论：**CHANGES**（阻塞项已裁决为强制修正，进入 S3）

## 阻塞项与裁决

| # | 阻塞项 | 裁决（S3 必须照此实现） |
|---|---|---|
| 1 | SUB-02 TTL 读 `row.started`，resume 旧 handle 后会被立即退休 | handle 新增 `idle_since: float`，在所有"转为 idle"的转场刷新；TTL 只读该字段。`_restore_row` 不算 idle 起点 |
| 2 | SUB-02 `_closed` 在 shutdown 之后才置位，窗口内仍可准入并重启 reaper | `shutdown()` 第一行置 `_closed = True`，`run/spawn/_begin` 一律拒绝；再取消任务 |
| 3 | SUB-02 风险定性错误 | 文档改写：20s 超时 → 遗留孤儿 worktree（不丢已提交内容）；前景 `_run_isolated` 被取消时未 commit 即删目录属既有语义，写入已知限制 |
| 4 | SUB-02 缺并发 close 保护 | 新增 `_closing: set[str]`，`close()` 入口检查并加入，finally 移除；并发第二次直接返回 False |
| 5 | SUB-03/SUB-04 部分用例修复前即绿 | 标为回归护栏；SUB-03 只保留"并发同 key 第二次被拒"为红转绿 |
| 6 | SUB-02 TTL 用例需 await 退休任务；`sleep(0)` 易 flaky | 退休返回 task/list，用例显式 await；stop 用例改用轮询上限而非单次 sleep |
| 7 | SUB-01 `if not path: continue` 破坏"逐字节不变" | 不加该分支；无 branch 时输出与旧完全一致；有 branch 无 commit 时写"未提交" |
| 8 | SUB-04 `self.agents.subagents()` 无防御 | `getattr` 防御 + 空目录返回 "" |
| 9 | SUB-05 会丢失 worktree 未提交的项目规则 | 接受并写入已知限制（worktree = HEAD）；仅当 `workdir` 非空时切换 gate workspace |

## 非阻塞（记录）

- 仅 `spawn_background` 触发 prune，静默会话不退休：接受（有界），注释说明。
- 20s 上限下大仓库 git 最坏耗时未实测：留人工验证。
- TTL 用模块常量而非 settings：接受，避免死配置。

## 通过项

- `world.aclose` 顺序安全；`Scheduler.aclose` 内嵌 shutdown 可行。
- `spawn` 全链路无 await，无竞态；`run` 确有窗口。
- `<agents>` 静态段每会话稳定，不破坏前缀缓存。
- SUB-05 的 sandbox gate 本已同源，仅补 dispatch gate。
- 无新依赖、未改默认值。

## 进入 S3 的强制清单

SUB-01、02、03、04、05（5 项全部）。
