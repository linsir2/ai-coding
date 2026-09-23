# AI-Coding

ThoughtCoding 的 Python 化实现 —— 一个**自持 ReAct 循环**的 AI 编程助手 CLI。

> Todo 驱动编排 + 显式停止策略、权限门与审批、结构化工具结果、四层上下文压缩、
> Git worktree 子智能体隔离、记忆系统、MCP 集成、会话持久化与回合中断。

## 技术栈

编排循环、工具分发、上下文压缩、记忆与会话**全部是自家实现**；模型调用走一个可替换的
`ChatEngine` 端口，默认实现直连 OpenAI 兼容的 `chat.completions`（不依赖任何 agent 框架）。
配置用 Pydantic + PyYAML；CLI 用 Typer；终端 UI 用 prompt_toolkit + rich；MCP 用官方 `mcp` 包；
bash 工具仅面向 **Linux**。

## 开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # 运行测试
ruff check .    # 静态检查
mypy ai_coding  # 类型检查
```

## 文档

面向使用的文档从 [文档导航](docs/README.md) 进入：运行指南、功能清单、架构设计、数据契约；里程碑演进记录见 `docs/m0..m3-analysis.md` 与 [集成测试](docs/integration_testing.md)。

## 里程碑

- `M0` 骨架 / 数据契约 / 配置 / CLI 壳 / 基础设施
- `M1` AI 服务 + 主循环 + 会话持久化
- `M2` 内置工具 + 权限门 + 计划模式
- `M3` 四层上下文压缩 + 记忆 + 技能
- `M4` 子代理 + Worktree 隔离 + 直接命令
- `M5` MCP 集成 + UI 完善
- `M6` 收尾 + 全量迁移测试

## 编排层重构（R1）

M0–M6 的循环由 SDK 内部驱动；R1 把控制权收回本项目，并移除对 SDK 的依赖：

- `R1a-1` 契约层：`TaskState` / `StepResult` / 结构化 `ToolResult` / `ToolContext`
- `R1a-2` 引擎端口：`ChatEngine` + 直连 `chat.completions` 的实现
- `R1a-3` 工具分发器：权限门 + 审批 + 有界并发 + 结构化拒绝
- `R1a-4` 循环本体：显式迭代、todo 门禁、停止策略、每轮落盘、回合可中断
- `R1a-5` 子智能体：共享父上下文、结构化信息包、ledger 回写、并发上限
- `R1a-6` 收尾：MCP 直连、删除 SDK 路径、依赖与文档同步

设计文档：`docs/r1-analysis.md`（总方案）与 `docs/r1a-{1..6}-analysis.md`（逐子交付）。

## 可靠性收口（R2）

R1 把循环拿回来之后，剩下的是「它能不能永远收尾」：任意一次工具/模型/子智能体卡住，回合都必须有终态。

- `R2a` 硬超时原语 + 回合级看门狗：`infra/deadline.py`（心跳 tick + **放弃式取消**）、
  `ai.turn_timeout_seconds`（默认 600，0 关闭）、`TurnStatus.TIMEOUT`、工具级 `tool_timeout` 结构化结果
- `R2b` 进程执行统一：`infra/subprocess_runner.py`（线程 + 进程组 kill），
  `bash` 与 `git worktree` 都走它；顺带修了 `bash` 忽略 `ctx.workspace` 的真 bug
- `R2c` 拆分：`core/turn_policy.py`（任务注入 + 停止判定，纯单元，19 条决策表测试）
- `R2d` MCP 真连：会话属主任务 + 连接超时 + `env/cwd` 透传 + 真 stdio 往返测试（gate）
- `R2e` CI：`.github/workflows/ci.yml`，**不排除任何测试文件**（含 bash/worktree 与 MCP 真连）

设计文档与根因分析（含「工具为什么会卡住」的实测证据链）：[docs/r2-analysis.md](docs/r2-analysis.md)。

## 记忆层重做（R3）

R1/R2 之后，记忆层是最后一块「能用但不好用」的地方。10 条可复现案例（中文召回为空、
同一事实存 5 份、矛盾共存、FIFO 删掉最关键的那条、每轮重抽全量历史、早期决策在压缩后丢失）
决定了它的形状：

- `R3a` 引擎内核：中文 bigram 分词、`MemoryNote` 契约（kind/scope/来源/取代链/保护位）、
  idf 覆盖度 + 新近性 + 重要性打分（分数阈值 + 相关性硬门槛）、对账式写入、按价值淘汰
- `R3b` 编排：**系统提示逐字节静态**；记忆以尾部易失块注入；细节走 `memory_search` 工具
  （结果进历史、长期命中缓存）；收割只吃回合增量；压缩改为**按预算分页**（+ `/compact`）
- `R3c` 整理与运维：`consolidate`（合并/提升/过期/淘汰）、`ai-coding memory list|search|forget|pin`、
  `scripts/memory_cases.py`（11 条案例真跑）

实测：案例 11/11；缓存前缀 append-only **92%** vs 每轮压缩 **11%**；
真实 LLM 跨会话 A/B —— 会话 A 教「统一用 uv」，新会话 B 开着记忆答 `uv add`、关掉记忆答 `npm install`。

设计与实测记录：[docs/r3-analysis.md](docs/r3-analysis.md)。

详见 [docs/](docs/README.md)。
