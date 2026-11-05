# 真机长任务审计 · 第二轮（2026-11-05）

本轮覆盖此前未验证的链路：沙箱/安装、Skill 调用、MCP 安装与触发、
并行子智能体、以及一次从零构建前后端小项目的长任务。

- 模型：DashScope `qwen3.7-flash`（`context_window=131072`）
- 环境：Ubuntu 24.04 · `bubblewrap 0.9.0`（no-proc 层）· `tmux 3.4` ·
  Python 3.14 / `.venv` · Node 24 / npm 11
- 驱动：`/tmp/aic-r2/scripts/runner.py`（走生产组合根
  `server.world.build_world`，AutoApprove 仅用于无人值守）+ 仓库自带
  `scripts/eval_fullstack_task.py`
- 原始日志/会话：`/tmp/aic-r2/logs/`、`/tmp/aic-r2/ws-*/`

## 1. 任务矩阵与结果

| 场景 | 配置 | 结果 |
|---|---|---|
| S1 沙箱墙（文件/网络/缓存/后台任务） | enforce + deny | 完成；发现 **R2-3** / **R2-4** / **R2-5** |
| S2 安装（pip/npm/curl）+ 文件边界 | enforce + inherit | 完成；pip 11.6s、npm 4.4s、curl 577B 均成功；文件边界仍生效但命令返回 `rc=0`（R2-3） |
| K Skill 安装/列出/读取/自动选用 | enforce + deny | 安装与读取成功；第二回合 **300s 超时**、无产物，发现 **R2-1** / **R2-2** |
| M MCP 安装/启动/调用/错误路径 | warn + deny，4 个 server | 本地 stdio 成功；npx server 卡在 connecting，发现 **R2-6** / **R2-7** |
| A 并行子智能体（两个 worker 同条消息） | enforce + deny，隔离关闭 | 成功：spawn 82/80ms，worker 13s/18s，文件落在主工作区 |
| F 长任务：TaskFlow 全栈项目 | warn + inherit，8 轮门禁回喂 | **中途停止**：后端 12/12 通过，前端目录为空、README 缺失；模型在“已通过”后持续重复读取同一处，发现 **R2-11** / **R2-12** |

## 2. 问题记录

### R2-1（P1，已修复）Trace 把压缩摘要的每个流式片段写成一行

- 证据：Skill 第二回合 trace `turn-2.jsonl`：1427 行 / 310,594 B，其中
  `CompactionDelta` 1311 条、`CompactionStarted` 8 条。
- 位置：`core/compaction.py:176` 每个摘要 chunk 发 `CompactionDelta`；
  `core/trace.py:668` 只过滤 `ToolChunk`，未过滤 `CompactionDelta`。
- 影响：一次普通压缩就制造数百 KB trace；而模型/用户又可能去读 trace
  （Skill 的 Steps 就要求检查 trace），形成“读自己越读越大”的反馈环。

### R2-2（P1，已修复）简单诊断任务反复压缩直到回合超时

- 证据：Skill 第二回合 8 次 `CompactionStarted`、7 次 `CompactionEnded`；
  回合 300s 超时，`skill-diagnosis.md` 未生成；用量 297,537 prompt tokens /
  6,647 completion tokens。Fullstack 第 1 轮在 iter 23–26 出现
  `before_messages == after_messages` 的空压缩，iter 27/29 又真实压缩。
- 机制：`compaction_ceiling(48000)=32000`（预留 16k），而 provider 报
  prompt_tokens 34k–36k > 32k，于是每步重新开闸；当 `max_messages` 与
  L4 tail 让历史“压不动”时，出现连续空压缩；真实压缩后历史又被新读入的
  trace 撑大，再次压缩。
- 影响：长任务实际停滞、费用极高（单回合 29.7 万 prompt tokens），且
  trace 膨胀（R2-1）放大问题。

### R2-3（P1）沙箱内写工作区外返回成功，但文件消失

- 证据（enforce + deny 与 enforce + inherit 均复现）：
  `echo escape > /tmp/aic-r2/outside-*.txt; echo rc=$?` →
  工具结果 `OK ... rc=0`；宿主检查该文件不存在。模型据此总结
  “文件边界未拦截 / inherit 模式可在工作区外写”。
- 机制：bwrap 给沙箱一个私有 `/tmp`（tmpfs），命令在沙箱内确实写成功，
  宿主看不到；每次 bash 调用之间也不保留。
- 影响：安全边界实际成立，但命令结果给出相反的结论；用户会以为越界
  成功，或写的临时文件在下一步神秘消失。需要 bash 层识别重定向目标，
  或显式提示“/tmp 是沙箱私有层”。

### R2-4（P2）`tool_search` 触发 “unknown tool requires confirmation”

- 证据：三次 `tool_search` 都出现
  `ToolApprovalRequested reason="unknown tool 'tool_search' requires confirmation"`，
  随后工具本身执行成功。
- 影响：TUI 中会给一个纯读、纯本地工具弹审批；无人值守路径靠 AutoApprove
  掩盖了它。门禁应认识“延迟注册工具”而不是当成未知工具。

### R2-5（P2）后台任务管理链路对模型不够可用

- 证据：bash 描述让模型用 `job_list/job_kill`，但两者是延迟工具；模型先
  判断“工具列表中没有”，尝试 `ps aux`，在 no-proc 沙箱得到
  `Error, do this: mount -t proc proc /proc`；之后 `tool_search` 才加载
  `job_list/job_kill` 并成功 kill。
- 影响：多步后才找到正确工具；`ps` 永远不可用。建议 bash 描述直接提示
  “job 工具需要 tool_search”，或让 job 工具不被延迟。

### R2-6（P2）MCP 进度回调抛异常

- 证据：调用 `progress-local/slow_task(steps=5)` 时连续 5 条
  `ERROR [root] Progress callback raised an exception: 'NoneType' object can't be awaited`；
  工具仍返回 `did 5 steps`。
- 影响：进度不显示、日志被污染。看起来是 MCP 客户端内部把未设置的
  progress handler 当 awaitable 调用。

### R2-7（P1）`npx` MCP server 长时间停在 connecting，且示例包握手失败

- 证据：`everything-npx` 在 t+0…27.5s 一直是 `connecting`；模型在回合
  ~50s 调用得到 `MCP server 'everything-npx' is still connecting.`；
  同期 `broken`（`/bin/false`）2.5s 内正确变为 `failed: Connection closed`。
  单独 `npx -y @modelcontextprotocol/server-everything` 4.8s 就能启动并打印
  `Starting default (STDIO) server...`，但原始 MCP `initialize` 10s 无响应。
  `how-to-run.md` 的示例正是这个包。
- 影响：按文档配置的 MCP 示例开箱不可用；状态只有“正在连接…”，
  无法区分“在安装”与“协议不兼容”；catalog 不列出该 server，模型无法
  自查。另：`open_all()` 返回时 `servers()` 全是 `connecting`，虽然
  echo 已在 9ms 内可用，面板状态滞后于真实可用性。

### R2-8（P2，成本）内置工具面每步约 27–31k 字符

- 证据：Skill 回合 `tools_chars=30833`，Fullstack `tools_chars=27181`；
  Skill 第一回合 6 步就用了 37,765 prompt tokens。
- 影响：长任务每步都为工具 schema 付费；`mcp_chars_warn` 只统计 MCP，
  内置工具面增长没有同类提示。

### R2-9（P3）管道 `grep … | head` 被记为失败

- 证据：`grep … | head -30` 有输出却返回 `nonzero_exit`（SIGPIPE 141），
  模型把它当成失败并继续排查。
- 影响：常见只读命令被误报；错误文案可说明“管道提前关闭（SIGPIPE）”。

### R2-10（P3，观察）长任务里模型重复复述同一计划

- 证据：Fullstack 第 1 轮连续多步输出“我来从零构建…先完成后端…”等
  近似段落，重复占 completion tokens；不影响正确性。

### R2-11（P1，已修复）任务目标保留上一轮失败反馈，模型在测试通过后仍回退到失败叙事

- 证据：Fullstack round 3 第 12 步修复 `tasks.py:41`，第 13 步 pytest 已
  `12 passed`；第 14 步模型一次发出 **8 个 `todo_write`**，返回的
  `<task-state>` 里 `goal` 仍是上一轮的
  `后端测试还没通过…FAILED tests/test_tasks.py…`；随后第 15–18 步又回到
  “重新读取 tasks.py / 测试仍失败”，直到第 19 步再跑 pytest 才恢复。
- 机制：每轮 `task.goal` 直接保存该轮用户输入（这里是带 pytest 失败
  原文的回喂消息），`todo_write` 又把 goal 原样渲染回模型；goal 是
  “指令”还是“当前状态”在两处语义不同，模型把它当状态读。
- 影响：一轮 30 次迭代里约 11 次用于重复读取同一处、7 次用于重复复述；
  真实产物（前端）迟迟不开始。与用户报告的“上下文失效/进度不动”一致。

### R2-12（P1，已修复）长任务在“验证已通过”后仍反复读取，直到手动停止

- 证据：Fullstack round 3（含一次自动续跑）共 38 次迭代、46 次工具调用
  （仅 25 种唯一参数）；其中同一处
  `read backend/app/routers/tasks.py limit=5 offset=39` 重复 7 次、
  `limit=20 offset=38` 重复 6 次；pytest 已多次 `12 passed`，模型仍反复
  宣称“测试失败”。该轮 prompt tokens 累计 **1,492,655**、completion
  11,989；一次压缩 180→47 条 / 49,125→12,251 tokens。
- 运行 13 分 43 秒后手动停止：后端 12/12 通过、前端目录只有空的
  `public/` `src/`、根 `README.md` 不存在。
- 触发条件叠加：R2-11 的陈旧 goal + 搜索门禁把“同文件不同窗口”的读取
  当作 novel findings（`interventions.py:184` 只在
  `novel_findings=False` 时提示，`stale_streak` 指纹又因窗口不同而不重复），
  因此模型可以连续十几次只读同一文件而不触发 L1/L3。
- 影响：长任务表面在跑（每次调用都成功），实际不产出；自动续跑把成本
  放大到每轮百万级 prompt tokens。

## 3. 正面结论

- 子智能体：同一条消息里的两个 `spawn_agent` 并行执行（82/80ms 返回），
  `wait_agent` 收敛，13s/18s 完成，文件与报告正确。
- MCP：本地 stdio server 9ms 调用成功；未知 server 返回结构化错误；
  坏 server 快速 failed，不阻塞启动。
- 沙箱：`network=deny` 给出可操作提示；`~/.cache` 可写；工作区缓存可写；
  后台 job 可列出并 kill，`late.txt` 未产生；宿主 `/tmp` 文件未出现。
- 安装：enforce + inherit 下 `pip install`、`npm install`、`curl` 全部成功，
  安装后文件边界仍生效。
- Skill：`skill install/list/read` 正常；模型按技能原文产出 `skill-report.md`
  （Steps 与 Verification 均逐条原文）。

## 4. 修复状态（上下文/停滞簇）

| 项 | 修复 | 测试 |
|---|---|---|
| R2-1 trace 膨胀 | `core/trace.py` 将 `CompactionDelta` 与 `ToolChunk` 同等视为 live-only；`CompactionStarted/Ended` 仍落盘 | `test_trace_recorder.py::test_compaction_deltas_are_live_only...` |
| R2-2 压缩反复开闸 | `compress_if_needed` 用 provider 的 observed 数记录饱和（不再混用本地估算）；`should_compact` 对 observed 增长要求 ≥ max(1024, ceiling/16) 才重开 | `test_compaction_gate_working_view.py` 两条新用例 |
| R2-11 陈旧 goal | 只有空/琐碎（<16 字符）goal 才被新指令替换；实质目标在后续修正轮中保留 | `test_agent_loop_react.py` 两条新用例 |
| R2-12 重复读同一文件 | `TurnBooks` 按 read 输出的行号记录覆盖区间；≥50% 重叠即计为 stale，动作后清空 | `test_turn_books.py` 两条新用例 |

顺带修正两条既有测试与新契约不一致：`compact_now` 的 `reason` 字段（`test_compaction.py`）、
`/tasks stop` 的中文提示（`test_repl.py`）。

全量：`.venv/bin/python -m pytest -q` 通过（0 failed；仅 8 条按环境跳过的 live/stdio 用例）。

## 5. 待补/未验证

- `network=proxy` 白名单模式、`read_deny_paths`（如 `~/.ssh`）未在本轮覆盖。
- MCP remote / OAuth 未覆盖。
- Fullstack 最终门禁（后端 pytest、前端 build、README、双服务启动）需在修复后复跑。
- R2-3～R2-10 尚未修复：沙箱 `/tmp` 假成功、`tool_search` 误审批、job 工具延迟、
  MCP 进度回调、npx server 卡 connecting、内置工具面成本、SIGPIPE 误报。
- 子智能体 worktree 隔离 + `merge_agent` 未覆盖（本轮显式关闭隔离）。
