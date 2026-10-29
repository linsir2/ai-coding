# R22 批次 9 队列（未修 P1–P3 逐项收口）

> 输入：`docs/r22-feature-matrix.md` §8 未修项、`docs/r22-batch8-queue.md` 收尾报告。
> 顺序：A-16（P2）→ A-17（P3）→ A-5/A-6/A-7（P3）→ U-1（观察，需复现）。
> 每项一节：调研 → 方案 → 评审 → 实现（TDD/RIPR）→ 代码评审 → 修复 → 真机。

## 进度

| 序 | 项 | 状态 |
| --- | --- | --- |
| 1 | A-16 OTLP 子代理 span 并组 | **收尾**（2026-10-29）：`docs/r22-batch9-a16-plan.md` §8；写入端还原内部树 + 导出按 `(scope, span)`；96 例绿、ruff/mypy 干净；真机旧轨迹 24 span/24 id、新轨迹 14 span/14 id，父链正确 |
| 2 | A-17 失败卡片标题 | **收尾**（2026-10-29）：`docs/r22-batch9-a17-plan.md` §6；失败/被拒标题改“已尝试{动词}”；前端 556 例 + tsc 绿；真机帧 159 `✗ 已尝试运行命令 · ls` |
| 3 | A-5 `/todo` 空态 | **收尾**（2026-10-29）：`docs/r22-batch9-a5-plan.md` §6；中文空态“开始一轮对话后建立”；60 例绿；真机帧 160 |
| 4 | A-6 空态一致性 | **收尾**（2026-10-29）：`docs/r22-batch9-a6-plan.md` §6；`emptyLabel` + 三面板文案；前端 563 例 + tsc 绿；真机帧 161–163 |
| 5 | A-7 模型字段口径 | **收尾**（2026-10-29）：`docs/r22-batch9-a7-plan.md` §5；`/config` 改 `default_model: qwen（qwen3.7-flash）`；59 例绿；真机帧 164 |
| 6 | U-1 `/tasks` 卡 Working… | **收尾**（2026-10-29）：`docs/r22-batch9-u1-plan.md` §6；`orphanTurnId` 解决终态早于 ack 的永久 busy；前端 566 例 + tsc 绿；真机连跑 3 轮回到 Ask anything；新观察 U-2（无代码影响） |

## 每项的既有证据（开题即可用）

- **A-16**：s5 轨迹 `dc6b6368…/turn-1.jsonl` 47 行；按 `span` 14 组、按 `(scope, span)` 21 组；
  7 个同名键（`i1/i2/i3/step1/step2/step3/turn`）被父、子共用。
- **A-17**：A-4 真机帧 156：`✗ 已运行命令 · ls` + 副标题"沙箱不可用（命令未执行）"；
  `toolTitle()` 只看 running/结束，不看失败 tone。
- **A-5**：`commands.py` 的 `/todo` 空态分支仍是英文，其它命令已中文；帧 `13-todo`。
- **A-6**：`/mcp` 解释"为什么空 + 怎么开"，`/skills`、`/sessions` 只有 `no matches`；帧 `05/06/07`。
- **A-7**：`/config` 打 `model: qwen`（provider 名），`/status` 打 `qwen3.7-flash`（model id）；帧 `08/10`。
- **U-1**：`/tasks` 之后 TUI 卡在 `Working…`，输入被排进 inbox，两次 esc 无效，重启恢复；未复现第二次。

## 环境备忘

- 真机：`tmux new-session -d -s <名> -x 190 -y 46` + `PATH=... .venv/bin/python -m ai_coding -p <config> -w <workspace>`。
- 导出验证：`/export otlp` 落 `<workspace>/.ai-coding/exports/`。
- 门禁：`ruff check ai_coding tests scripts`、`mypy ai_coding`、`pytest`、前端 `bun test && bun run typecheck`。

## 收尾报告（2026-10-29）

### 修了什么（6/6 全部收尾）

| 项 | 结论 | 关键证据 |
| --- | --- | --- |
| A-16 OTLP 子代理 span | 修 | 写入端还原内部树 + `(scope, span)` 分组；旧轨迹导出 24 span/24 id、新轨迹 14 span/14 id；96 例 |
| A-17 失败卡片标题 | 修 | `已尝试{动词}`；前端 556 例；真机帧 159 |
| A-5 `/todo` 空态 | 修 | 中文空态；60 例；真机帧 160 |
| A-6 空态一致性 | 修 | `emptyLabel` + 三面板说明；前端 563 例；真机帧 161–163 |
| A-7 模型字段口径 | 修 | `default_model: qwen（qwen3.7-flash）`；59 例；真机帧 164 |
| U-1 卡 Working… | 修 | `orphanTurnId`；前端 566 例；真机连跑 3 轮 |

### 没修 / 新增观察

- **U-2（新，P3 观察）**：U-1 竞态下，结算前几毫秒排进 inbox 的行不会自动派发；永久 busy 已消除。
- A-6 范围外：`/stats`、REPL `/skills` 仍有英文行；`Search` 占位符未本地化。

### 门禁

`pytest` 全量 **4641 例** exit 0（8 skipped：live/MCP stdio/本机 bwrap 可用）；`ruff`、`mypy`（167 文件）
干净；前端 `bun test` **566 通过 / 0 失败**，`tsc --noEmit` 干净。

### 下一轮从哪里开始

1. **U-2**（观察）：ack 结算后的队列 flush；
2. `/stats`、REPL `/skills` 的英文空态（A-6 范围外，可并入一次小清扫）；
3. 工作区改动尚未 commit（本批仅要求先 push 上一批，见 `05e90a3`）。
