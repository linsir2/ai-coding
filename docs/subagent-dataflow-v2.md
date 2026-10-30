# 子代理信息流复研与 v2 方案（评审稿）

> 状态：**已评审、已实现、已跑真机**（2026-10-30）。v1（`docs/subagent-handoff-contract.md`）
> 冻结了 brief 与 packet 的**形状**；这一轮定的是**内容**：父子之间到底传什么、哪些信息对
> 模型下一步有用、以及信息在 worktree/后台这两条路径上是否真的传得下来。
>
> 结论先说：发现 **2 个 P0 + 4 个 P1**，其中两个是这次真读代码才暴露的——子代理的系统提示
> 被拼了两遍（父的 assembled 提示 + 子自己再 build），以及**子代理的写在 worktree 删除时
> 直接丢失**（没有 commit，artifacts 指向已删除路径）。v2 方案在 §5，四个待拍板在 §11。

## 1. 复研方法

- 本地：把 `ParentContext → PromptAssembler → 子 AgentLoop → SubagentResult → 三个消费者`
  整条链逐段读了一遍（路径见 §2/§4），并用真实 `PromptAssembler` 复现了重复拼接。
- 外部：opencode 源码笔记（本仓库 `docs/orchestration-explore/source-research/opencode.md`
  已对 `packages/core/src/tool/plugin/subagent.ts` 做过逐行分析）、Claude Code 官方文档、
  DSH（DeepSeek Harness）subagent 插件、OpenHands task tool。

### 1.1 外部实现的关键事实

| 来源 | 输入给子代理 | 回给父代理 | 启示 |
|---|---|---|---|
| opencode（`opencode.md:742-825`） | 只有一行角色前缀 + `input.prompt`：`"You are a subagent spawned by another session." + prompt`；**不复制父的 system prompt/历史** | 一条合成消息：`<subagent sessionID="…" state="…" description="…">\n{text}\n</subagent>` + metadata `{source, childID, agent, state}` | 子代理是**自己的完整 session**（自己的 select/prompt/tools）；父只收一个紧凑信封 |
| Claude Code 官方 | 子代理有独立上下文；`Explore`/`Plan` 不加载 CLAUDE.md/git 快照以省 token | 只回**final message + metadata**，其余上下文丢弃；worktree 隔离可选 | 层数/上下文都该克制；回包就是最终结果 |
| DSH（npm `@dsh-cc/subagent-task`、`@xfcodeai/dsh-tool-subagent`） | 兼容 Claude Code Task：`subagent_type` + prompt；支持 `continuable`（返回 id 可继续对话） | 结果 + 可继续的句柄 | 长任务要能续；权限沿委派树继承 |
| OpenHands Task tool | `task(prompt, type)`，子会话可 resume | `TaskObservation`（结果） | 输入只有 prompt+type，输出是观察结果 |

共同点：**父 → 子只给任务与必要的角色，不整段复制父提示；子 → 父只给结果与少量元数据。**
我们的 v1 在「回包形状」上已经对齐，但在「输入」上仍把父的 assembled 提示整段塞进去，
并且回包里混入了对模型决策无用的诊断字段。

## 2. 现状数据流（逐段，带代码位置）

```text
父回合
  └─ tool call: subagent / parallel_research
      ├─ 工具从 ToolContext 取：
      │    messages   = 本次请求视图（压缩后，不含 task block）  request_builder.py:109-120
      │    instructions = 已装配的 system prompt（整段）        request_builder.py:118
      │    permissions = 会话规则
      └─ ParentContext(messages, instructions, permissions)     domain/subagent.py:40-55
          └─ SubAgentRunner.run(task, ...)
              ├─ 可选 worktree（isolate=true）：temp 目录，退出即删   core/subagent.py:258-286
              └─ _run_in：
                   full      : seeded = parent.messages
                               prompt = PromptAssembler(parent.instructions)   ← 已装配的整段
                   task_only : seeded = []
                               prompt = PromptAssembler(self._base_instructions)
                   ↓ 每一轮 _build_instructions：
                   parent 的整段 + project + agent body + <methods>(子工具面) + skills
                                                        ai/prompt.py:112-172
                   ↓ 子跑完
              SubagentResult（17 字段）→ _with_ledger 写 JSON
                   ├─ subagent_tool: _render（模型可见）+ data
                   ├─ research_fanout: _packet（子集）→ render_report
                   ├─ background: row + notice（200 字摘要 + ledger 指针）
                   └─ _write_trace_note: notes → transcripts/skill-traces/trace-field.md
```

## 3. 字段消费者矩阵（谁真的读）

| 字段 | 模型可见（`_render`） | `data` | 研究合并 | 后台通知 | 其它 | 判断 |
|---|---|---|---|---|---|---|
| status | 标题 | ✓ | ✓ | ✓ | row | 有效 |
| conclusion | 正文 | 仅长度 | 摘要兜底 | 摘要 | ledger | 有效 |
| todos | 未完成项 | ✓ | — | — | ledger | 有效（实现型委派） |
| artifacts | 产物路径 | ✓ | — | — | ledger | **有陷阱**，见 F2 |
| iterations | 标题 | ✓ | — | — | ledger | 低价值 |
| workdir | 「工作区」 | ✓ | — | — | ledger | **噪音**，见 F2 |
| ledger_path | 「完整回包」 | ✓ | — | ✓ | row/notice | 仅在需要时有用 |
| error | 无正文时 | ✓ | ✓ | ✓ | row | 有效 |
| context_mode | — | ✓ | — | — | ledger/测试 | 诊断，模型不需要 |
| truncated | 警告 | ✓ | ✓ | ✓ | — | 有效 |
| exit_reason | 警告 | ✓ | ✓ | — | ledger | 有效 |
| usage | tokens 行 | ✓ | 求和 | row | ledger | **报告用，模型不需要** |
| facts | 最多 8 键、每值 80 字 | ✓ | 解析 | **丢失** | ledger | 有效（研究路径完整） |
| schema_valid | 失败时警告 | ✓ | ✓ | **丢失** | ledger | 有效 |
| schema_errors | 失败时文字 | ✓ | ✓ | **丢失** | ledger | 有效 |
| notes | — | — | — | — | **只写 trace-field.md** | **无读者** |
| confidence | — | — | — | — | **仅 ledger** | **无消费者** |

## 4. 发现

### F1（P0）full 模式的系统提示被拼了两遍

`ToolContext.instructions` 是**已装配**的整段（`request_builder.py:118`，来自
`agent_loop._build_instructions`）。`_run_in` 把它当作 `PromptAssembler` 的 **base**
（`core/subagent.py:600-616`），而 `_build_instructions` 又会追加 project / agent /
`<methods>` / skills（`ai/prompt.py:128-172`）。结果：

```text
parent = BASE + project + agent + methods(parent tools) + skills
child  = parent + project + agent + methods(child tools) + skills
```

用真实 `PromptAssembler` 复现（BASE 很短、project/skills 各一段）：

| 指标 | 父 | 子 |
|---|---|---|
| `<methods>` 段数 | 1 | **2** |
| project 段数 | 1 | **2** |
| skills 段数 | 1 | **2** |
| 字符数 | 617 | **1140** |

后果：token 翻倍；孩子同时看到父的工具方法（包括它没有的 `subAgent` 行）和自己的方法；
两段 project/skills 内容相同，纯噪音。opencode 的做法正好相反：孩子只拿一行角色 + prompt，
system prompt 由孩子自己的 session 重新装配。

### F2（P0）子代理的写会丢，artifacts/workdir 指向已删除路径

`_run_isolated` 用 `worktree_manager.temporary()`（`core/subagent.py:264`），
`temporary` 的 `finally` 强制 `git worktree remove --force`（`core/worktree.py:129-140`）。
子代理**没有 commit**（`grep commit core/subagent.py` 为空），分支 `ai_sub_*` 仍停在原 HEAD，
worktree 里的未提交改动随目录一起删除。

而回包把 `workdir`（worktree 路径）和 `artifacts`（文件路径）交给父级（`tools/subagent_tool.py:80-88`）。
父模型如果按路径去读，文件已不存在；如果委派的是「修 bug」，改动等于没发生。
`general` 子代理是有写工具的（`agents/registry.py:141-146`），`subagent` 工具默认
`isolate=True`（`subagent_worktree_isolation: true`），所以这条路径是可达的。

opencode 的子代理是完整 session，结果持续存在；Claude Code 的 worktree 也是可回看的隔离副本。
我们的「临时 worktree + 强制删除」与「回包承诺 changed files」自相矛盾。

### F3（P1）模型可见回包混入决策无关的诊断

`_render`（`tools/subagent_tool.py:41-88`）把 `usage`（prompt/completion tokens）、`workdir`
（临时目录）、`ledger_path`（每条都显示）、`iterations` 都写给模型。其中 usage 是纯报告字段
（v1 已确认不进预算），workdir 在 F2 下还是死路径，ledger 只在回包被截断/校验失败时才需要。
这些信息占上下文、分散模型注意力，却不改变下一步决策。

### F4（P1）notes 是只写不读；confidence 没有消费者

- `notes` 只进 `_write_trace_note` → `transcripts/skill-traces/trace-field.md`
  （`core/subagent.py:899-930`）。全仓库没有任何读该文件的代码（只有测试读）；
  所谓「给后来者的笔记」并没有进入任何后续子代理的提示。
- `confidence` 只在 `SubagentResult`、`_with_ledger`、`to_dict` 之间传递；没有渲染、没有
  事件、没有 UI（前端 grep 无 confidence）。F4 的「不确定协议」收集了它，却没人用。

按「有用/有人消费才保留」的标准，这两个字段与 trace-field 写入目前是冗余。

### F5（P2）后台完成通知丢掉了结构化结果

`_finish_background` 的 notice 只用 `conclusion or error` 的 200 字摘要
（`core/subagent.py:431-468`）。带 schema 的后台子代理，`facts`/`schema_valid` 不进通知，
模型只拿到一句散文 + ledger 指针；要看结构化结果必须再读文件。

### F6（P2）同一份回包，两个适配器给出不同信息

`subagent` 用 `_render`（结论 + facts 单行 + 未完成项 + 产物 + workdir + ledger），
`parallel_research` 用 `render_report`（摘要 + findings + 文件）。同一个子代理失败，
前者给 `child_timeout`，后者原样 `timeout`。v1 已把「status 原样、适配器各自映射」写成规则，
但**信息量**仍不一致：研究路径没有 todo/artifact，工具路径没有 findings。

## 5. v2 方案（最小改动，先解决 P0）

### 5.1 输入：子代理装配自己的提示，父只给视图

对齐 opencode（孩子是完整 session）与 Claude Code（孩子有独立上下文）：

- `_run_in` 一律 `PromptAssembler(self._base_instructions)`（task_only 与 full 相同）；
  **唯一区别是 `seeded` 与 project 段**：full 播种 `parent.messages` 并启用 project 段，
  task_only 播种空且不启用（R7e-4 的「干净窗口」保持不变）。
- project 段由 `SubAgentRunner` 在构造时建立一次：`ProjectInstructionsLoader(self._workspace)`
  ——用**主工作区**而不是 worktree，保证同一项目读到同一份约定；只传给 full 模式的孩子。
  `build_subagent_runner` 已有 workspace，不新增跨层参数。
- **删除 `ParentContext.instructions`**：它变成没有任何消费者；`subagent_tool` /
  `parallel_research_tool` 不再传 `instructions=ctx.instructions`。
- 效果：project/skills/methods 各一次；`<methods>` 按**孩子自己的工具面**裁剪（不再看到
  `subAgent` 行）；`<plan-mode>` 段不会误入子代理；token 约减半。

### 5.2 输出：模型只读「下一步决策需要的东西」

`_render` 收敛为：

```text
[name] status in N iteration(s)              # 状态
⚠ truncated: …                               # 有截断才显示
<conclusion>                                 # 正文
facts: k=v, …                                # 有 schema 时
⚠ 结构校验未过（…），已保留原文                 # 校验失败时
未完成项 (n): …                               # 有才显示
产物: …                                      # 有才显示
工作区分支: ai_sub_xxx（未合并）                 # 见 5.3；没有隔离时不显示
完整回包: <path>                             # 只在 truncated 或 schema 失败时显示
```

移出模型可见面、只留 `data`：`usage`、`workdir`、`iterations`、`context_mode`。
`todos`/`artifacts`/`error`/`truncated`/`facts`/`schema_*` 保留。

### 5.3 写委派：有 commit 才有交接

在 `_run_isolated` 的 worktree 分支里，子代理返回后：

1. 若 worktree 有改动：`git add -A && git commit -m "subagent: <name>"`；
2. 把 `worktree_branch` / `commit` 放进 `SubagentResult`，并把 packet 版本升到 **v2**
   （删了 notes/confidence，按 v1 自己的规则「删字段 = 大版本」）；
3. 渲染成「工作区分支: ai_sub_xxx（未合并）」，而不是一个会消失的路径；
4. worktree 目录仍删除，**分支保留**，父级可以 `git diff <branch>` 或合并。

这样 artifacts 的路径虽然失效，但父级知道去哪个分支看；不再把「已删除的临时路径」当成交接物。

### 5.4 冗余字段的去留（待拍板）

- `notes` + `trace-field.md`：没有读者。
- `confidence`：没有消费者。

二选一：删除（协议、字段、写入、测试一起删），或给它们真正的消费者（confidence 在低置信度
时渲染一行；notes 只进 ledger 供人看，删掉 trace-field 写入）。

### 5.5 不改的东西

- `SubagentBrief` 的形状与上限不变；packet 升到 v2（删 notes/confidence、加
  `worktree_branch`/`commit`），版本规则不变。
- 工具面、agent 目录、权限层、fan-out 并发模型。
- `subagent` 工具的 `task: string` 自由文本（v1 已拍板）。
- 研究合并的 `render_report`（它服务多问题合并，语义不同；不强行统一）。

## 6. 逐文件改动

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `ai_coding/core/subagent.py` | `_run_in` 统一 `PromptAssembler(base_instructions)` + `project=ProjectInstructionsLoader(workspace)`；删除 parent.instructions 分支；`_render` 裁剪；`_finish_background` 摘要带 schema 提示 | F1/F3/F5 |
| 2 | `ai_coding/domain/subagent.py` | `ParentContext` 删 `instructions`；`SubagentResult` 加 `worktree_branch`/`commit`（可选，wire 加法）；`to_dict` 同步 | F1/F2 |
| 3 | `ai_coding/tools/subagent_tool.py` | 不再传 `instructions`；description 写明「写改动在分支上，未合并」 | F1/F2 |
| 4 | `ai_coding/tools/parallel_research_tool.py` | 不再传 `instructions` | F1 |
| 5 | `ai_coding/core/subagent.py`（worktree 分支） | 有改动则 commit；把 branch/commit 放进结果 | F2 |
| 6 | `ai_coding/core/plan_runner.py` / `wave_runner.py` | 若它们构造 `ParentContext`，同步删 `instructions`（grep 确认） | F1 |
| 7 | 测试 | `test_subagent_context_mode`：full 改为「播种父消息、提示来自 base/project」；新增提示只出现一次 project/methods；`test_structured_handoff`：render 不再含 usage/workdir；新增 worktree commit/分支用例；`test_subagent_contract`：ParentContext 两字段 | RIPR |
| 8 | `docs/subagent-handoff-contract.md` / `docs/b0-…` | v2 同步 | 文档 |

> 先 `grep ParentContext(` 确认所有构造点，避免漏改（当前：`subagent_tool:257`、
> `parallel_research_tool:146`，可能还有测试）。

## 7. v2 数据流

```text
父回合
  └─ tool call
      ├─ ParentContext(messages, permissions)          ← instructions 删除
      └─ SubAgentRunner.run(task, ...)
          ├─ worktree（隔离时）：子跑 → 有改动则 commit → 分支保留
          └─ _run_in：
               prompt = base + project + agent body + <methods>(子工具面) + skills
               seeded = parent.messages（full）| []（task_only）
               ↓
          SubagentResult(+ worktree_branch/commit)
               ├─ 模型可见：status/conclusion/facts/未完成/产物/分支/按需 ledger
               ├─ data：全部字段（含 usage/workdir/context_mode）
               ├─ background notice：状态 + 摘要 + schema 失败提示 + ledger
               └─ ledger：完整 packet（含 notes/confidence 若保留）
```

## 8. 成功后的样子

- full 模式子代理的 prompt 里 project/skills/`<methods>` 各一次，工具方法只有它自己的；
  同样的任务，子代理 prompt 约减半。
- 委派「修 bug」：回包写「工作区分支: ai_sub_fix_ab12（未合并）」，父级能 `git diff` 到改动；
  artifacts 不再被当成可读路径。
- 模型看到的回包只有：状态、结论、结构化 facts、未完成项、产物、必要时的 ledger；
  usage/workdir 不再占位。
- 后台子代理完成通知能说明「结构校验未过」，不再只给散文。
- `notes`/`confidence` 要么有真实消费者，要么不存在。

## 9. RIPR

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 先写用例：prompt 不重复、full 播种消息、render 无 usage/workdir、worktree 有 commit/分支、后台 notice 带校验结论 | 全红 |
| **I** | §6 的 1–6 | 转绿 |
| **P** | 相关 + 全量 | 0 failed |
| **P** | `ruff`/`mypy`/前端 | 干净 |
| **P** | 突变 A：full 仍用 parent.instructions | 重复用例红 |
| **P** | 突变 B：worktree 不 commit | 分支用例红（改动丢失） |
| **P** | 突变 C：render 仍输出 usage | 裁剪用例红 |
| **P** | 突变 D：后台 notice 无 schema 提示 | 通知用例红 |
| **R** | `git status --porcelain` | 仅 §6 文件 |

### 9.1 用例

| # | 用例 | 期望 |
|---|---|---|
| D1 | full 模式：父提示为 assembled，子提示里 project/skills/`<methods>` 各 1 次 | 无重复 |
| D2 | full 模式：子首条消息含父消息内容；task_only 不含 | 播种语义不变 |
| D3 | 子提示的 `<methods>` 不含 `subAgent` 行（子栈没有它） | 工具面裁剪生效 |
| D4 | 真实 git 仓库 + 隔离：子写文件 → 回包有 branch/commit，`git show` 能看到改动 | 写不丢 |
| D5 | `_render` 不出现 `tokens:`/`工作区`；截断时出现 ledger | 模型面收敛 |
| D6 | 后台 + schema 失败：notice 含「结构校验未过」 | 通知不丢结论 |
| D7 | `ParentContext` 两字段构造；旧调用点全部更新（grep 为 0） | 无残留 |

## 10. 风险 / 自检

1. **prompt 语义变化**：full 从「父的整段提示」变成「同一 base/project/skills + 子 agent」。
   真机要跑一次 full 子代理，确认行为不退化（尤其 explore 的 role 与 project 约定）。
2. **worktree commit 需要 git 身份**：容器里可能没有 `user.name/user.email`。要么显式
   `-c user.name=... -c user.email=...`，要么检查失败时降级为「改动未提交，分支保留」并照实报告。
   不能因为 commit 失败让子代理结果丢失。
3. **commit 范围**：`git add -A` 会把子代理写的临时文件也提交；隔离 worktree 里这是预期的，
   但要在提交信息里写明 `subagent: <name>`，便于父级识别。
4. **删除 ParentContext.instructions**：所有构造点必须一次改完；漏一个就是运行期 TypeError。
5. **notes/confidence 删除**：`test_trace_field.py`/`test_f1_f4_fixes.py` 会整组失效，需按新契约
   重写或删除；这是需求变更，不是测试出错。
6. **后台通知**：200 字摘要 + 一行校验提示，不要塞 facts（会超）。

## 11. 决定（2026-10-30）

| 问题 | 决定 |
|---|---|
| 写委派（F2） | **commit + 分支交接**：成功后有改动就 commit，packet 带 `worktree_branch`/`commit`，worktree 目录仍删、分支保留 |
| full 提示（F1） | **子代理自己装配**：base + project + agent body + 自己的 methods + skills；`ParentContext.instructions` 删除 |
| notes/confidence（F4） | **全部删除**：不确定协议、两个字段、`_extract_notes_and_confidence`、trace-field 写入与相关测试 |
| 模型可见回包（F3） | **只留决策相关**：删 usage/workdir/iterations；ledger 仅在截断或结构校验失败时显示 |

## 12. 实施与验证（2026-10-30）

**实现**：`domain/subagent.py`（ParentContext 两字段；SubagentResult 删 notes/confidence、
加 branch/commit）；`core/subagent.py`（子代理自建提示、project 仅 full、删协议与 trace-field、
后台通知带分支与校验结论）；`core/worktree.py`（`commit_if_dirty`，自带 git 身份、失败返回
None）；`tools/subagent_tool.py`（模型面裁剪、分支行、data 带 branch/commit）；builder 注入
`ProjectInstructionsLoader`。

**RIPR 突变**（每条先红后还原）：提示不重建 → full 用例红；不 commit → 分支用例红；
ledger 每条都显示 → 报告裁剪用例红；后台通知不标校验失败 → 通知用例红；不传 project →
project 一次用例红。

**门禁**：后端全量 `pytest` exit 0；`ruff`/`mypy` 干净；前端 `tsc` + 577 tests pass。

**真机**（dashscope，真实模型）：

- full 子代理：prompt 981 字符、`<methods>` 1 次、不含父提示、不含 `subAgent` 行；
  `status=completed`，结论正确。
- 隔离写委派：`status=completed`、branch `ai_sub_write-probe_0e92d344`、
  commit `704af9c…`；`git show <branch>:hello.txt` 得到 `ok` —— 改动不再丢失。
- ledger 同步：同一个 run 的 ledger 文件是 `v=2`、`worktree_branch` 与 packet 一致
  （commit 发生在 ledger 写入之后，`_refresh_ledger` 会重写一次，否则"完整回包"里没有分支）。
- 复跑 B0 两问 fan-out：两条均 completed，报告质量未退化。
