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

## 当前状态

主线里程碑 M0–M6 全部完成并分阶段提交推送至 `github.com/linsir2/ai-coding`。质量门禁：`pytest` 全绿、`ruff` 0 问题、`mypy strict` 0 问题。