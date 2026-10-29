# R22 批次 8 队列（按"先快后慢"排序）

> 输入：`docs/r22-feature-matrix.md`（A-1…A-15、O-1）、`docs/r22-context-audit.md`。
> 规则：**每项独立走一轮** 调研 → 方案 → 评审 → 实现 → 代码评审 → 修复 → 复审；
> 一项收尾（文档 + 测试 + 真机/离线证据）再开下一项。任一项评审不收敛就停在方案层，不硬改。

## 排序与理由

| 序 | 项 | 级别 | 为什么排这里 | 落点（已定位） |
| --- | --- | --- | --- | --- |
| 1 | **A-9** 调色板 | P2 | 纯前端、无契约、一帧即可验；先做能立刻看到的东西 | `component/command-palette.tsx`、`util/dialog-select.ts` |
| 2 | **A-13** OTLP span 名 | P2 | 后端一行、导出物可直接验（无需模型） | `core/export_otlp.py:113` |
| 3 | **A-10** 工具卡片时长含审批等待 | P2 | 记账口径问题，边界清晰但要动 `ToolFinished` 的计时起点 | `core/dispatch.py`、`component/toolcard.tsx` |
| 4 | **A-14** 任务清单没随完成收尾 | P2 | 要改 `stop_policy`/收尾语义，需先量影响面 | `core/turn_policy.py`、`domain/task.py`、`domain/turn_digest.py` |
| 5 | **A-12** 切换窗口内的提交仍带旧 id | P3 | 竞态，验证要渲染级测试；风险低但难验 | `routes/session.tsx:172,305`、`server/handlers.py:925` |
| 6 | **A-4** `bwrap.unavailable` 报成 `denied` | P2 | 牵动 `ToolResult.error` 契约 + `docs/data-contracts.md` + 前端映射表，改动面最大 | `core/dispatch.py`、`util/toolmeta.ts:100-115` |
| 7 | **O-1** 重复调用守卫挡住"重复读同一段来累加" | 观察 | 需要先设计（守卫是防循环的，不能简单放开） | 重复调用守卫所在处 + `read` 工具 |

## 执行进度

| 序 | 项 | 状态 |
| --- | --- | --- |
| 1 | A-9 调色板 | **收尾**（2026-10-29）：方案/评审/实现/代码评审/复审见 `docs/r22-batch8-a9-plan.md`；真机帧 `150–154`；前端 544 例 + tsc 全绿 |
| 2 | A-13 OTLP span 名 | **收尾**（2026-10-29）：`docs/r22-batch8-a13-plan.md`；8→9 例绿；真实 trace 122 span 名字 ≤17 字符、无 `|`；ruff/mypy 干净；新发现 A-16（scope 并组） |
| 3 | A-10 工具时长含审批 | **收尾**（2026-10-29）：`docs/r22-batch8-a10-plan.md`；真机帧 155 `14.1s（审批 13.9s）`，轨迹同值；后端 5 条新测 + 前端 550 例全绿 |
| 4 | A-14 任务清单收尾 | **收尾**（2026-10-29）：`docs/r22-batch8-a14-plan.md`；`update_goal(complete)` 关掉残留项；goal_tools 17 例（含门控 NUDGE→FINISH）；ruff/mypy 干净 |
| 5 | A-12 切换窗口旧 id | **收尾**（2026-10-29）：`docs/r22-batch8-a12-plan.md`；`switching` 闸门 + 暂存 + 失败回填；2 例渲染级新测，前端 552 例 + tsc 干净 |
| 6 | A-4 沙箱不可用被报成拒绝 | **收尾**（2026-10-29）：`docs/r22-batch8-a4-plan.md`；新码 `sandbox_unavailable`；真机帧 156（影子 `bwrap`）+ 轨迹 `error:"sandbox_unavailable"`；顺带更正"本机 enforce 不可用"（实际能走 `no-proc` 层），新发现 A-17 |
| 7 | O-1 重复调用守卫挡住重读累加 | **收尾**（2026-10-29）：`docs/r22-batch8-o1-plan.md`；守卫改按"结果还能不能复用"计数 + `2×阈值` 封顶；轨迹重放 iter3 ALLOW / 第 5 次 DENY；guard 29 例、ruff/mypy 干净 |

## 待办

| 序 | 项 | 状态 |
| --- | --- | --- |
| — | 全量门禁（pytest/ruff/mypy/bun test/tsc）+ 收尾报告 | 下一步 |

## 每项的既有证据（开题即可用，不必重找）

- **A-9**：调色板未过滤态只显示 8 条、`↑↓` 不滚动、无滚动指示；输入 `agents` 显示 `no matches`，而 `dialog.agents.show` 在代码里是 `palette: true`（`routes/session.tsx:449`）。帧：`/tmp/r22-shots/60-palette*`、`62`、`64`。
- **A-13**：`"name": "|".join(events)` 产出几百字符的 span 名（实测 `IterationStarted|ContextAssembled|AssistantMessage|ToolApprovalRequested|JobChanged|…`）；导出物 `/workspace/eval-workspace/r22-s1/.ai-coding/exports/traces-20261029-121135.json`。
- **A-10**：`✓ 已运行命令 · git status · 28.9s`，其中大部分是人工审批等待；`ToolFinished.duration_ms` 从发起算起（`turn-2.jsonl` 里 32,534ms 的那条）。
- **A-14**：goal 回合结束后 `task.items=[('1','in_progress'),('2','completed'),('3','completed')]`，模型主动指出"这是过期数据"并花掉一次迭代；该轮到 `迭代预算用尽（10 轮）`。帧 `111-goal-progress`。
- **A-12**：`app.tsx:253-259` 的 `await loadSession` 之后才 `switchTo`，这段时间 `buildSubmission(props.commands, props.sessionId, …)` 仍用旧 id，后端 `handlers.py:925` 回 `session not loaded`。
- **A-4**：`ToolFinished{error:"denied", output_preview:"error: bwrap.unavailable", duration_ms:32534}`（`/workspace/eval-workspace/r22-s1/.ai-coding/traces/43f3ab1a…/turn-2.jsonl`）。本沙箱 `bwrap` 建命名空间被拒，所以只能用 `process_sandbox.mode: "off"` 跑真机（`config.record.yaml` 有注释）。
- **O-1**：S3 收尾里模型自述"read 工具的重复调用守卫（duplicate_call）阻止了对同一参数的多次调用，我无法按 500 行一 chunk 的方式逐块累加统计"。

## 环境备忘（下一轮直接用）

- 真机：`tmux new-session -d -s <名> -x 190 -y 46` + `PYTHONPATH=/workspace/ai-coding .venv/bin/python -m ai_coding -p <config> -w <workspace>`；配置 `config.record.yaml`（48k）、`config.compact.yaml`（6k）。
- 录制代理：`setsid nohup .venv/bin/python scripts/recording_proxy.py --upstream https://dashscope.aliyuncs.com --port 8899 --out /tmp/record/<场景>.jsonl &`；**别用 `pkill -f recording_proxy`，会杀掉自己的 shell**，用 `ss -ltnp | grep 8899` 取 pid。
- 门禁：`ruff check ai_coding tests scripts`、`mypy ai_coding`（167 文件）、`pytest`（全量约 5 分钟）、前端 `cd ai_coding/frontend && bun test && bun run typecheck`。
- 工作区：`r22-s1`（代码/评审/计划）、`r22-s3`（6000 行日志，压缩/计数）、`r22-s5`（小模块，子代理/后台）。

## 收尾条件（批次 8 完成）

1. 七项都有结论：修了（含测试 + 证据）或明确"不修 + 理由"；
2. 全量 pytest / ruff / mypy / bun test / tsc 一次通过；
3. `docs/r22-feature-matrix.md` §8/§11 的状态列全部更新；
4. 一份收尾报告：本次总共修了什么、没修什么、下一个轮次该从哪里开始。

## 收尾报告（2026-10-29）

### 修了什么（7/7 全部有结论）

| 项 | 结论 | 关键证据 |
| --- | --- | --- |
| A-9 调色板 | 修 | 自己算滚动 + 行高 1 + 同名去重；真机帧 150–154；前端 544 例 |
| A-13 OTLP span 名 | 修 | span 名取最早事件；122 span ≤17 字符、无 `\|`；9 例 |
| A-10 时长含审批 | 修 | `approval_ms` 贯通到卡片；真机帧 155 `14.1s（审批 13.9s）` |
| A-14 任务清单收尾 | 修 | `update_goal(complete)` 关残留项；goal_tools 17 例 |
| A-12 切换窗口旧 id | 修 | `switching` 闸门 + 暂存/回填；2 例渲染级新测 |
| A-4 沙箱报成拒绝 | 修 | 新码 `sandbox_unavailable`；真机帧 156（影子 `bwrap`）+ 轨迹 |
| O-1 重读累加 | 修 | 守卫按"能否复用"计数 + `2×阈值` 封顶；s3 轨迹重放 iter3 ALLOW |

### 没修（有意留下，均有理由）

- **A-11（P1）`/resume` 后 reload 用旧 sessionId**：与 A-12 同根但改的是终态事件里的 id 权威性，单独一轮。
- **A-16（P2）子代理 span 与父循环并组**：分组键要加 `scope`，属契约级改动。
- **A-17（P3）失败卡片标题仍说"已运行命令"**：`toolTitle` 的通用语义，牵动多张卡片与快照。
- **A-5/A-6/A-7（P3）**：本地化/空态/字段口径，批次 5 范畴。
- **O-1 的"逐 chunk 明细表"**：单块 40 KB 必被 L3 持久化、L4 摘要，上下文预算问题；
  正确做法是每读一块即写计数或用 `grep -c`。守卫同批并行计数不共享属既有边界，记在 `r4-analysis.md` §8。

### 门禁

`pytest` 全量 4629 例（8 skipped：live/MCP stdio/本机 bwrap 可用）exit 0；`ruff`、
`mypy`（167 文件）干净；前端 `bun test` 553 通过 / 0 失败，`tsc --noEmit` 干净。

### 下一轮从哪里开始

1. **A-11**（P1）：`routes/session.tsx` 的 reload 分支改用 `event.data["sessionId"]`，或与 `new_session` 互斥；
2. **A-16**（P2）：`export_otlp` 分组键 `(scope, span)` + spanId 派生带 scope；
3. **A-17**（P3）：失败卡片标题的通用措辞；
4. 观察项：U-1（`/tasks` 后卡 `Working…`，需日志复现）；A-15 已在批次 7（`r22-batch7-h11-plan.md`）处理，矩阵未单列。
