# AI-Coding

ThoughtCoding 的 Python 化实现 —— 基于 **OpenAI Agents SDK** 的 AI 编程助手 CLI。

> Hook 责任链 + 权限门、四层上下文压缩、Git worktree 子代理隔离、记忆系统、MCP 集成、会话持久化。

## 技术栈

运行时代理引擎用 [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)；配置用 Pydantic + PyYAML；
CLI 用 Typer；终端 UI 用 prompt_toolkit + rich；bash 工具仅面向 **Linux**。

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

详见 [docs/](docs/README.md)。
