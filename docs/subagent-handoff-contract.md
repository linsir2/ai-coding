# 子代理交接契约：父 → 子 / 子 → 父（评审稿）

> 状态：**已评审、已实现、已跑真机**（2026-10-30）。目标是把两个方向的数据契约**冻结**下来：父代理交给子代理的
> 简报与继承上下文，以及子代理交回父代理的 packet。冻结 = 字段表固定、上限固定、校验
> 规则固定、版本策略固定，并有测试钉住。
>
> 范围只做这两份契约与它们的校验/渲染；不改子代理运行时、不加新工具、不改 fan-out 的
> 合并策略。四个待拍板问题在 §12。

> **v2 提示**：§1–§2 是 v1 落地时的盘点（保留作历史依据）。v2 已删除 notes/confidence、
> 删除 `ParentContext.instructions`、回包新增 branch/commit；变化与真机见 §13 与
> `docs/subagent-dataflow-v2.md`。

## 1. 现状盘点（逐字段，已核对代码）

### 1.1 父 → 子：继承上下文

| 字段 | 位置 | 现状 | 问题 |
|---|---|---|---|
| `messages` | `domain/subagent.py:40-55` `ParentContext` | 父请求视图（压缩后、**不含**易变 task block） | 无显式上限/版本；靠上层压缩保证 |
| `instructions` | 同上 | 父的 system prompt；`task_only` 时换成 composition root 的 base instructions | 空串必须保持 `None`（已有测试） |
| `permissions` | 同上 | 父会话规则（M1a ④） | 已冻结，无问题 |

`ParentContext` 本身是 `frozen=True`，结构没问题；缺的是**把"传什么、不传什么"写成契约
并用测试钉住**（例如"task block 不传"、"task_only 下 messages 被忽略"）。

### 1.2 父 → 子：任务简报

| 现状 | 位置 | 问题 |
|---|---|---|
| `run(task: str, ...)` 只接受字符串 | `core/subagent.py:185-240` | 没有结构；不同调用点各自拼散文 |
| `ResearchFanOut._brief` 拼中文散文 | `core/research_fanout.py:158-170` | 只有 fan-out 一条路有"背景/问题/范围"的隐式格式；无校验、无渲染契约 |
| `SubAgentTool` 直接透传模型的 `task` 字符串 | `tools/subagent_tool.py:198-206, 281` | 自由文本是产品特性（模型自己写），但没有任何边界 |

### 1.3 子 → 父：回包

| 字段 | 位置 | 现状 |
|---|---|---|
| `status / conclusion / todos / artifacts / iterations / workdir / ledger_path / error / context_mode / truncated / exit_reason / usage / facts / schema_valid / schema_errors / notes / confidence` | `domain/subagent.py:57-123` `SubagentResult`，`frozen=True` | 17 个字段，R1→R4 一路加法；`to_dict()` **无版本字段** |
| `facts` | 由 `extract_json_facts` 解析 | 只校验 `required` 键**存在**，不校验类型（`domain/subagent.py:128-166`） |
| `schema_valid/schema_errors` | 同上 | 三态（None=没要 schema）设计正确 |
| `notes/confidence` | `core/subagent.py:774-799` | 只认中文尾注（`_UNCERTAINTY_PROTOCOL`） |
| ledger | `core/subagent.py:802-832` | `to_dict()` 原样落盘，是长期产物 |
| 两个消费者 | `tools/subagent_tool.py:77-118`；`core/research_fanout.py:172-207` | 同一个 packet，两套映射 |

## 2. 复核发现的 6 个真实问题

1. **输入没有结构**：只有 fan-out 会拼固定格式的 brief，其它调用点（`subAgent`）是任意散文，
   没有"必须给 objective / 可选给 scope/context/constraints"的契约。
2. **schema 只查键、不查类型**：`extract_json_facts` 只做 `required` 存在性检查。子代理返回
   `{"summary": 123, "key_files": "a.py"}` 也判 `valid=True`，而 `RESEARCH_SCHEMA`
   （`core/research_fanout.py:36-45`）明确声明了 `string` / `array of string`。**声明与执行不一致。**
3. **两条返回指令互相矛盾**：`_format_instruction`（`core/subagent.py:836-852`）说
   "Output the JSON object and nothing else"；`_UNCERTAINTY_PROTOCOL`（`:922-930`）又要求
   在结尾追加中文四行（置信度/原因/需要/笔记）。带 schema 的调用同时收到两条互斥指令；
   `_extract_notes_and_confidence` 只认中文行，所以 JSON 调用**永远拿不到** notes/confidence。
4. **回包没有版本**：`to_dict()` 无版本键。ledger 文件是跨会话、跨版本留存的长产物，字段已从
   6 个长到 17 个；没有版本就无法判断一个旧文件是哪一代形状，也没有"哪些改动是加法"的规则。
5. **两个消费者各自解释 status**：`subagent_tool` 把 `timeout` 映射成 `child_timeout`
   （`CHILD_ERRORS`，`tools/subagent_tool.py:27-33`），`research_fanout` 原样暴露 `timeout`。
   没有契约说"packet 带原始 status、适配器各自映射"，于是同一个子代理失败在两个工具里
   是两个错误码。
6. **上限分散**：summary 600 / files 8 / findings 5（`core/research_fanout.py:46-49`）、
   子代理结论 4000（`config/models.py:507`）、工具 schema 4000（`tools/subagent_tool.py:20-25`）、
   工具侧 question 800 / context 1000（`tools/parallel_research_tool.py:33-38`）。同一个
   "上下文/简报长度"没有单一来源，绕过工具直接调 fan-out 的调用点（未来 C3）就没有校验。

## 3. 网络调研结论

| 来源 | 结论 | 落点 |
|---|---|---|
| OpenAI Agents SDK Handoffs：`input_filter` 接收并返回 `HandoffInputData`（完整历史默认传入，可裁剪） | 交接的输入是一个**显式、可裁剪的对象**，不是隐式全局 | 我们的 `ParentContext` + `context_mode`（full/task_only）就是这个形状；把它写成契约 |
| 同一文档：handoff 后由 specialist 拥有对话直到 final output | 交接输出是"最终结果"，不是中间推理 | 我们只回包 conclusion + 结构化 facts，不回推理（已经是这样） |
| Claude Agent SDK Structured outputs：传 JSON Schema，结束时结果里带**校验过**的 `structured_output` | 结构化回包必须**被校验**，不能只声明 | 补 `type`/`items.type` 校验；`schema_valid` 保留三态 |
| AI-SDLC Agent Orchestration Spec §4.3：契约要声明版本；加可选字段=兼容，加必填/删字段=破坏 | 版本与加法规则必须写死 | `SUBAGENT_PACKET_VERSION` + `to_dict()["v"]` + additive-only |
| Agent Handoff Contracts（Moeid Saleem）：每个 handoff payload 内嵌 `schema_version`，按破坏性 API 变更管理 | 同上；没有版本就无法演化 | 同上 |
| AgentPatterns / Fan-Out-Fan-In：子代理回包是**压缩摘要**，orchestrator 合并 | 回包本来就该有上限，不是全量 transcript | 沿用 conclusion 4000 / summary 600 等上限，并集中定义 |

## 4. 冻结契约 A：父 → 子（`SubagentBrief`）

```python
# domain/subagent.py
OBJECTIVE_MAX = 800
CONTEXT_MAX = 1_000
SCOPE_ITEMS_MAX = 10
SCOPE_ITEM_MAX = 200
CONSTRAINTS_MAX = 10
CONSTRAINT_MAX = 200

@dataclass(frozen=True)
class SubagentBrief:
    objective: str                                  # 必填
    scope: tuple[str, ...] = ()                     # 优先搜索范围
    context: str = ""                               # 共享背景/约束
    constraints: tuple[str, ...] = ()               # 硬约束（只读、不改文件…）

    def problems(self) -> tuple[str, ...]: ...      # 长度/条数校验，空 = 合法
    def render(self) -> str: ...                    # 确定性渲染，见下
```

**渲染（冻死的格式，测试逐字节钉住）**：

```text
[TASK]
<objective>

[CONTEXT]
<context>            # 为空则整段省略

[SCOPE]
- <scope[0]>
- <scope[1]>          # 为空则整段省略

[CONSTRAINTS]
- <constraints[0]>    # 为空则整段省略
```

- objective 必填且非空；超限/超条数 → `problems()` 非空，调用方**拒绝**（不静默截断）。
- 渲染结果总长上限 `BRIEF_MAX = 4_000`（与子代理结论上限同一个数量级）；超限也是一个
  problem，由调用方拒绝。
- **不**在 brief 里写"返回 JSON"之类的格式指令：格式由 `output_schema` 决定（§5.2），
  一份 brief 只有一种含义。
- **`run()` 仍是 `str`**：fan-out 在父侧构造 `SubagentBrief` 并 `render()`，再把渲染结果
  交给 `run`；模型自由委派的 `subAgent` 字符串路径**逐字节不变**。两条路径最终都是子代理的
  一条 user message。校验放在 fan-out 的 `_run_one`（拒绝并给 error packet），因为它是唯一
  的内部调用点；不把 `str | SubagentBrief` 联合类型塞进 runner（那会让两个形状都能绕过契约）。

**继承上下文（`ParentContext`）的冻结规则**（不改结构，只写死语义并加测试）：

1. `messages` 是父请求视图，**不含**易变的 `<task-state>` 尾块；
2. `context_mode="task_only"` 时 `messages` 被忽略，子代理只拿 base instructions + brief；
3. `permissions` 原样传给子会话规则层，任何模式都不删减；
4. **系统提示不继承**（r25/v2）：`ParentContext` 只有 `messages`/`permissions` 两个字段；
   子代理用 composition root 的 base + 自己的 agent body + 自己工具面的 methods + skills
   重新装配，`full` 额外启用 project 段并播种父消息，`task_only` 两者都没有。

## 5. 冻结契约 B：子 → 父（packet v2；v1 因 r25 删字段升版）

### 5.1 线上形状（`SubagentResult.to_dict()`）

新增唯一的版本键，其余字段不变：

```json
{ "v": 2, "status": "...", "conclusion": "...", "todos": [], "artifacts": [],
  "iterations": 0, "workdir": null, "ledger_path": "...", "error": null,
  "context_mode": "full", "truncated": false, "exit_reason": null,
  "usage": null, "facts": null, "schema_valid": null, "schema_errors": [],
  "worktree_branch": null, "commit": null }
```

- `SUBAGENT_PACKET_VERSION = 1`，与 `to_dict()` 同处 `domain/subagent.py`。
- **兼容规则（写进文档 + 测试）**：只允许**加可选字段**；加必填字段/删字段/改字段含义 =
  major +1。`v` 是消费方判断依据，不是给模型看的。
- **不写 `from_dict`**：今天没有任何读 packet 的代码（ledger 只是给人/模型看的文件），
  为它写反序列化就是死代码。等有消费者再加。

### 5.2 结构化回包的校验（补类型）

`extract_json_facts` 在"required 存在"之外，增加**最小类型校验**（不是 JSON Schema 解释器）：

- 顶层必须是 object（已有）；
- `required` 键存在（已有）；
- 若 schema 的 `properties[key]` 声明了 `type`，检查基本类型：
  `string / integer / number / boolean / array / object`；
- 若声明 `array` 且给了 `items.type`，检查每个元素的类型；
- 错误信息点名键与期望类型：`key 'summary' must be string, got int`；
- **required 键类型错 → 整个 packet 无 facts**（原有工作仍留在 conclusion）；
  **optional 键类型错 → 只丢这个键，facts 保留其余字段**，同时 `schema_valid=False`
  并记录错误。理由：一个格式错的 optional 字段不该把一份好摘要一起丢掉（真机上第一次
  跑就遇到：`findings` 返回成对象，`summary`/`key_files` 都是对的）。
- `_format_instruction` 现在把**属性形状**也告诉子代理（`summary: string`、
  `key_files: string[]`），因为子代理看不到 schema 对象本身；只报键名是那次真机失败的原因。
- 仍然**容忍额外键**、**忽略** `oneOf/$ref/pattern` 等未实现的 keyword（文档写明）。

这直接修掉问题 2：`RESEARCH_SCHEMA` 声明 `summary: string`、`key_files: array<string>`，
子代理返回数字/字符串时 `schema_valid=False`，父级能看见"形状不对"而不是拿到一个坏值。

### 5.3 返回指令只留一条

- 传了 `output_schema`：只追加 `_format_instruction`（JSON only），**不再**追加
  `_UNCERTAINTY_PROTOCOL`（问题 3）。
- 没传 schema：**不追加任何格式指令**（r25/v2 删除了只写不读的中文不确定协议）。
- F4 的 `notes`/`confidence` 已删除（r25/v2）：前者只写一份没人读的 trace 文件，后者没有
  任何消费者。schema 里声明的可选键仍然会进入 `facts`，由调用方自己解释。

### 5.4 status 与错误码的分工（写死，不改代码）

- packet 里的 `status` **永远是原始值**（`completed|max_iterations|timeout|error|cancelled`）。
- 适配器负责映射到各自工具的错误码：`subagent` → `child_timeout/child_error/child_max_iterations`；
  `parallel_research` → 原样放进 `data.results[].status`，整体失败用 `all_children_failed`。
- 新增测试同时钉住两个适配器，防止下一次"一个改了一个没改"。

### 5.5 上限的单一来源

| 上限 | 值 | 位置 |
|---|---|---|
| brief objective / context / scope / constraints | 800 / 1000 / 10×200 / 10×200 | `domain/subagent.py`（新） |
| 子代理结论 | 4000 | `config/models.py:507`（沿用） |
| 工具 schema | 4000 | `tools/subagent_tool.py`（沿用，移到模块常量注释里引用） |
| research 每条 summary / files / findings | 600 / 8 / 5 | `core/research_fanout.py:46-49`（沿用） |
| 合并报告 | 8000 | `core/research_fanout.py:49`（沿用） |

## 6. 不做的事（说清为什么）

- **不**把通用 `subagent` 工具的 `task: string` 改成结构化字段：模型自由委派是产品特性，
  改它会动模型面 schema、提示词与所有委派测试；冻结的是**内部调用点**（fan-out）的 brief。
- **不**写 `from_dict`（没有消费者）。
- **不**实现完整的 JSON Schema 子集（只做 type/items.type）。
- **不**改 `SubagentResult` 的字段（只加 wire 版本键，不改 dataclass）。
- **不**新增事件、工具、运行时或第二套 fan-out。

## 7. 逐文件改动

| # | 文件 | 改动 |
|---|---|---|
| 1 | `ai_coding/domain/subagent.py` | 新增 brief 上限常量、`SubagentBrief`（problems/render）、`SUBAGENT_PACKET_VERSION`；`to_dict()` 加 `"v"`；`extract_json_facts` 加 type/items.type 校验 |
| 2 | `ai_coding/core/subagent.py` | 子代理自建提示（base+project+agent+methods），`_format_instruction` 给 required/optional 键标注形状；schema 调用只发一条指令 |
| 3 | `ai_coding/core/research_fanout.py` | `_brief` 改为构造 `SubagentBrief` 并 `render()`；question 上限引用 `OBJECTIVE_MAX`，shared context 引用 `CONTEXT_MAX`（删除本模块重复常量） |
| 4 | `ai_coding/tools/parallel_research_tool.py` | 改用 `SubagentBrief` 的上限常量（`_MAX_QUESTION_CHARS`/`_MAX_CONTEXT_CHARS`/`_MAX_SCOPE_*` 删除） |
| 5 | `ai_coding/tools/subagent_tool.py` | 只在 description 里写明回包契约 v1 与"schema 调用只返回 JSON"；行为不变 |
| 6 | `tests/test_structured_handoff.py`（既有） | 追加：类型错误判 invalid（required 致命 / optional 丢弃）、`to_dict()["v"]==2`、schema 调用只发一条 JSON 指令 |
| 7 | `tests/test_subagent_contract.py`（新） | brief：problems（空/超长/超条数）、render 逐字节、确定性；ParentContext 四条语义（task block 不传由构造方测试覆盖） |
| 8 | `tests/test_research_fanout.py`（既有） | 断言 brief 走 `SubagentBrief` 渲染（含 `[TASK]`/`[SCOPE]`），question 超长由 fan-out 自己拒绝 |
| 9 | `docs/b0-parallel-research-plan.md` | §4.1/§4.2 的 brief/schema 改为引用本契约 |

## 8. RIPR

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 先写 §7 的 6/7/8 三类用例 | `pytest` 红（缺 `SubagentBrief`/`v`/类型错误） |
| **I** | §7 的 1–4 产品文件 | 新用例转绿 |
| **P** | 相关测试 + 全量后端 | 0 failed |
| **P** | `ruff` / `mypy` | 干净 |
| **P** | **突变 A**：`extract_json_facts` 删类型检查 | 类型用例红 |
| **P** | **突变 B**：`to_dict` 删 `"v"` | 版本用例红 |
| **P** | **突变 C**：schema 调用仍追加中文协议 | "只有一条返回指令"用例红 |
| **P** | **突变 D**：`_brief` 绕过 `SubagentBrief` 拼散文 | fan-out 渲染用例红 |
| **P** | **突变 E**：`objective` 上限删掉 | problems 用例红 |
| **R** | `git status --porcelain` | 仅 §7 文件 + 文档 |

### 8.1 用例

| # | 用例 | 期望 |
|---|---|---|
| C1 | `SubagentBrief("x"*801)` → `problems()` 点名 objective | 拒绝 |
| C2 | scope 11 项 / 单项 201 字 / constraints 11 项 → problems | 拒绝 |
| C3 | 合法 brief `render()` 逐字节等于固定文本（含/不含可选段两种） | 冻结 |
| C4 | `run(brief)` → 子代理第一条 user message 以 `render()` 开头，且仍带 `_format_instruction` | 输入契约 |
| C5 | `run("散文")` → 子代理文本与今天逐字节一致（回归） | 自由委派不变 |
| C6 | schema 声明 `summary: string`，子代理返回 `{"summary": 1}` → `schema_valid=False`，错误点名 summary | 类型 |
| C7 | schema 声明 `key_files: array<string>`，返回 `{"key_files": "a.py"}` → invalid | items.type |
| C8 | schema 调用时子代理收到的文本**不含**「不确定协议」；无 schema 时**含** | 指令唯一 |
| C9 | `facts={"confidence":"low","notes":"n"}` → packet.confidence/notes 取自 facts | 统一 |
| C10 | `to_dict()["v"] == 2`，且原有 17 个键一个不少 | 线上形状 |
| C11 | 同一 packet 经 `subagent_tool` 与 `research_fanout`：status 原始值不变，错误码按各自规则映射 | 适配器分工 |

## 9. 成功后的样子

- 父级（fan-out）只构造一次 `SubagentBrief`，子代理收到固定信封；长度问题在父级就被拒绝。
- 子代理返回 `{"summary": 123}` 时，父级拿到 `schema_valid=False` 与 `key 'summary' must be
  string, got int`，而不是把一个整数当摘要拼进报告。
- 带 schema 的子代理只收到一条格式指令；自由委派不发任何格式指令。
- ledger 文件第一行有 `"v": 2`；之后只加可选字段时，旧文件仍可读。
- `subagent` 与 `parallel_research` 对同一个失败给出各自一致、可解释的错误码。

## 10. 自检（可能翻车的点）

1. **brief 渲染改变子代理输出质量**：fan-out 的 brief 从中文散文变成固定信封，提示词变了，
   真机需要重跑一次 2 问调研确认质量不降。
2. **类型校验太严**：只检查声明了 `type` 的键；没声明的保持"只查存在"。`RESEARCH_SCHEMA`
   的两个 required 都声明了类型，所以会生效。
3. **`run(task)` 仍是 `str`**：brief 在父侧渲染；`spawn_background` 与后台子代理完全不受影响。
4. **中文协议对自由委派仍有价值**：只在有 schema 时跳过，不删协议本身。
5. **`v` 键会不会被旧消费者误读**：`to_dict` 目前只有 ledger 与 `subagent_list`（后者读内存
   row，不读 packet），没有按位置解包的消费者；加键安全。

## 11. 真机计划

- 脚本：真实 `SubAgentRunner` + dashscope 模型；`brief = SubagentBrief(objective="找出
  wave_planner.py 的职责", scope=("ai_coding/core",), context="这是一个 Python 仓库")`，
  `run(brief.render(), output_schema=RESEARCH_SCHEMA)`。
  - 断言 `schema_valid is True`、`facts["summary"]` 是 `str`、`key_files` 是 `list[str]`；
  - 断言 `to_dict()["v"] == 2`；
  - 信封与"只发一条 JSON 指令"由单测钉住（真机不暴露子代理内部消息）。
- 再跑一次 B0 的 2 问 fan-out 真机，确认改 brief 后输出质量与延迟没有退化。

## 12. 待拍板

四点均已拍板（2026-10-30）：

| 问题 | 决定 |
|---|---|
| 通用 `subagent` 输入 | **保持自由文本**；只冻结内部调用点（`SubagentBrief`） |
| 有 schema 时的返回指令 | **只发 JSON 指令**；schema 声明的可选键仍进 `facts`，packet 不再有 notes/confidence 字段（v2 删除） |
| 类型校验 | **通用函数** `extract_json_facts` 加 `type`/`items.type`（不做 JSON Schema 解释器） |
| 版本 | **写进 `to_dict()`**：v2（r25 删字段升版），加法兼容规则写死 |

## 13. 实施与验证（2026-10-30）

**实现**：`domain/subagent.py`（`SubagentBrief` + 6 个上限常量 + `SUBAGENT_PACKET_VERSION`
+ 类型校验）；`core/subagent.py`（schema 调用只发一条格式指令、可选键点名、facts 优先
schema 调用只发一条指令）；`core/research_fanout.py`（brief 走 `SubagentBrief`、非法 brief 拒绝、
`ResearchPacket.schema_valid/errors`）；`tools/parallel_research_tool.py`（上限改为 domain 常量）。

**RIPR 突变**（每条都先红后还原）：删类型检查 → 类型用例红；删 `"v"` → 版本用例红；
schema 调用仍追加中文协议 → 指令唯一用例红；brief 丢 scope/context → 信封用例红；
objective 上限关掉 → problems 用例红。

**门禁**：后端全量 `pytest` exit 0；`ruff`/`mypy`（171 files）干净；前端 `tsc` + 577 tests pass。

**真机**（真实 `SubAgentRunner` + dashscope）：

- `SubagentBrief` 渲染 + `RESEARCH_SCHEMA`：`status=completed`、`schema_valid=True`、
  `summary` 为 `str`（约 250 字）、`key_files` 为 `list[str]`、`to_dict()["v"] == 2`。
- 第一次真机（只有键名、没有形状）子代理把 `findings` 返回成对象 → `schema_valid=False`。
  这正是 §5.2 那条 optional-drop 与形状提示的来源；修正后重跑通过。
- 复跑 B0 两问 fan-out：`completed=2`，报告仍有摘要/关键文件，质量未退化。
