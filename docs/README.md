# 文档导航

**ai-coding** 是自持 ReAct 循环的 AI 编程助手终端应用（ThoughtCoding 的 Python 化实现；R1 起不再依赖
OpenAI Agents SDK，只留 `openai` 与 `mcp` 两个库）。**入口 `ai-coding` 直接启动 Textual 全屏 TUI**：
顶部状态栏、中部可滚动 transcript（流式回复 + 工具事件块）、底部输入栏；slash 命令可手打，
也可输入 `/` 唤起命令面板。旧的 Typer 子命令（`ask` / `config-check` / `memory` / `trace`）已删除，
职责收进 slash 命令。本仓库文档按用途分为「面向使用的文档」与「里程碑演进记录」。

## 面向使用的文档

| 文档 | 内容 |
|---|---|
| [运行指南](how-to-run.md) | 环境要求、安装、配置、入口速查、端到端示例、验证与故障排查 |
| [功能清单](features.md) | 全面能力列表：入口、全屏 TUI、slash 命令、内置工具、安全、上下文、记忆、子代理、技能、MCP、R12 Textual UI（v1）与已知退化 |
| [架构设计](architecture.md) | 分层与依赖方向、模块职责、引擎边界、组合根装配、回合数据流与扩展点 |
| [数据契约](data-contracts.md) | domain 模型字段、会话 JSON 落盘格式、配置 schema、关键不变量 |

阅读建议：第一次使用从[运行指南](how-to-run.md)开始；需要理解内部设计看[架构设计](architecture.md)；对接或扩展数据看[数据契约](data-contracts.md)。

## 里程碑演进记录（开发追踪）

以下文档记录每阶段的拆解、TDD 用例与验收标准，属于开发过程档案而非使用说明。

| 文档 | 范围 |
|---|---|
| [M0 分析](m0-analysis.md) | 工程骨架、数据契约、配置、CLI 壳、基础设施 |
| [M1 分析](m1-analysis.md) | AI 服务 + 主循环 + 会话持久化 |
| [M2 分析](m2-analysis.md) | 内置工具 + 权限门 + 计划模式 |
| [M3 分析](m3-analysis.md) | 上下文压缩 + 记忆 + 技能 |
| [集成测试](integration_testing.md) | 全链路真实网络迁移验证方法 |

## 编排层重构记录（R1，开发追踪）

M0–M6 之后的一次结构性重构：把循环控制权从 SDK 收回本项目，并移除 SDK 依赖。

| 文档 | 范围 |
|---|---|
| [R1 总方案](r1-analysis.md) | 影响分析、执行范围、逐文件步骤、TDD 用例、架构图、自我核验；R1a-1 契约层也在其中 |
| [R1a-2](r1a-2-analysis.md) | 引擎端口：`ChatEngine` + 直连 chat.completions |
| [R1a-3](r1a-3-analysis.md) | 工具分发器：权限门 + 审批 + 有界并发 |
| [R1a-4](r1a-4-analysis.md) | 循环本体：迭代、todo 门禁、停止策略、落盘、中断 |
| [R1a-5](r1a-5-analysis.md) | 子智能体：共享上下文、结构化回包、ledger 回写 |
| [R1a-6](r1a-6-analysis.md) | 收尾：MCP 直连 + 删除 SDK 路径 + 文档同步 |

## 可靠性收口记录（R2，开发追踪）

R1 之后的第二轮：让回合**任何情况下都能收尾**，并把「工具卡住」的根因查清。

| 文档 | 范围 |
|---|---|
| [R2 分析](r2-analysis.md) | 工具卡住的根因证据链（沙箱 seccomp + 两个真实设计缺陷）、硬超时与回合看门狗、进程执行统一、`turn_policy` 拆分、MCP 真连、CI 与环境矩阵 |

## 记忆层重做记录（R3，开发追踪）

R2 之后：记忆层按 10 条可复现案例重做，并解决它与提示词缓存的冲突。

| 文档 | 范围 |
|---|---|
| [R3 分析](r3-analysis.md) | 10 条案例实测、分词/打分/对账收割、注入位置与缓存（7% vs 94%）、按预算分页压缩、`memory_search` 工具、11 条案例验收脚本与真实 LLM A/B |

## 可观测 / 门卫 / 钩子三件套设计（R4，开发追踪）

R3 之后：编排层已经跑得对、收得回、记得住，但它是黑盒。R4 补上「看得见」与「拦得住」。

| 文档 | 范围 |
|---|---|
| [R4 设计文档](r4-analysis.md) | **完整设计文档**：三层定义（观测/门卫/钩子）与分工判据；12 条必须守住的既有不变量 + 12 条新增原则；目标架构（分层图、含 A1–C4 判定管线的时序图、span 模型、存储布局）；R4a 可观测（事件契约 / JSONL 契约 / 账本口径 / cache 真值优先 / 脱敏）、R4b 门卫（守卫链 + 4 内置守卫 + **改写后重过权限门** + token 闸门）、R4c 钩子（点位 × 能力矩阵 + python/command 双形态 + 环境隔离 + 失败语义）；契约汇总、配置与兼容、28 个测试文件、9 条真实验收案例、风险自核；**15 个待拍板决策点** |
| [GenericAgent 调研](r4a-genericagent-research.md) | 外部对标（14.2k star，arXiv 2604.17091）：源码级拆解它的极简循环、9 原子工具、L0–L4 记忆、四段语境压缩、hooks、观测插件；逐项对照我方；由此产生的 4 条 R4 修订（cache 真值 / hook 双形态 / fail 语义 / 索引不变量）与「不建议照搬的 7 条」 |

## 循环内核拆分记录（R5，开发追踪）

R4 之后 `core/agent_loop.py` 涨到 788 行、`process_input` 单方法 229 行，因此触发一次纯结构重构。

| 文档 | 范围 |
|---|---|
| [R5 分析](r5-analysis.md) | **已实施**：现状实测、拆什么与不拆什么（YAGNI 边界）、四个新单元的接口、`process_input` 229→153 行、行为不变性证据（既有断言零改动 + 真实 LLM 事件序列逐项一致）、实际未达标项 |
| [R5 计划](r5-plan.md) | 9 个任务、3 个提交的实施步骤 |

## 回合记忆 / 失败升级 / 人在环（R6，开发追踪）

R6 处理四个候选（工作记忆锚、失败升级、`ask_user`、记忆证据），每一条都先用实测论证：
压缩探针、两次真实对照、一个真实的失败回合、两次「不许用工具」的断言实验。

| 文档 | 范围 |
|---|---|
| [R6 分析](r6-analysis.md) | **已实施**：四个实测、四个候选逐个设计、注入位置与缓存纪律、逐文件步骤、契约变更、数据流、TDD 用例清单、自我核验、14 个决策点 |
| [R6 计划](r6-plan.md) | 13 个任务的可执行计划（每步含失败测试与命令） |

两处实测修正（都写进 r6-analysis.md）：**摘要环只覆盖最近 12 轮**，更早的决策归记忆层；
**真实运行里模型是否去翻会话文件是它的自由**，环能保证的是「被压缩掉的那几轮有路可查」。

## 当前状态

主线里程碑 M0–M6 与 R1–R9 全部完成，分阶段提交。
质量门禁：`pytest` **1301 例通过 / 2 跳过**全绿（这是**含** `tests/test_pty_textual.py` 的数字；
CI 那条命令把它 deselect 掉，于是是 **1300 通过 / 2 跳过 / 1 deselected**——两个数字指同一次运行，
不是一个含 pty 一个不含）、`ruff` 0 问题、`mypy`（按 3.10 下限检查）0 问题（105 文件）；
离线验收脚本：`memory_cases` 11/11、`trace_cases` 6/6、`r6_cases` 9/9 全过、`r7_cases` 11/11、
`r7e_cases` 9/9、`r8_cases` 13/13、`r9_cases` 12/12、`r10_cases` 16/16；
检索基准 `retrieval_cases --check` 退出 0。
MCP 真连与真实 LLM A/B 在正常环境另有 gate 测试 + CI 步骤。

> **R10 的 0.25 相关度门槛让一个 fixture 措辞变成了承重结构。**
> `scripts/r8_cases.py::evidence_filter` 与 `tests/test_tool_memory_search.py` 里的 R8b 用例是
> **同一个三笔记题**（`t1` / `m1` / `l1`，查询「构建用什么」→ `['构建','建用','用什']`）。
> `m1` 必须与 `t1` / `l1` **共享 `建用`**：只共享 `构建` 时它覆盖 3 个已知词里的 1 个，
> `relevance 0.221 < 0.25` 的地板，于是被挡掉——**这条用例失败在相关度上，不是失败在
> evidence 上**。R10 的 Task 2 改了测试那份（`构建改用 uv` → `构建用 uv，改用别家`）却漏了脚本那份，
> 两处一起修好才回到 13/13。改动是 fixture 文本，**断言一个都没动**；两处现在都有一行注释说明
> 措辞为什么是承重的。
> 这类副本是检索改动的真实盲区：同一个 fixture 存在测试与脚本两份，改一处不会让另一处失败。
> 而且只在**本分支内** bisect 是分不出「本轮弄坏」与「本分支上更早的提交弄坏」的——基线要取
> 分支之前的那个提交。

R4（可观测 / 门卫 / hook）**已按 R4a → R4b → R4c 顺序实现**：设计与决策点见 [r4-analysis.md](r4-analysis.md)，
实施计划见 [r4-plan.md](r4-plan.md)，外部对标见 [r4a-genericagent-research.md](r4a-genericagent-research.md)。

R6（回合摘要环 / 失败升级 / `ask_user` / 记忆证据 / 索引不变量）**已按 R6a → R6b → R6c → R6d → R6e 顺序实现**：
设计与实测见 [r6-analysis.md](r6-analysis.md)，分步计划见 [r6-plan.md](r6-plan.md)，
验收脚本 `scripts/r6_cases.py`（离线 9 条 + 真实 3 条）。

### R4–R10 一览

里程碑表此前停在 R3，所以它在说谎：R4 之后的所有轮次都存在。逐轮方案、计划与执行状态写在
对应文档里，这里只列范围。

| 轮次 | 主题 | 文档 |
|---|---|---|
| R4 | 可观测 / 门卫 / hook 三件套（看得见 + 拦得住） | [r4-analysis](r4-analysis.md) / [r4-plan](r4-plan.md) / [外部对标](r4a-genericagent-research.md) |
| R5 | 循环内核拆分（纯结构重构，`process_input` 229→153 行，行为不变） | [r5-analysis](r5-analysis.md) / [r5-plan](r5-plan.md) |
| R6 | 回合摘要环 / 失败升级 / `ask_user` / 记忆证据 / 索引不变量 | [r6-analysis](r6-analysis.md) / [r6-plan](r6-plan.md) |
| R7 | 循环瘦身收口 + L2 头尾窗口 + 人在环两处收口 + MCP 工具面治理 | [r7-analysis](r7-analysis.md) / [r7-plan](r7-plan.md) / [待办总账](r7-backlog.md) |
| R7e | 模型写的 checkpoint + 回合级 steering + 工具面阈值告警 + `memory_write` | [r7e-analysis](r7e-analysis.md) / [r7e-plan](r7e-plan.md) |
| R8 | 模型侧 checkpoint / 记忆 evidence 同分破平局 + 过滤 / 工具流式与 Esc 取消 | [r8-analysis](r8-analysis.md) / [r8-plan](r8-plan.md) |
| R9 | 依赖硬门禁（策略在工具层）+ 轨迹的 `overview` / HTML / OTLP-JSON 三个出口 | [r9-analysis](r9-analysis.md) / [r9-plan](r9-plan.md) |
| **R10（检索）** | 把检索变成**可测量的**：离线基准 + BM25 排序 + 两道门禁 + 召回会解释失败 + 命中即使用 + 过滤先于排序 | [r10-analysis](r10-analysis.md) / [r10-plan](r10-plan.md) / 执行计划 `docs/superpowers/plans/2026-10-01-r10-retrieval.md` |

**两处命名撞车，别混**：`R10`（检索）、`R11`、`R12` 是**检索轮**之后的编号；而
[终端界面重构](#终端界面重构r10--r12)那一节里的「R10 → R12」指的是**界面轮**的 R10–R12，
早于检索轮。文档里 `R10b` / `R10c` 永远指检索轮（R10a 基准 / R10b 排序与门禁 / R10c 诊断与过滤）；
R10d（embeddings）**刻意拆出未实现**，理由见 [r10-plan](r10-plan.md) 末尾的执行记录。

检索轮实测（R10 前 → R10 后）：R@1 0.68→**0.72**、R@3 0.80→**0.85**、FP@5 0.518→**0.350**、
paraphrase@3 6/12 不变、no-answer 4/6 不变。**通用词重叠的诱饵仍然泄漏**，三种修法都实测赔了
改述召回，所以它是被接受的残留而不是回归——细节与数字见
[功能清单](features.md) 的记忆章节。

## 终端界面重构（R10 → R12）

R10 先把界面从「逐行 `console.print()` + 单行提示符」换成 `prompt_toolkit.Application` 全屏壳
（实施计划见 `docs/superpowers/plans/2026-09-28-terminal-interaction-redesign.md`），但循环事件、
输入提交、人机通道与 `/model` 四处数据流都没接上。

R12 把它整体重写为 Textual（设计与计划见 `docs/superpowers/specs/2026-09-28-textual-ui-design.md`、
`docs/superpowers/plans/2026-09-28-textual-ui.md`）：`ui/textual_app.py` 组装 App，
`ui/transcript.py` 承载流式 Markdown 与工具事件块（`ui/blocks.py`），`ui/status_bar.py` /
`ui/input_bar.py` / `ui/modals.py` 分别负责状态栏、输入栏与审批 / `ask_user` 模态；
旧的 `prompt_toolkit` 模块与依赖一并删除，入口只剩 `ai-coding` 一条。v1 已接：事件→块、提交、
审批、`ask_user`、slash、`/model`、命令面板；明确不做：欢迎页 / 侧栏 / 计划卡、`Esc` 取消、
steering。一条已知退化（模型驱动的 slash 命令不渲染工具事件块）见
[功能清单](features.md) 的「R12：Textual UI」节。

