# eval-02 · 功能矩阵（读代码得出，待实测/已实测）

> 来源：`docs/features.md`（81 KB，权威）+ `docs/data-contracts.md` + `docs/architecture.md`
> + 直接读 `ai_coding/**`。判据列给出可验证的判据。

## A. 入口 / UI

| # | 功能 | 入口 | 判据 | 状态 |
|---|---|---|---|---|
| A1 | 全屏 TUI 启动 | `ai-coding --path ... -w ...` | 进程起、bun 前端连上 IPC | ✅ 已实测 |
| A2 | 会话标签 / transcript | TUI | 抓屏可见 transcript 与事件块 | ✅ 已实测 |
| A3 | 流式输出 | TUI | 回复逐块渲染 | ✅ 已实测 |
| A4 | 工具事件块 | TUI | `✓ 已读取文件 · sample.txt · 1ms` | ✅ 已实测 |
| A5 | `/` 命令面板 | TUI 输入 `/` | 面板弹出 | 待实测 |
| A6 | `Ctrl-Q` 退出 | TUI | 进程退出 | 待实测 |
| A7 | `Esc` 中断回合 | TUI | transcript 留红色标记 | ⚠️ 文档记为**已知退化**（R12 起键位在已删除的 reader.py） |
| A8 | `Ctrl-Y` 复制工具全文 | TUI | 剪贴板 | 待实测 |

## B. slash 命令

| # | 命令 | 作用 | 状态 |
|---|---|---|---|
| B1 | `/help` `/resume` `/clear` `/quit` | 基础 | 待实测 |
| B2 | `/plan` `/plan run` | 计划模式 + 驱动 | 待实测（阶段 4/5） |
| B3 | `/compact` | 主动压缩 | 待实测 |
| B4 | `/memory` | 记忆统计 | 待实测 |
| B5 | `/tasks` `/tasks stop` | 后台子任务 | 待实测（阶段 4） |
| B6 | `/agents` `/trace` `/undo` `/review` | 代理表/轨迹/撤销/评审 | 待实测 |
| B7 | 未知命令编辑距离 ≤2 → `did you mean` 且不发模型 | `app/slash.py` | 待实测 |

## C. 内置工具（`tools/builder.py` 装配）

| # | 工具 | 权限 | 判据 | 状态 |
|---|---|---|---|---|
| C1 | `read` | ALLOW | 行号读取、分页 | ✅ 已实测（sample_tasks PASS） |
| C2 | `glob` | ALLOW | 通配匹配 | 待实测 |
| C3 | `grep` | ALLOW | 正则搜索 | 待实测 |
| C4 | `write` | WARN | 写入 + 过期读保护 | ✅ 已实测（write+edit PASS） |
| C5 | `edit` | WARN | 精确替换 | ✅ 已实测 |
| C6 | `bash` | WARN | 执行 + 超时 + 截断 | ✅ 已实测（bash-ok PASS） |
| C7 | bash 危险命令 | DENY | `sudo rm -rf /` 被拒且不执行 | ✅ 已实测（bash-deny PASS，模型自行拒绝） |
| C8 | `todo_write` | ALLOW | 计划清单跨回合存活 | ✅ 已实测（todo PASS，4 calls，survived=2） |
| C9 | `subAgent` | ALLOW | 委派 + ledger 回写 | ✅ 已实测（subagent PASS） |
| C10 | `skill` | ALLOW | 读技能目录 | 待实测 |
| C11 | `memory_search` / `memory_write` | ALLOW | 召回/写入 | ✅ 已实测（冒烟 notes 提取） |
| C12 | `checkpoint_write` | ALLOW | 本会话摘要环 | ✅ 已实测（r8 real_checkpoint_round_trip PASS） |
| C13 | `ask_user` | ALLOW | 模态提问 | ✅ 已实测（r6 offline） |
| C14 | `mcp_call` | WARN | 远端调用 | ✅ 已实测（stdio 往返，CI gate） |
| C15 | `webfetch` / `websearch` | WARN | 网页抓取/搜索 | 待实测 |
| C16 | `job_list/output/kill` | ALLOW | 后台 shell | 待实测（阶段 4） |

## D. 安全 / 权限

| # | 功能 | 判据 | 状态 |
|---|---|---|---|
| D1 | ALLOW/WARN/DENY 三级 | 门判定 | ✅ 已实测 |
| D2 | 审批面板打印参数 | WARN 层显示将做什么 | 待实测 |
| D3 | 沙箱限工作区 | `..`/绝对路径被拦 | 待实测 |
| D4 | 过期读保护 | `NEVER_READ`/`STALE` 阻止写入 | 待实测 |
| D5 | bash 硬拒绝模式 | `rm -rf /`/`sudo`/`shutdown`… | ✅ 已实测 |

## E. 上下文 / 回合编排

| # | 功能 | 判据 | 状态 |
|---|---|---|---|
| E1 | L1 裁剪中间段 | 超 `max_messages` 裁中段 | 待实测 |
| E2 | L2 信封化 | 头尾窗口 + 内容寻址落盘 | ✅ 已实测（r7 PASS；但 `l2_long_history_stays_under_budget` **FAIL**） |
| E3 | L3 超大输出持久化 | `<persisted-output>` 占位 | 待实测 |
| E4 | L4 真 LLM 摘要 | 摘要非空 | ✅ 已实测（冒烟 summary=OK len=227） |
| E5 | 回合墙钟 | `turn_timeout_seconds` → TIMEOUT | ✅ 已实测（watchdog PASS，elapsed=20.0s status=timeout） |
| E6 | 单次工具超时 | 结构化 `tool_timeout` | ✅ 已实测 |
| E7 | 子智能体超时 | `subagent_timeout_seconds` | 待实测 |
| E8 | MCP 连接超时 | `mcp.connection_timeout` | 待实测 |

## F. 停止 / 续跑

| # | 功能 | 判据 | 状态 |
|---|---|---|---|
| F1 | `stop_policy` natural/todo_aware/strict | 决策表 | ✅ 已实测（todo_aware 生效） |
| F2 | todo 门禁 | 依赖未做完挡住 | ✅ 已实测（r9 forward-reference cycle PASS） |
| F3 | 预算耗尽自动续跑 | `[continue]` nudge | 待实测 |
| F4 | `[acceptance-gate]` nudge | 验收门 | 待实测（阶段 5） |
| F5 | `[strategy-gate]` 失败升级 | 连续失败注入 | ✅ 已实测（r6 stalled_turn PASS） |

## G. 记忆

| # | 功能 | 判据 | 状态 |
|---|---|---|---|
| G1 | 收割（增量 + 对账） | ADD/UPDATE/SUPERSEDE/NOOP | ✅ 已实测（冒烟 notes） |
| G2 | 召回（BM25 + 双门禁） | R@3/FP@5 | ✅ 已实测（可达 R@3=0.99 FP@5=0.46） |
| G3 | 未命中诊断 | no_match/terms_known/near_misses | ✅ 已实测（retrieval_cases） |
| G4 | 整理（合并/提升/淘汰） | consolidate | ✅ 已实测（r10 write→recall→consolidate PASS） |
| G5 | 场景标签 / 位置引用 | scene / `m<idx>` | ✅ 已实测（r11c 文档记 93% 采纳率） |
| G6 | 写侧总开关 | `extraction_enabled` | 待实测 |
| G7 | 可选语义档 | embeddings | 待实测（默认关） |
| G8 | ✅ 批次增量 | 不丢不重、任一批 ≠ 整段历史 | ✅ **M-A6 已修正**（多回合合并是设计；断言已改） |
| G9 | ✅ 缓存前缀 | 剥离易失尾部后前缀完好 | ✅ **M-A6 已修正**（改用路径无关的布尔不变量） |

## H. 子智能体

| # | 功能 | 判据 | 状态 |
|---|---|---|---|
| H1 | worktree 隔离 | 子代理在临时 worktree | 待实测 |
| H2 | `output_schema` handoff | 结构化 facts | 待实测 |
| H3 | `background=true` 毫秒返回 | 调用即返 run id | 待实测（阶段 4） |
| H4 | `/tasks` 列表/停止 | 表行 + 停止 | 待实测（阶段 4） |
| H5 | 并发上限 | 信号量满报错 | 待实测 |
| H6 | ledger 回写 | `transcripts/subagents/*.json` | ✅ 已实测（冒烟 ledgers=1） |

## I. 技能 / MCP / 计划 / 可观测 / 验收 / 导出

| # | 功能 | 判据 | 状态 |
|---|---|---|---|
| I1 | 技能目录 + `skill` 工具 | 读技能正文 | 待实测 |
| I2 | MCP stdio 真连 | 往返 | ✅ 已实测（CI gate + 测试） |
| I3 | MCP `env/cwd` 透传 | 子进程环境 | 待实测 |
| I4 | 计划模式 `plan` agent | 只读 | 待实测（阶段 5） |
| I5 | `present_plan` 批准即 arm | `armed=True` | 待实测（阶段 5） |
| I6 | boot 续跑 | `find_armed_session` | 待实测（阶段 4） |
| I7 | trace / OTLP / HTML 导出 | 文件产出 | ✅ 已实测（r9 PASS） |
| I8 | 验收契约 + 验收门 | COMPLETED | 待实测（阶段 5） |
| I9 | 记忆 schema v2 迁移 | 旧文件只读迁移 | 待实测 |

## 汇总

- **已实测通过**：约 30 项（A1-A4、C1/C4-C9/C11-C13、D1/D5、E2/E4-E6、F1/F2/F5、G1-G5、H6、I2、I7）
- **实测偏差/缺陷**：3 项（G8、G9、E2 的预算子项）+ 1 个真实运行时缺陷（`httpx2`）
- **待实测**：约 30 项 → 阶段 4/5 补齐
- **已知退化**：A7（Esc 中断键位）
