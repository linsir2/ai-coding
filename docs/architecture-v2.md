# ai-coding 完整架构图（v2 · 2026-10-21）

> **分支**: `feat/orchestration-cognition` | **轮次**: M0–M8 + R1–R12 全部完成
> **测试**: pytest 2024 passed / 2 skipped · ruff 0 · mypy 116 files clean

---

## 一、一句话总览

ai-coding 是一个**分层、引擎可替换的自持 ReAct 循环**对话式编码助手：领域契约、编排循环、工具分发、上下文压缩、记忆与会话全部自家实现；模型调用只经由一个薄薄的 `ChatEngine` 端口。前端为 TypeScript + OpenTUI，经 Unix socket IPC 与后端对话。

---

## 二、包结构与依赖方向

```
ai_coding/
├── cli.py                  # entrypoint：命令注册 + 组合根装配
├── domain/                 # 纯数据契约 dataclass，零业务依赖，最底层
│   ├── message.py          # ChatMessage, ToolCall, ToolResult, ToolOutput
│   ├── session.py          # SessionData (session_id/title/messages/task)
│   ├── loop.py             # StepResult, TurnOutcome, TurnStatus
│   ├── task.py             # TodoItem, TaskState (goal/items/render)
│   ├── subagent.py         # SubAgentConfig, SubAgentResult
│   ├── model_config.py     # ModelConfig (name/base_url/api_key/fallback)
│   ├── usage.py            # Usage (prompt/completion/total/cached)
│   ├── turn_digest.py      # TurnDigest (回合摘要环 + 收尾交接渲染)
│   ├── run.py              # Run, RunStep, RunOutcome
│   └── acceptance.py       # AcceptanceItem (verify陈述+shell证明)
├── config/                 # 配置模型(Pydantic) + YAML加载(camel→snake)
│   ├── models.py           # AppConfig, AIConfig, MemoryConfig, MCPConfig
│   └── loader.py           # ConfigLoader (load_app/load_mcp)
├── infra/                  # 标准库封装
│   ├── json_utils.py       # JSON 读写
│   ├── file_utils.py       # 文件操作
│   ├── time_utils.py       # 时间工具
│   ├── logger_setup.py     # 日志
│   ├── deadline.py         # 有界等待(墙钟超时)
│   ├── subprocess_runner.py # 进程执行(killpg + reap)
│   └── vcs.py              # Git pin/stash/create
├── security/               # 安全层
│   ├── permission_gate.py  # PermissionGate(ALLOW/WARN/DENY表+bash红线)
│   ├── sandbox.py          # Sandbox(工作区路径限制)
│   ├── approval.py         # ApprovalCallback(Result: YES/ALWAYS/NO/STOP)
│   ├── file_state_tracker.py # 过期读保护(mtime+size快照)
│   ├── grants.py           # 授权持久化(always/once)
│   ├── bash_redlines.py    # bash硬拒绝模式(rm -rf /等)
│   └── user_prompt.py      # UserPromptCallback(ask_user通道)
├── tools/                  # 工具契约 + 实现 + 注册表 + schema
│   ├── base.py             # BaseTool(name/description/schema/execute/precheck)
│   ├── registry.py         # ToolRegistry(注册/查找/parallel_safe分组)
│   ├── builder.py          # build_tool_registry(装配7个内置+subAgent)
│   ├── schema.py           # 工具schema转换
│   ├── bash_tool.py        # bash执行(shell命令,超时,截断)
│   ├── read_tool.py        # 行号读取(offset/limit,10MB上限)
│   ├── write_tool.py       # 写入文件(过期读保护)
│   ├── edit_tool.py        # 精确字符串替换(过期读保护)
│   ├── glob_tool.py        # 通配符匹配(过滤符号链接逃逸)
│   ├── todo_write_tool.py  # 计划模式待办维护(pending/in_progress/completed/blocked/dropped)
│   ├── subagent_tool.py    # 委派子任务(worktree隔离)
│   ├── skill_tool.py       # 读取技能目录SKILL.md
│   ├── memory_search_tool.py # 记忆检索(BM25+两道门禁)
│   ├── memory_write_tool.py  # 记忆写入(evidence=model)
│   ├── checkpoint_tool.py  # checkpoint_write(模型侧摘要)
│   ├── history_search_tool.py # 历史回忆(压缩后取回)
│   ├── mcp_call.py         # MCP远端工具统一入口
│   ├── job_tools.py        # 后台子任务查询(job_list/job_output/job_kill)
│   ├── goal_tools.py       # start_goal/update_goal
│   ├── present_plan_tool.py  # present_plan(plan+items提交)
│   ├── finish_research_tool.py # finish_research(研究出口)
│   ├── ask_user_tool.py    # ask_user主动提问
│   ├── webfetch_tool.py    # HTTP获取
│   ├── websearch_tool.py   # 网络搜索
│   ├── grep_tool.py        # 内容搜索
│   ├── htmltext.py         # HTML转文本
│   └── skill_review.py     # 技能审核
├── ai/                     # 引擎端口 + 实现 + 映射 + 提示装配
│   ├── engine.py           # ChatEngine(ABC): step(aclose)
│   ├── openai_engine.py    # OpenAIChatEngine(chat.completions stream)
│   ├── fallback_engine.py  # FallbackEngine(多模型降级链)
│   ├── factory.py          # build_engine_from_config
│   ├── mapping.py          # StepResult ↔ API响应映射
│   ├── toolcalls.py        # 增量tool_calls合并(纯函数)
│   ├── llm_helpers.py      # LLM辅助函数(summary_shrink等)
│   └── prompt.py           # 提示词模板(plan-mode段/方法论段)
├── core/                   # 编排与增强(唯一同时编排各层的层)
│   ├── agent_loop.py       # AgentLoop: process_input(自持ReAct循环)
│   │   ├── _run_turn()     # 回合本体: snapshot → begin → while(policy → assemble → step → tools)
│   │   ├── _take_steering() # 吸收中途输入(steering队列)
│   │   ├── _run_tools()    # dispatch_batch → write_back → checkpoint
│   │   └── _finish_turn()  # closer.close → harvest → hooks
│   ├── turn_books.py       # TurnBooks(账本: iteration/latency/failed_steps)
│   ├── turn_hooks.py       # HookRunner(turn.start/step.before/tool.before/after/end)
│   ├── turn_report.py      # TurnReporter(对外陈述/usage)
│   ├── request_assembly.py # assembler.assemble(压缩+instructions+volatile tail)
│   ├── request_builder.py  # 请求构建(易失尾部块顺序)
│   ├── context_compression.py # ContextManager(4层压缩: L1/L2/L3/L4)
│   ├── compaction.py       # Compactor(/compact手动+溢出救援+二次合并)
│   ├── memory_turn.py      # 回合记忆注入(TurnMemory)
│   ├── steering.py         # SteeringQueue(回合中改方向)
│   ├── acceptance.py       # 验收门(verify shell命令,失败nudge)
│   ├── guard.py            # GuardChain(duplicate_call/bash_policy/path_escape/secret)
│   ├── budget.py           # Token账本(prompt/completion/total)
│   ├── observers.py        # LoopObserver(SafeObserver+事件扇出)
│   ├── events.py           # 事件家族(IterationStarted/ToolStarted/Finished/etc.)
│   ├── trace.py            # TraceRecorder(JSONL轨迹+脱敏+summary.json)
│   ├── snapshots.py        # SnapshotStore(git refs)
│   ├── undo_store.py       # Undo/Redo消息半边+文件半边
│   ├── sop_router.py       # SOP Router(技能路由: keyword/tool/task_type/failure_pattern)
│   ├── jobs.py             # 后台Job管理(job_list/output/kill)
│   └── mcp.py              # MCPManager(open_all/list_tools/aclose)
├── service/                # 会话持久化
│   └── session_service.py  # SessionService(create/save/load/list/resolve/delete)
├── memory/                 # 记忆三层操作与持久化
│   ├── note.py             # MemoryNote(frozen dataclass: id/text/kind/scope/scene/importance)
│   ├── store.py            # MemoryStore(JSON单级磁盘索引+驱逐算法)
│   ├── scoped_store.py     # ScopedStore(双级作用域: workspace/user)
│   ├── relevance.py        # BM25检索+三重门控(threshold/min_relevance/relative_band)
│   ├── embeddings.py       # EmbeddingIndex(可选语义向量层,惰性嵌入)
│   ├── schema.py           # HarvestCandidate(提取器输出契约)
│   └── service.py          # MemoryService(recall/harvest/consolidate)
├── skills/                 # 技能目录注册表
│   ├── catalog.py          # build_catalog(8000字符预算)
│   ├── registry.py         # SkillRegistry(SKILL.md扫描)
│   ├── install.py          # 技能安装(owner/repo或本地目录)
│   ├── policy.py           # 安装策略(拒绝符号链接/无description)
│   ├── render.py           # 技能渲染(frontmatter version/status回显)
│   └── __init__.py
├── app/                    # 命令分发与行式输出
│   ├── slash.py            # parse_slash(已知/未知/普通文本分类)
│   ├── commands.py         # handle_input(CONTINUE/QUIT/NEW_SESSION)
│   ├── console.py          # DialogueConsole(文本输出)
│   └── trace_text.py       # 轨迹文本渲染
└── server/                 # 后端IPC(Unix socket + bun前端)
    ├── world.py            # build_world(运行时组合根)
    ├── service.py          # IpcService(JSON-RPC over Unix socket)
    ├── handlers.py         # make_handlers(session_prompt/resume/initialize/etc.)
    ├── launcher.py         # run_tui(spawn bun前端)
    ├── bridge.py           # EventBridge(LoopEvent→wire事件→IPC notify)
    └── display.py          # 显示工具
```

**依赖方向单向**:
```
domain ← config/infra ← security/tools/ai ← core ← service/memory/skills/app ← server
```

关键约束:
- `domain` 不被任何层反向 import
- `ai` 只依赖 `domain`(不依赖 `tools`——工具 schema 由调用方以 `list[dict]` 传入)
- `core` 是唯一同时编排各层的层
- `cli.py` 提供装配原语, `server/world.py::build_world` 是运行时组合根

---

## 三、一次回合的数据流

```
用户输入 → TUI(IPC session.prompt) → server/handlers.py:_launch_turn
  → AgentLoop.process_input(_turn_lock串行)
    → _capture_snapshot(git pin pre-turn)
    → _begin_turn(user消息入history, turn_count++, guards.reset)
    → while True:
        ├─ policy.decide_next_iteration(FINISH/NUDGE/CONTINUE纯函数决策)
        ├─ FINISH: 验收契约_verify_acceptance → break
        ├─ NUDGE: synthetic user消息(task-gate/fatigue/strategy)
        ├─ _take_steering(吸收中途输入)
        ├─ assembler.assemble(压缩+instructions+volatile tail)
        │   ├─ compactor.compress(4层: L1 snip/L2 elide/L3 persist/L4 summarize)
        │   ├─ instructions(项目指令+skill目录)
        │   └─ volatile tail(memory→mcp_catalog→task→skills→hook→digest→budget)
        ├─ _model_step(engine.step) → assistant落history
        │   ├─ 无工具 → decide_after_step → FINISH or nudge+checkpoint
        │   └─ 有工具 → _run_tools(dispatch_batch→gate→审批→执行)
        │       ├─ parallel_safe+ALLOW → asyncio.gather并发
        │       ├─ 否则严格串行
        │       ├─ WARN → ApprovalCallback(TUI弹模态/y/N/s)
        │       ├─ DENY → 硬拒绝(bash红线)
        │       └─ write_back(tool消息配对)+TaskUpdated+checkpoint节流落盘
        └─ 异常漏斗(CancelledError→persist; DeadlineExpired→failed_outcome)
    → _finish_turn(skill outcome→closer.close→harvest→hooks→返回text/handoff)
  
事件流: LoopEvent → SafeObserver → EventBridge → IPC notify → 前端reducer
```

---

## 四、记忆系统架构

```
MemoryService
├── recall_notes(query, k, kind, scope, scene, since_days, evidence)
│   ├─ _candidates() → 预过滤(kinds/evidence/scope/scene/since_days)
│   ├─ rank_notes_detailed() → BM25 + 可选余弦融合
│   │   ├─ BM25(Okapi公式: k1=1.2, b=0.75, CJK bigram)
│   │   └─ EmbeddingIndex(可选: text-embedding-v4, 0.5·BM25⊕0.5·余弦)
│   ├─ 三重门控: threshold≥0.25 AND min_relevance≥0.25 AND relative_band≥0.55
│   ├─ touch_notes(ids) → 命中即使用(hits+last_used写盘)
│   └─ 未命中探针: terms_known/unknown, near_misses, index概览
├── harvest(session_messages) → 提取候选 → reconcile(ADD/UPDATE/SUPERSEDE/NOOP)
│   ├─ 同义合并(导语剥离后相似度≥0.80)
│   ├─ 形状门(is_plausible_note_text: 拒绝JSON碎片)
│   └─ 易变状态过滤(_is_volatile_note)
├── consolidate() → 合并近似重复(同kind+同scene) + 按hits提升importance + evict
│   └─ value = importance + 0.5*min(hits,4) + 0.5*freshness(半衰期30天) + stake
└── write_note(note) → 模型主动写入(evidence=model)

存储: .memory (v2 JSON, 原子写tmp+os.replace)
作用域: ScopedStore(workspace级.project优先 + user级可选)
```

---

## 五、压缩机制(4层)

| 层 | 触发 | 动作 | 产物 |
|---|------|------|------|
| L3 | 单结果 > per_result_persist_bytes(30KB) | 持久化到 transcripts/persisted/ | `<persisted-output>` 占位 |
| L1 | 超 max_messages(400) | 裁剪中间段, 保留 head/tail | `[snipped N messages]` |
| L2 | 早于 keep_recent_tool_results(3) | 换为可回收信封(正文落盘elided/) | 头尾窗口(l2_window_chars=200, budget=4000) |
| L4 | 超上下文预算(max_context_tokens=48000) | LLM摘要压缩(3次失败熔断) | `<previous-checkpoint>` 合并旧摘要 |

**兜底**: 硬裁剪 + sanitize_pairs 保证严格配对
**原则**: 所有阶段在消息列表浅拷贝上进行, 绝不修改调用方历史

---

## 六、安全体系

```
PermissionGate(ALLOW/WARN/DENY表)
├── ALLOW: read, glob, todo_write, subAgent, skill, memory_search, memory_write
├── WARN: write, edit, bash → 走ApprovalCallback(TUI弹模态)
├── DENY: bash红线(rm -rf /, sudo, shutdown, chmod 777, 凭据路径)
└── 未知工具 → 保守WARN

Sandbox: 路径解析限制在工作区内(..,绝对路径,符号链接逃逸抛SandboxViolation)
FileStateTracker: 过期读保护(NEVER_READ/STALE阻止写入)
Grants: always/once授权持久化(extra_workspace_roots声明式常驻授权)
Guards(duplicate_call/bash_policy/path_escape/secret): 带状态运行时判定
```

---

## 七、MCP集成

```
MCPManager
├── open_all() → supervisor每server独立连接(并发≤4,永久backoff重连)
├── available() → 单一来源决定可调用的远端工具
├── catalog文本(每条≤120字符,总量≤8000字符,每步重建挂volatile tail)
├── mcp_call(server, tool, input) → 统一LLM入口
├── OAuth(remote类型: DCR/PKCE/token交换,token存~/.local/share/ai-coding/mcp-auth.json)
├── /mcp slash面板(引导式连接向导)
└── 单个server失败静默跳过(增强组件不阻塞会话)
```

---

## 八、前端架构(TypeScript + OpenTUI + Bun)

```
ai_coding/frontend/src/
├── component/          # 组件: session-tabs, transcript, prompt(input), permission, ask, modals
├── dialog/             # 对话框: sessions, skills, status, help, mcp
├── state/              # 状态管理: session reducer(纯函数)
├── ipc/                # IPC通信: JSON-RPC over Unix socket
├── keymap.ts           # 键位表(ctrl+x=q退出,esc两段中断)
├── util/               # 工具: slash.ts(补全过滤), permission.ts(permissionPayload)
└── types/              # 类型定义
```

**区域**: 顶部session-tabs / 中部transcript(流式Markdown+工具事件块) / 底部input / 弹层(审批/命令面板)

---

## 九、里程碑演进时间线

| 阶段 | 主题 | 关键交付 |
|------|------|----------|
| **M0** | 工程骨架 | domain契约, config, CLI壳, infra |
| **M1** | AI服务 | ChatEngine, AgentLoop主循环, 会话持久化 |
| **M2** | 工具+安全 | 8个内置工具, PermissionGate, 计划模式待办, fallback_models降级 |
| **M3** | 上下文+记忆 | 4层压缩, 记忆层(M3重写), 技能目录, Prompt装配 |
| **M4** | 子代理 | SubAgentRunner, worktree隔离, MCP remote+OAuth |
| **M5** | MCP集成 | MCPManager直接连stdio, REPL/富文本UI |
| **M6** | 收尾迁移 | 收尾交接(handoff), 全量迁移测试 |
| **R1** | 编排重构 | 自持循环, 移除SDK, todo门禁, 结构化回包 |
| **R2** | 可靠性 | 任何情况都能收尾, 硬超时, 进程执行统一 |
| **R3** | 记忆重做 | BM25检索, 价值驱动驱逐, 10条案例实测 |
| **R4** | 可观测/门卫/hook | TraceRecorder, GuardChain, hook双形态(command/python) |
| **R5** | 循环瘦身 | process_input 229→153行, 纯结构重构 |
| **R6** | 人在环 | 回合摘要环, 失败升级三档, ask_user, 记忆证据 |
| **R7** | 循环收口 | L2头尾窗口, MCP工具面阈值告警 |
| **R7e** | 模型侧能力 | checkpoint_write, steering队列, memory_write |
| **R8** | 流式+取消 | 模型checkpoint, evidence过滤, 工具流式chunk, Esc取消当批 |
| **R9** | 依赖门禁 | todo_write依赖硬校验, 轨迹三个只读出口(overview/HTML/OTLP) |
| **R10** | 检索可测量 | BM25排序+两道门禁+召回解释失败+命中即使用 |
| **R11** | 记忆增强 | 查询清洗(R11a), 召回预算(R11b), scene分组+位置引用(R11c) |
| **R12** | Textual UI | 全屏Textual界面(v1会话闭环), 事件→块→transcript |

---

## 十、扩展点速查

| 想改什么 | 怎么做 |
|----------|--------|
| 换模型引擎 | 实现 `ChatEngine` + `build_engine_from_config` 返回 |
| 新增工具 | 继承 `BaseTool` + `build_tool_registry` 注册 |
| 改审批策略 | 实现 `ApprovalCallback` + `build_world` 传入 |
| 观察循环 | 实现 `LoopObserver` + braid进loop |
| 加运行时守卫 | 实现 `Guard.check()` + `GuardChain` + `_make_guards` |
| 挂钩子 | 配置 `hooks.events` + python/command双形态脚本 |
| 让模型主动提问 | 实现 `UserPromptCallback` + `build_world` 传user_prompt |
| 改回合摘要/交接 | 改 `domain/turn_digest.py`(纯函数) |
| 接入外部工具集 | MCPManager模式: 适配成 `BaseTool` 注册 |
| 换压缩/记忆实现 | 注入到 `AgentLoop(context/memory)` |
| 改停止规则 | 改 `core/turn_policy.py`(纯单元,19条决策表测试) |
| 给工具加调用上限 | 覆盖 `max_duration_seconds` |

---

## 十一、对照竞品差异

| 维度 | 我们 | opencode | dsh(Cordis) |
|------|------|----------|-------------|
| 循环控制 | **纯函数TurnPolicy**(可表格测试) | needsContinuation位 | 工具未完→null |
| 持久化 | 整文件快照+openTurn marker | SQLite行+事件投影 | durable inbox+claim |
| 审批崩溃恢复 | ✅ pending/<id>.json marker | ❌ ephemeral事件 | ❌ 无 |
| 工具并行 | 最保守(全部safe+ALLOW才gather) | 每call一个fiber全并发 | runGroup池(上限10) |
| 压缩策略 | 4层(L1/L2/L3/L4) | compaction.ts多层 | range-surface(已否决) |
| 记忆检索 | BM25+可选语义档(默认关) | 简单关键词 | 多层记忆(L0-L4) |
| MCP | 官方mcp包stdio直连 | 自有实现 | 未明确 |
| 前端 | TypeScript+OpenTUI+Bun | Python+Textual | Rust+WebAssembly |

---

## 十二、当前研究出口(未实施)

| 编号 | 主题 | 状态 |
|------|------|------|
| C-1 | 模型可见的历史回忆 | ✅ 已完成(history_search) |
| C-2 | 静态方法论分区块化 | ✅ 已完成(METHODS_SECTION) |
| C-3 | 审批拒绝自由文本反馈 | ✅ 已完成(no::说明语法) |
| C-3b | 模型侧停后台子代理 | 待做(subagent_kill) |
| F1 | Skill Market动态定价 | 设计完成, 实施待定 |
| F2 | Fatigue System | 设计完成, 实施待定 |
| F3 | Cross-Domain Immune | 设计完成, 实施待定 |

---

*文档版本: v2 · 生成时间: 2026-10-21 · 基于 docs/ 下 ~100 篇分析文档综合整理*
