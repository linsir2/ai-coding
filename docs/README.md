# 文档导航

**ai-coding** 是基于 OpenAI Agents SDK 的 AI 编程助手 CLI（ThoughtCoding 的 Python 化实现）。本仓库文档按用途分为「面向使用的文档」与「里程碑演进记录」。

## 面向使用的文档

| 文档 | 内容 |
|---|---|
| [运行指南](how-to-run.md) | 环境要求、安装、配置、CLI 命令速查、端到端示例、验证与故障排查 |
| [功能清单](features.md) | 全面能力列表：CLI、内置工具、安全、上下文、记忆、子代理、技能、MCP、REPL |
| [架构设计](architecture.md) | 分层与依赖方向、模块职责、SDK 边界、组合根装配、回合数据流与扩展点 |
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

## 当前状态

主线里程碑 M0–M6、编排层重构 R1、可靠性收口 R2 全部完成，分阶段提交。
质量门禁：`pytest`（521 例，含 bash/worktree 真实子进程）全绿、`ruff` 0 问题、
`mypy strict`（按 3.10 下限检查）0 问题；MCP 真连在正常 OS 上另有 gate 测试 + CI 步骤。
