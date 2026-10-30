# B0 方案：build 并行调研 `parallel_research`（已实现）

> 状态：**已实现、已评审、已跑真机**（2026-10-30）。方案按「现有设计 → 影响范围 → 契约 →
> 数据流 → 逐文件改动 → RIPR 用例 → 成功后的样子 → 风险」写清；实施后的修订与评审结论在 §12。
> C2 的评审结论放在 §0。

## 0. 先评审 C2（已落地，含真机结论）

**真机**：C2 落地时本机端点 `http://127.0.0.1:8899/...` 不可达，因此 C2 的验证是真实模块 +
脚本化 engine 的测试。B0 这一轮改用 `config.local.yaml` 的 dashscope 端点，**跑成了真实模型
回合**（见 §12）。

**评审发现并已修的 3 个问题**（均在 `core/wave_runner.py`）：

1. **P0：旧收据会吞掉新完成。** `StepDelivery.step_id` 是条目 id，不是尝试 id。第一次
   run 留下 `stalled` 收据后，第二次 run 完成同一条目时，旧的 `_receipted` 集合让
   `_record` 直接 return，**新完成没有收据**。已改为「本次 run 去重」的
   `_recorded_this_run`；旧收据只用于断点补验。
2. **P1：断点补验会写父会话气泡。** 补验属于上一轮的事，不该再写 synthetic 气泡；
   已改为只在 `plan.receipt` 事件上落一行摘要。
3. **P2：死常量** `_MAX_NO_PROGRESS`（wave 级无进展停止已移除，避免一个失败条目
   误停整波）；已删除。

**未改但已知**：

- 波墙钟只在回合之间检查；单个回合仍受全局 `turn_timeout_seconds`（1800s）约束，
  可能超出 `plan_wave_wall_seconds` 一个回合。要精确需要「按会话的 turn deadline」，
  属于 AgentLoop 改动，C2 不做。
- `task.plan_tokens_used` 在波结束才累加；崩溃会丢掉当前波的账。
- 前端侧栏仍显示 goal 的 `tokensUsed`，计划 token 账没接；C3 再接。

**门禁**：后端全量 exit 0；`ruff`/`mypy` 通过；前端 `tsc` 通过、576 tests pass；
新增 `test_wave_runner.py` 13 例。

## 1. B0 要解决什么

build 里模型经常有 2–4 个**互不依赖**的调研问题（入口在哪、测试怎么跑、接口谁在用、
文档怎么说）。今天只能：
- 串行发多次 `read/grep`，每轮一次模型调用；
- 或让模型在一次 batch 里发多个 `subagent` 调用——能并发，但父会话要为每个子代理
  重复上下文、结果要自己拼，且没有统一的结构化输出。

B0 提供一个**只读 fan-out 工具**：一次调用、2–4 个独立问题、`explore` 子代理并发、
统一 JSON 输出并合并。**不做写并行**。

## 2. 现有设计（必须复用，不另造）

| 现有件 | 位置 | 复用什么 | 不改什么 |
|---|---|---|---|
| 子代理执行器 | `core/subagent.py:180-210`（`run`）、`:212-250`（隔离）、`:569`（上下文模式） | 直接调用；每子代理自己的 AgentLoop、worktree、超时、usage | 不写第二套子代理 |
| 子代理工具 | `tools/subagent_tool.py:119-292` | 父上下文构造（`ParentContext`，:256-263）、`EmitObserver`、`runner` 只读属性（:340-343） | 不改它的行为 |
| 只读子代理 | `agents/registry.py:45-58`（`_EXPLORE_ALLOWED`） | `agent="explore"`：read/glob/grep/web*/skill/memory/history，**无 write/bash** | 不加新代理 |
| 工具基类 / 结果 | `tools/base.py`、`domain/message.py`（`ToolResult/Artifact`） | 标准 `ToolResult` + `data` + `output` | 不造新结果类型 |
| 并发限流 | `core/subagent.py:206`（信号量） | runner 的 `max_concurrent_subagents` | 不再加 semaphore |
| 权限门 | `security/permission_gate.py:327-398`（ALLOW 清单） | 加一个 ALLOW 项，避免每调一次弹审批 | 不改门逻辑 |
| 计划模式策略 | `agents/registry.py:60-80`（`_PLAN_SOFT_DENY`，禁 `subagent`） | **r25 起不再禁 `parallel_research`**：规划阶段也需要调研（见 §13）；`subagent` 仍禁 | 不改 plan 的只读语义 |
| 工具卡 | `frontend/src/util/toolmeta.ts:16-35` | 加一个中文动词 | 不改渲染 |
| 并行只读工具批次 | `core/dispatch.py:347-370` | 工具 `parallel_safe=True`，同批可并发 | 不改 dispatcher |

**为什么不复用 `SubAgentTool` 直接“发多个调用”**：模型一次 batch 发 N 个 `subagent`
确实能并发，但：
- 每个子代理拿到的是父请求视图（`full`），N 份上下文 = N× token；
- 结果是 N 条独立 tool message，父模型还要再做一次汇总；
- 没有统一的问题 id / key_files / 失败隔离。
B0 的价值是把这三件事收进一次工具调用，不是重新实现并发。

## 3. 架构与数据流

```text
模型一步
  └─ tool_call: parallel_research{questions:[q1..qN], context}
      └─ ToolDispatcher（ALLOW + parallel_safe；build 与 plan 都可达）
          └─ ParallelResearchTool.execute
              ├─ 从 ToolContext 取 messages/permissions → ParentContext（系统提示由子代理自建）
              └─ ResearchFanOut.run(questions, parent, observer, parent_span)
                  ├─ for q in questions:
                  │     SubAgentRunner.run(
                  │        task=brief(q, shared_context),
                  │        agent="explore",           ← 只读白名单
                  │        isolate=False,            ← 读当前工作区（只读无需 worktree）
                  │        context_mode="task_only", ← 每个子代理只拿 brief，不拿父历史
                  │        output_schema=RESEARCH_SCHEMA,
                  │        parent=ParentContext, observer=EmitObserver, parent_span=call_id)
                  │     （并发度由 runner 的 max_concurrent_subagents 信号量负责）
                  └─ 合并：按 id 排序、key_files 去重、summary 截断、usage 求和
              └─ ToolResult(output=合并 markdown, data={results, usage}, artifacts=())
          └─ tool message 回到模型；下一步模型基于合并结果继续
```

**数据归属**：父会话只新增一条 tool message；子代理的 transcript / worktree 全部丢弃；
落盘的是 `data.results`（结构化）+ `output`（可读）。trace 通过 `EmitObserver`
把每个子代理的 span 挂在本次 tool call 下。

## 4. 契约

### 4.1 工具 schema（`parallel_research`）

```json
{
  "type": "object",
  "properties": {
    "questions": {
      "type": "array", "minItems": 2, "maxItems": 4,
      "items": {
        "type": "object",
        "properties": {
          "id": {"type": "string", "description": "短 id，合并时用它排序，可省略"},
          "question": {"type": "string"},
          "scope": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["question"]
      }
    },
    "context": {"type": "string", "description": "共享背景/约束，可省略"}
  },
  "required": ["questions"]
}
```

- 2–4 条：1 条请直接 `subagent`；>4 拒绝（并发与 token 都不划算）。
- `scope` 只是提示（目录/文件/模块），不改变只读权限；单项截到 200 字，最多 10 项。
- `context` ≤ 1000 字；`question` ≤ 800 字；超长直接拒绝（不静默截断）。
- `id` 可省略（默认 `q1..qN`，若与已有 id 冲突则加 `-2` 后缀，不静默覆盖）；重复的显式 id 拒绝。
- 错误码：`too_few_questions` / `too_many_questions` / `bad_questions` /
  `duplicate_question_id` / `context_too_long` / `all_children_failed`。
  （`runner_unavailable` 不实现：工具只在有 runner 的主栈注册，这是一个不可能发生的分支。）

**工具属性**：`parallel_safe=True`（同批多次调用由 runner 信号量兜底）；
`mutates_world=False`；`max_duration_seconds = ceil(4 / max_concurrent) * (timeout + 15)`
（一条 lane 一个子代理预算，另加 15s 覆盖 worktree/收尾；不能只给一轮，否则 tool 先被 dispatcher
取消、已完成的子代理结果全丢）。

### 4.2 子代理输出 schema（`RESEARCH_SCHEMA`）

```json
{
  "type": "object",
  "properties": {
    "summary": {"type": "string"},
    "key_files": {"type": "array", "items": {"type": "string"}},
    "findings": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["summary", "key_files"]
}
```

失败/无结构时 `facts=None`，原始 `conclusion` 仍保留（`SubagentResult` 已有）。
`key_files` 必须是仓库相对路径；**不采 `artifacts`**（artifacts 可能是 worktree 内路径）。

**r25 起并入冻结契约**（`docs/subagent-handoff-contract.md`）：
- 子代理收到的任务文本由 `SubagentBrief.render()` 生成（`[TASK]/[CONTEXT]/[SCOPE]/[CONSTRAINTS]`），
  不再是本模块手拼散文；非法 brief 在 `_run_one` 里直接返回 `error` packet。
- schema 调用的返回指令只有一条（JSON），不再叠加中文不确定协议；`confidence`/`notes`
  由 schema 声明为可选键、`_packet` 优先从 facts 读。
- `extract_json_facts` 现在执行声明的 `type`/`items.type`：`RESEARCH_SCHEMA` 的
  `summary: string`、`key_files: array<string>` 真正生效；校验失败会带进
  `ResearchPacket.schema_valid/schema_errors` 并显示在合并报告里。

### 4.3 结果

```python
data = {
  "results": [
    {"id","status","summary","key_files","findings","error","truncated","usage"}
  ],
  "usage": {"total_tokens": int, ...} | None,
  "questions": int,
  "completed": int,
}
```

- 至少一条 `completed` 或 `max_iterations`（有部分结论）→ `success=True`，其余失败条目原样返回
  （与 `SubAgentTool` 的 `SUCCESS_STATUSES` 同一判据）。
- 全部失败 → `success=False`，`error="all_children_failed"`。
- 单条失败不改整体成功；`output` 用 `## q1 · ok` / `## q2 · error` 分段。
- `data.usage` 是所有 packet usage 的**按键求和**（prompt/completion/total 等），
  全都没有报告时为 `None`；只报告，不进父账本。
- 上限：每条 summary 600 字、key_files 8 个、findings 5 条；合并后总输出 ≤ 8000 字符；
  任何一层被裁都置 `truncated=True`，报告里标「（已截断）」——不能静默丢弃。
- `max_iterations` 这类「成功但被截停」的条目：`error` 置空，不把 `exit_reason` 渲染成
  成功条目下的「错误」。

### 4.5 成本（用户已选：只报告，不进父 turn 预算）

- 每个子代理独立 usage；工具把 `usage` 求和放进 `data.usage`，trace 里也有 scoped 子 span。
- **不改 `TurnBooks.tokens_spent` / token gate**：子代理花费不参与父回合的硬预算。
  原因：一次 fan-out 可能让父回合在拿到结果后的下一轮被 token gate 截断，模型来不及综合；
  用户明确选择不把子代理纳入 token。
- 已有 `subagent` 的行为完全不变（它的 usage 仍只在 data/表格里）。
- **代价**：花费不会自动计入预算，需要看 `data.usage` / `/budget` / trace。工具描述里写明
  「costly, 2–4 questions only」。
- 失败子代理若报告 partial usage，也照实放进 data（钱已经花了）。

### 4.6 只读与权限

- `agent="explore"`：写/编辑/bash 被规则拒。
- **`readonly=True` 硬开关**：项目 agent 可以用同名文件覆盖内置 `explore`（nearer scope wins
  whole）。若覆盖后允许 write/bash，只靠 agent id 的「只读」就名存实亡。fan-out 给
  `SubAgentRunner.run(..., readonly=True)`，子会话 `session.readonly=True`；工具面会剔除
  mutating 工具，dispatch gate 也会拒。这样三层：explore 规则 + readonly 会话 + dispatch 结构拒绝。
- **不建 worktree（实施后修订，用户拍板）**：只读会话已经让写根本不会发生，而临时 worktree 是
  HEAD 快照——真机上子代理把未跟踪的 `wave_runner.py` 报成「不存在」。所以 fan-out 子代理
  `isolate=False`，直接读当前工作区；真机只读探针验证其工具面只有 `glob/grep/read`。
- `parallel_research` 加入 `_allow_tools`（否则 unknown tool → WARN → 每次弹审批）。
- **不**加入 plan agent 的 `_PLAN_SOFT_DENY`（r25；见 §13）：规划阶段正是最需要调研的地方。
  `subagent` 仍被拒——它能指向有写工具的 agent，会把 plan 会话的只读姿态绕开。
- `explore` 白名单不含 `parallel_research`，子代理不能递归 fan-out。

## 5. 逐文件改动（最小集）

| # | 文件 | 改动 | 为什么必须 |
|---|---|---|---|
| 1 | `ai_coding/core/subagent.py` | `run()`/`_run_isolated()`/`_run_in()` 增加 `context_mode: str | None = None` 与 `readonly: bool = False`；子会话 `readonly=` 落到 `SessionData` | fan-out 必须 `task_only` + 真只读 |
| 2 | `ai_coding/core/research_fanout.py`（新） | `ResearchQuestion`、`ResearchPacket`、`RESEARCH_SCHEMA`、`ResearchFanOut.run`、合并与截断 | 一个可单测的纯编排层；工具只做参数/结果映射 |
| 3 | `ai_coding/tools/parallel_research_tool.py`（新） | `BaseTool`：name/description/schema；`execute` 构造 `ParentContext`、调 fanout、映射 `ToolResult` | 模型入口 |
| 4 | `ai_coding/tools/builder.py` | 在 `include_subagent` 分支注册 `ParallelResearchTool(runner)`（与 `SubAgentTool` 共用同一个 runner；子栈不注册） | 不重复构造 runner |
| 5 | `ai_coding/domain/subagent.py` | 新增 `SUCCESS_STATUSES`（`{completed, max_iterations}`） | 去掉 `subagent_tool` / fan-out / 工具三处重复定义 |
| 6 | `ai_coding/security/permission_gate.py` | `_allow_tools` 加 `parallel_research` | 不弹审批 |
| 7 | `ai_coding/agents/registry.py` | r25：**从** `_PLAN_SOFT_DENY` 移除 `parallel_research`（`subagent` 保留） | 规划阶段可只读调研 |
| 8 | `ai_coding/frontend/src/util/toolmeta.ts` | `TOOL_VERBS` 加 `parallel_research: "并行调研"` | 工具卡可读 |
| 9 | `tests/test_subagent_context_mode.py`（**已存在**） | **追加** per-call `task_only` / 未知模式回落 / readonly 工具面用例，不改旧断言 | RIPR 红相 |
| 10 | `tests/test_research_fanout.py`（新） | 假 runner：并发、部分失败、合并、上限、usage | RIPR 主体 |
| 11 | `tests/test_parallel_research_tool.py`（新） | schema、参数校验、ParentContext、ToolResult、权限、时长上限 | 工具契约 |
| 12 | `docs/plan-orchestration-c-review.md` | 记录 B0 落地状态 | 文档同步 |

**不新增**：新 agent、新 worktree 管理器、新 semaphore、新事件类型、新前端组件。

### 5.1 关键签名（实现时按这个写，不即兴加参数）

```python
# core/subagent.py
async def run(self, task: str, name: str = "sub-agent", *,
              worktree_manager=None, isolate=True, parent=None,
              parent_span="", observer=None, output_schema=None,
              agent=None, context_mode: str | None = None,
              readonly: bool = False) -> SubagentResult: ...

# core/research_fanout.py
async def run(self, questions: list[ResearchQuestion], *,
              parent: ParentContext | None,
              observer: Any = None,
              parent_span: str = "") -> list[ResearchPacket]: ...

# tools/parallel_research_tool.py
async def execute(self, params, ctx=None) -> ToolResult: ...
```

### 5.2 自检（可能翻车的点，先写下来）

1. **`task_only` 的 brief 不够**：子代理看不到父会话刚读的文件。缓解：`context` 字段 +
   `scope`；不把父历史复制 N 份是有意的成本选择。若实测质量差，改成 `full` 只需改一个
   参数（但成本 ×N）。
2. **子代理只读但跑在真实工作区**：`isolate=False`，所以读到的就是用户当前的文件（含未提交）；
   写入由 readonly 会话 + 工具面 + dispatch gate 三层挡住，不能把「只读」寄托在 worktree 上。
3. **子代理返回坏 JSON**：`facts=None`，原始 conclusion 仍保留；合并只用 `summary` 兜底。
4. **全部超时**：`success=False`；父模型得到明确失败，不会假装有结果。
5. **父账本看不到子代理 usage**：已知缺口；`data.usage` 与 trace 可见，但 turn budget 不变。
6. **计划模式可见但被拒**：与 `subagent` 同形；模型会看到工具却收到 deny，这是现有策略的
   一致行为，不是新洞。
7. **并发峰值**：2–4 个子代理 × 每个 AgentLoop；`max_concurrent_subagents` 是唯一闸门，
   不能在这个工具里再开一条并发路径。工具自己的 `max_duration_seconds` 按 lane 数算
   （`ceil(4/并发数) * (timeout + 15)`），否则 dispatcher 会先取消调用。

## 6. 成功后的样子（先设想，再倒推细节）

- 模型问 3 个独立问题 → 一次 `parallel_research` 调用；
- 工具卡显示「并行调研 · 3 个问题」，可展开三条结果；
- 3 个子代理并发（不超过 6），各自只拿 brief，不拿父历史；
- 一个失败时，另两个结果照常返回，模型能看到 `error` 段；
- 80 字符目录 + 3×600 字摘要，落在 8k 内；
- trace 中 3 个 `subagent:research:qX` span 挂在同一个 tool call 下；
- 没有任何文件被写；plan 模式下同样可调用（只读子代理）；
- 父回合 token 账不包含子代理，但 `data.usage` 与 trace 能看到真实花费。

## 7. RIPR（红 → 实现 → 证明 → 重构）

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 先写 3 个测试文件，全部 import/属性失败 | `pytest` 红；错误指向缺失模块/参数 |
| **I** | §5 的 8 个产品文件 | 新测试转绿 |
| **P** | 三个测试文件 | 全绿 |
| **P** | 全量 `pytest` / `ruff` / `mypy` / `tsc` / `bun test` | 0 failed / 0 issue |
| **P** | **突变 A**：`ResearchFanOut` 去掉 `context_mode="task_only"` | `test_research_fanout.py` 红（KeyError） |
| **P** | **突变 B**：`ResearchFanOut` 去掉 `readonly=True` | `test_research_fanout.py` 红（KeyError） |
| **P** | **突变 C**：从 `_allow_tools` 删 `parallel_research` | 权限测试红（WARN unknown tool） |
| **P** | **突变 D**：全部失败也 `success=True` | all-fail 测试红 |
| **R** | `git status --porcelain` 白名单 | 仅 §5 文件 + 新测试/文档 |

### 7.1 具体用例（先设计，后实现）

| # | 用例 | 类型 | 期望 |
|---|---|---|---|
| U1 | schema：2–4、question 必填、context 可选 | 契约 | 1/5 条拒绝，2/4 通过 |
| U2 | execute → 3 次 `runner.run`，参数含 `agent=explore`、`isolate=False`、`context_mode=task_only`、`readonly=True`、`output_schema` | 编排 | 假 runner 记录逐项匹配 |
| U3 | 3 问，1 个 `status=error` | 失败隔离 | `success=True`，results 三条，error 段可见 |
| U4 | 3 问全 error | 失败 | `success=False`，`all_children_failed` |
| U5 | 合并：重复 key_files 去重；summary 超 600 截断；总输出 ≤8k | 合并 | 断言长度与去重 |
| U6 | usage：3 个 `{total_tokens: 100}` | 计量 | `data.usage.total_tokens=300`；**不改变** `TurnBooks.tokens_spent` |
| U7 | ParentContext：messages/permissions 原样传递（r25/v2 起不含 instructions） | 上下文 | 假 runner 收到 |
| U8 | permission gate `decide` 对 `parallel_research` 为 ALLOW | 权限 | 不询问 |
| U9 | plan agent 规则对 `parallel_research` 为 ALLOW；`subagent` 仍 DENY | 策略 | 见 §13 |
| U10 | `SubAgentRunner.run(context_mode="task_only", readonly=True)` 覆盖全局；子会话 `readonly=True`、工具面剔除 mutating | 单测 | 现有 `test_subagent_context_mode.py` 追加，假 engine 记录 mode/readonly |
| U11 | 前端 `toolVerb("parallel_research") === "并行调研"` | UI | 断言 |

## 8. 影响与风险

| 项 | 影响 |
|---|---|
| 工具面 | schema 约 +400 字符，每次请求都带 |
| token | N 个子代理 × 600–1500 字 brief；task_only 把父历史复制去掉 |
| 延迟 | 3 个子代理并发，最慢一个决定总时长；受 `subagent_timeout_seconds` |
| 正确性 | 只读，不改仓库；失败隔离；无写合并 |
| 计划模式 | 工具可见但被规则拒绝（与 subagent 同形），不绕过 plan 只读 |
| 预算 | 子代理 usage 只进 `data`/trace，**不进 turn 预算**；花费不会自动触发 token gate |
| 重复调用 | 现有 duplicate guard 会拒同一问题集（默认 guidance「reuse the earlier result」） |

**非目标**：不做写并行、不做递归 fan-out、不做后台任务（`background` 仍走 `subagent`）、
不做跨请求缓存、不新增事件类型。

## 9. 决策（已确认）

| 问题 | 结果 |
|---|---|
| 子代理上下文 | **task_only** |
| 临时 worktree | **不建**（`isolate=False`，读当前工作区；只读已由 readonly 兜住；真机发现 HEAD 快照看不到未提交文件） |
| 失败策略 | **部分成功**（单条失败不影响其余） |
| usage 记账 | **只报告**：`data.usage` + trace；**不纳入 turn/token 预算** |
| plan 模式 | **允许**（只读 fan-out；`subagent` 仍拒） |
| 默认条数上限 | 4（本文默认，不再问） |

## 10. 实施顺序（评审通过后）

1. `subagent.py` 加 `context_mode` / `readonly` + 现有 `test_subagent_context_mode.py` 追加用例（RIPR R/I）。
2. `research_fanout.py` + 假 runner 单测（并发、失败、合并、上限、usage）。
3. `parallel_research_tool.py` + schema/参数/ParentContext 测试。
4. builder 注册 + 权限 allow + plan deny。
5. 前端 label。
6. 全量门禁 + 突变验证；文档更新。

## 11. 收敛检查（按你给的分析清单逐条对照）

| 清单项 | 本文落点 |
|---|---|
| 现有设计与复用契约 | §2（逐件：runner / ParentContext / explore / EmitObserver / 权限 / 工具卡） |
| 对代码的影响与执行范围 | §5（7 个产品文件 + 2 个新测试 + 2 个追加测试 + 文档） |
| 每个文件怎么实现、每一步 | §5 表格 + §5.1 签名 |
| 数据流影响 | §3（模型 → dispatcher → tool → fanout → N children → merge） |
| 成功后的效果 | §6 |
| 模糊项 / 风险 | §5.2 自检 + §8 + 本文已确认决策 |
| 最小化 / 第一性 | 不新增 agent、worktree 管理器、semaphore、事件、前端组件；只加一个工具与一个编排类 |
| 契约与函数作用 | §4 + §5.1；没有「说不清作用」的接口 |
| TDD / RIPR | §7（R/I/P/R + 7 条突变 + 11 个用例） |
| 架构图 | §3 |
| 最终模样 | §6（模型一次调用、3 条结果、只读、失败隔离、花费可见） |

**已确认决策**：task_only、部分成功、usage 只报告不进预算、plan 模式允许只读调研
（`subagent` 仍拒）、不建 worktree、readonly 硬开关、条数上限 4。

## 12. 实施与评审结论（2026-10-30）

**实现**（按 §10 顺序逐项，TDD）：`subagent.py` 的 `context_mode` / `readonly` / `max_concurrent`；
`research_fanout.py`；`parallel_research_tool.py`；`domain/subagent.py` 的 `SUCCESS_STATUSES`；
builder / 权限 ALLOW / plan deny / 前端 label；三个测试文件（含既有 `test_subagent_context_mode.py` 追加）。

**独立评审**（子代理只读代码，不改文件）：无 P0。只读三层、预算只报告、递归、plan 拒绝四项契约均成立。
修复的 P1：

1. **时长上限少算一条 lane**：`timeout + 5` 在 `max_concurrent < 4` 时会在最后一 lane 还在跑时
   取消整个调用（dispatcher 取消后已完成结果全丢）。改为
   `ceil(4 / max_concurrent) * (timeout + 15)`，15s 与 `SubAgentTool` 同源；`max_concurrent`
   成为 `SubAgentRunner` 的只读属性。
2. **截断不标注**：summary/key_files/findings 被裁时 `truncated` 仍为子代理自己的值（成功恒 False），
   报告看起来完整。改为任何一层裁切都置 `truncated=True`；成功的 `max_iterations` 不再写「错误」。

其余采纳的 P2：`SUCCESS_STATUSES` 收敛到 domain；工具时长上限按 lane；`scope` 单项 200 字上限；
描述不再无条件宣称 worktree；`runner_unavailable` 作为不可达分支不实现。

**真机**（`config.local.yaml` → dashscope，真实模型）：

- `parallel_research` 2 问：`success=True`、`completed=2`、18–67s、usage 正常求和
  （`prompt 49134 / completion 8156`）；trace 里子代理 span 挂在同一 tool call 下。
- 只读探针：`SubAgentRunner.run(readonly=True, isolate=False)` 真实子代理只拿到
  `glob / grep / read`，明确回答「没有 write 工具」，探针文件未产生。
- 真机暴露的问题：worktree 里 `wave_runner.py`（未跟踪）被报「不存在」→ 已按用户拍板改为
  `isolate=False`（见 §4.6 / §9）。

**门禁**：后端全量 `pytest` exit 0；`ruff` / `mypy`（171 files）干净；前端 `tsc` 通过、577 tests pass。

**残留风险（不改）**：调用仍可能被**回合级** deadline 取消（父回合 `turn_timeout_seconds` 用尽），
此时已完成的 packet 会一起丢失——这是 dispatcher「取消即丢弃」的既有语义，`subAgent` 同形；
工具自己的上限已经按 lane 算够，不会先于子代理预算触发。


## 13. r25/B：规划阶段放开只读调研（已实现）

**为什么改**（复研结论）：计划执行回合的波会话是 `agent="build"`、`readonly=False`
（`core/wave_runner.py:675-694`），主栈工具里有 `parallel_research`，所以**执行阶段**模型本来
就能自己发起调研并当场读完整报告（≤8k tool result）。真正的缺口在**规划阶段**：
`_PLAN_SOFT_DENY` 把 `parallel_research`、`webfetch`、`websearch`、`subagent` 全拒了，
规划时只能串行 read/grep。Claude Code 的 Plan 子代理正是这个形状：plan 模式里用 read-only
子代理做调研，主会话保持只读。

因此原 C3「执行器代跑调研波」被放弃（它需要把结果跨会话搬运进下一波，通道只有 900 字的
`wave_note`，必然有损；详见 `docs/c3-research-wave-plan.md` §13）。

**改了什么**：
- `agents/registry.py`：`_PLAN_SOFT_DENY` 移除 `parallel_research`。`subagent` 保留——
  它能指向有写工具的 `general`，放开它等于从后门扩大 plan 会话的只读姿态。
- `core/plan_runner.py`：`PLAN_PROMPT` 增加一句——2–4 个独立只读问题用 `parallel_research`，
  它并发跑只读子代理并返回一份合并报告。不加这句，提示词仍在说「Explore with read/grep/glob」，
  模型不会去用它。
- 测试：`test_parallel_research_tool.py` 的原「plan agent 拒绝本工具」用例按新契约改写为
  `gate.hides(...) is False` + `gate.check(...) is ALLOW` + `subagent` 仍 `hides`；新增
  真实请求面用例（驱动一次 plan posture 的 `process_input`，读引擎收到的工具表）：
  `parallel_research` 在、`subAgent` 不在。

**为什么安全**：fan-out 子代理固定 `agent="explore"` + `readonly=True`（B0 §4.6），
`explore` 白名单本身只读；plan 会话的 `readonly` 覆盖层仍在，写工具照旧被 dispatch 结构性拒绝。
`subagent` 依旧被拒，所以规划阶段无法经它拿到写能力。

**已知取舍**：`explore` 白名单含 `webfetch`/`websearch`，所以规划阶段的调研子代理可以联网
（用户已拍板允许）；plan 主会话自己仍然不能直接联网。规划回合会因此更慢、更贵，是否使用由
模型按需决定。

**真机**（`config.local.yaml` → dashscope，真实模型）：真实 `AgentLoop` + 真实 registry +
`session.agent="plan"`、`readonly=True`。请求面 11 个工具，`parallel_research` 在、
`subAgent` / `bash` 不在、`read` 在。模型在同一回合里先 `read`、再调
`parallel_research`（2 个问题），拿到 **5500 字符**的合并报告（含两个问题的 id/摘要/关键文件），
随后写出分工总结并用 `finish_research` 收尾——全程零写入、未提交计划。
探针脚本在 `/tmp/r25_plan_research_smoke.py`（不进仓库）。
