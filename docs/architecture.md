# 架构设计

ai-coding 是一个分层、**引擎可替换**的对话式编码助手：领域契约、编排循环、工具分发、
上下文压缩、记忆与会话全部是自家实现；模型调用只经由一个薄薄的 `ChatEngine` 端口。

> R1（编排层重构）之后，项目**不再依赖任何 agent 框架**：循环归我们，SDK 已彻底移除。

## 包结构与依赖方向

```
ai_coding/
├─ cli.py          entrypoint：命令注册 + 组合根装配（唯一接触全部层的编排）
├─ domain/         纯数据契约 dataclass，零业务依赖，最底层
│                  R6 追加：turn_digest（回合摘要环 + 收尾交接渲染）
├─ config/         配置模型（Pydantic）与 YAML 加载，仅依赖 domain
├─ infra/          标准库封装：json / file / time / logger
├─ security/       沙箱、权限门、审批回调、过期读保护；R6 追加 user_prompt（模型主动提问的通道）
├─ tools/          工具契约 + 8 个内置工具 + 注册表 + schema 转换 + 装配
├─ ai/             引擎端口（ChatEngine）与直连 chat.completions 的实现、映射、提示装配
├─ core/           自持循环、工具分发、事件、上下文压缩、MCP、子智能体、worktree、项目说明
│                  R4 追加：observers（扇出）、trace（轨迹+脱敏）、budget（账本）、guard（门卫）、hooks（钩子）
│                  R5 拆出：turn_report / memory_turn / compaction / request_builder
├─ service/        会话持久化
├─ memory/         记忆检索 / 提取 / 聚合
├─ skills/         技能目录注册表
└─ ui/             REPL 交互 + slash 解析 + 富文本控制台 + 事件渲染
```

依赖方向单向：`domain` 不被任何层反向 import；`config`/`infra` 只依赖 `domain`/标准库；
`ai` 只依赖 `domain`（**不依赖 `tools`** —— 工具 schema 由调用方以 `list[dict]` 传入，
否则会与 `tools → security` 形成环）；`core` 是唯一同时编排各层的层；`cli` 是组合根。

## 模块职责

| 包 | 职责 | 关键入口 |
|---|---|---|
| `domain` | 不可变数据契约，跨层边界 | `ChatMessage`、`TaskState`、`StepResult`、`ToolResult`、`SessionData`；R6 起 `TurnDigest`（回合摘要 + 收尾交接，与 `TaskState` 一样自带 `render()`） |
| `config` | 配置模型、YAML 加载、camel→snake 归一化 | `ConfigLoader`、`ConfigManager`、`AppConfig`、`AIConfig` |
| `infra` | JSON/文件/时间/日志工具 | `json_utils`、`file_utils`、`time_utils`、`logger_setup` |
| `security` | 失败关闭的安全层 | `Sandbox`、`PermissionGate`、`ApprovalCallback`、`FileStateTracker`；R6 起 `UserPromptCallback`（模型主动提问的通道，`ask_user` 用） |
| `tools` | 工具契约、实现、注册表、schema | `BaseTool`、`ToolContext`、`ToolRegistry`、`build_tool_registry` |
| `ai` | 引擎端口 + 实现 + 映射 + 提示装配 | `ChatEngine`、`OpenAIChatEngine`、`PromptAssembler` |
| `core` | 编排与增强 | `AgentLoop`、`ToolDispatcher`、`ContextManager`、`MCPManager`、`SubAgentRunner`；R5 起另有 `TurnReporter`（对外陈述）、`TurnMemory`（记忆侧）、`Compactor`（压缩）、`request_builder`（易失尾部） |
| `service` | 会话读写（原子） | `SessionService` |
| `memory` | 记忆三层操作与持久化 | `MemoryService`、`MemoryStore` |
| `skills` | 技能目录扫描与目录文本 | `SkillRegistry` |
| `ui` | 交互终端与命令解析 | `run_repl`、`parse_slash`、`DialogueConsole`、`ConsoleObserver` |

## 引擎边界

`ChatEngine` 是唯一的模型调用端口，语义是**跑一步**而不是跑完一个回合：

```python
class ChatEngine(ABC):
    async def step(self, messages, *, tools, instructions=None, on_delta=None) -> StepResult: ...
    async def aclose(self) -> None: ...
```

`messages` **已包含本轮用户消息**，引擎不得追加任何东西（历史 bug：早期实现每回合发两条
相同的 user 消息，现在有回归测试兜住）。`tools` 是已经构建好的 OpenAI function schema。

默认实现 `OpenAIChatEngine` 直连 `chat.completions(stream=True)`：把 `delta.content` 累积成文本，
把按 `index` 分片的 `tool_calls` 增量合并成调用（`ai/toolcalls.py`，纯函数），
并可选地请求 `stream_options.include_usage` 取回 token 用量。

**换引擎只需实现 `ChatEngine`**：领域层、编排、分发、会话、记忆、UI 均不受影响。

## 组合根与装配

`cli.py` 是组合根：

```
ConfigManager.initialize(path) ─→ AppConfig
   ├─ build_engine_from_config(app_cfg) ─→ OpenAIChatEngine
   ├─ build_tool_registry(app_cfg, workspace, skills) ─→ ToolRegistry
   │     └─ Sandbox + FileStateTracker + 8 个工具（含 subAgent）+ 可选 skill
   ├─ MCPManager(app_cfg.mcp).open_all() ─→ 远端工具注册进同一个 registry
   └─ _build_loop(...) ─→ AgentLoop(engine, sessions, registry,
                                     gate, approval, context, memory, prompt,
                                     skills, project, settings, observer, workspace)
```

两条关键接线规则：

- **审批策略在组合根决定**：`ask` 是非交互入口，`approval=None` 被解释为
  「自动批准 WARN 级」（DENY 级照旧拦截）；`repl` 显式传 `CLIApprovalCallback` 弹 y/N/s。
- **权限门属于分发器，不属于注册表**：注册表只装工具，门与回调由 `AgentLoop` 注入。

## 一次回合的数据流

```mermaid
sequenceDiagram
    participant U as 用户
    participant L as AgentLoop
    participant P as TurnPolicy
    participant E as ChatEngine
    participant D as ToolDispatcher
    participant S as SessionService

    U->>L: process_input(session, input)
    L->>L: append user message + load/create TaskState
    L->>L: deadline = now + ai.turn_timeout_seconds（0=关闭）
    loop 由 TurnPolicy 决定是否继续
        L->>P: decide_next_iteration(快照：轮次/时钟/花费 + R6b 失败连胜与成功数)
        P-->>L: ITERATE / NUDGE([strategy-gate]，落盘) / FINISH（含「零进展」提前收尾）
        L->>L: compress(副本) + 拼 instructions + 注入易失尾部块
        Note over L: 尾部 = memory / task / hook / digest（R6a：仅当本轮历史真被改写时才有 digest）
        L->>E: step(messages, tools, instructions, on_delta)  ← 受剩余回合预算约束
        E-->>L: StepResult(text, tool_calls, finish_reason, usage)
        L->>L: append assistant
        alt 无工具调用
            L->>P: decide_after_step（todo 门禁 / 自然结束 / nudge 上限）
            P-->>L: NUDGE（注入 [task-gate]，落盘）或 FINISH
        else 有工具调用
            L->>D: dispatch_batch(calls, ToolContext, deadline)
            D->>D: gate → 审批 → 执行（有界并发，单调用 min(工具上限, 剩余回合预算)）
            D-->>L: DispatchOutcome 列表（含结构化拒绝）
            L->>L: append tool messages + 事件 + 落盘（长会话按步长节流）
        end
    end
    Note over L: 任一步到期 → TurnOutcome(TIMEOUT)，已完成文本与轨迹保留
    L->>L: R6a：写回合摘要环 + 未完成时生成 handoff（不改 text）
    L->>S: ensure_tool_pairing + save（含 turnCount / turnDigests）
    L-->>U: text or handoff
```

## 扩展点

| 想改什么 | 怎么做 |
|---|---|
| 换模型引擎 | 实现 `ChatEngine` 并在 `build_engine_from_config` 里返回它 |
| 新增工具 | 继承 `BaseTool`（实现 `execute(params, ctx)`），在 `build_tool_registry` 注册 |
| 改审批策略 | 实现 `ApprovalCallback`，在组合根传入 |
| 观察循环 | 实现 `LoopObserver`（`on_event`），挂到 `AgentLoop` 或 `process_input` |
| **加一个运行时守卫**（R4b） | 在 `core/guard.py` 实现 `check()`（返回 `GuardDecision`），加进 `GuardChain`；在 `cli._make_guards` 装配 |
| **挂钩子**（R4c） | 配置 `hooks.events`，写脚本（stdin/stdout JSON）或放 `<workspace>/.ai-coding/hooks/*.py`（进程内） |
| **改可观测**（R4a） | 自己实现 `LoopObserver` 并 `Braid` 进去；或改 `core/trace.py` 的落盘与脱敏策略 |
| **让模型主动提问**（R6c） | 实现 `UserPromptCallback`（`async ask`）并在组合根传给 `build_tool_registry(user_prompt=...)`；无人入口传 `None` 即不注册该工具 |
| **改回合摘要/交接的措辞与边界** | 改 `domain/turn_digest.py`（纯函数，无 IO）：`build_digest` 的一行内容、`render_working_memory` 的注入规则、`render_handoff` 的报告 |
| 接入外部工具集 | 用 `MCPManager` 的模式：把远端工具适配成 `BaseTool` 注册进同一个注册表 |
| 换压缩/记忆实现 | `AgentLoop` 的 `context` / `memory` 都是可注入的可选组件 |
| 改「什么时候继续/停止」的规则 | 改 `core/turn_policy.py`（纯单元，19 条决策表测试），不要动编排器 |
| 给某个工具加/改调用上限 | 覆盖该工具的 `max_duration_seconds`（`None` = 只受回合预算约束） |
| 新增一种「等外部世界」的 IO | 走 `infra/deadline.run_with_deadline`；子进程走 `infra/subprocess_runner` |

## 设计取舍

- **失败关闭**：未知工具与无回调的 WARN 默认拒绝；DENY 永不因回调而放行。审批回调自身抛异常
  也按拒绝处理。
- **组合根决定策略、机制层保持中立**：分发器对 WARN 的处理是「问回调、无回调即拒」，
  「非交互该自动批准」是 CLI 组合根的政策。
- **静默降级**：记忆、L4 摘要、MCP 连接、worktree 隔离等增强组件失败时回退而非中止会话；
  但**模型调用失败不降级**——那是主链路断裂，必须显式以 `ERROR` 结束并保留已完成轨迹。
- **易失 vs 持久**：每轮注入的任务状态块是视图（不落盘），todo 门禁提醒是指令（落盘）。
- **只改副本**：上下文压缩在历史副本上进行，绝不污染调用方状态。
- **结构化优先**：工具结果、拒绝、子智能体回包都带机器可读的 `data`，而不是只有一段文本。
- **任何等待都有上限（R2）**：模型步、工具调用、子智能体、MCP 连接、压缩/记忆都受
  `infra/deadline` 约束；到期是**放弃式取消**（不等对方响应取消），因此
  「工具卡住 ⇒ 回合永远不结束」这类故障不可能再出现。回合上限见 `ai.turn_timeout_seconds`。
- **进程执行只有一个出口（R2）**：`infra/subprocess_runner`（线程 + `killpg`），
  不再让业务代码各自 `create_subprocess_*`——那样会各自踩一次「取消要等管道 EOF」的坑。
