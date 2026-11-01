# 真机运行记录：C-5b 子智能体编排（qwen-plus）

> 目的：不是测模型能力，而是测**编排层**（spawn / send / interrupt / resume /
> close）在真实 provider 下能不能跑、好不好用、效率与上下文如何。
> 日志：`/tmp/real-agent-run.log`；结果：`/tmp/real-agent-run-result.txt`；
> 会话/指标：`/tmp/real-agent-session.json`。

## 1. 场景

- 工作区 `/tmp/real-agent-ws`：6 个 `aN.txt` + `report.txt`。
- 子任务：逐个读 6 个文件 → 写 `NOTES.md` → 写 `REPORT.md`（真实模型执行）。
- 父侧脚本（不依赖父模型是否愿意调工具）：
  1. `spawn_agent`（`fork_turns=none`）
  2. 等到子代理出现第一条工具结果
  3. `send_message`（追加 ADDENDUM 指令）
  4. 等 `[agent-message]` 投放后 `interrupt_agent`
  5. `resume_agent` → `send_message` 续跑
  6. 等完成 → 校验文件 → `close_agent` → 从磁盘 `resume_agent` → 再 close
- 配置：`plan_orchestrator=codex`、`subagent_worktree_isolation=false`、
  `process_sandbox.mode=warn`、`network.mode="off"`、`max_tool_iterations=100`。
- Key 只在 `/tmp/ds.env`（0600），未入库。

## 2. 结果

15/15 检查通过（`/tmp/real-agent-run-result.txt`）：

| 检查 | 结果 |
|---|---|
| spawned / handle / session / sidecar | 通过 |
| `send_message` 返回 queued，`[agent-message]` 投放 | 通过 |
| interrupt：`running → interrupted`、无 notice、`open_turn=None`、loop 释放 | 通过 |
| resume：idle、不自动起 turn | 通过 |
| 续跑 turn：turn_count=2，最终 REPORT/NOTES 正确、含 ADDENDUM | 通过 |
| 完成 notice 一次 | 通过 |
| close + 从磁盘 resume + 再 close | 通过 |

时间线（毫秒）：spawn 返回 2、首工具 2185、send→投放 1409、interrupt 4、
resume 54、resume→完成 24671。turn1 用量 prompt 8041 / completion 134；
turn2 prompt 44771 / completion 867（累计步数，见 3）。

## 3. 效率与上下文

- 45k 不是单次上下文：`budget.py` 的 prompt 是**turn 内每步累加**，最后一步
  `context=4364`；turn2 共 13 步，约 75% 是每步重复的系统提示与工具 schema，
  历史内容只有约 4.8k 字符。
- 压缩未触发是正确的：闸门看单次 observed（4.3k），远低于 ceiling（约 115k）。
- 主要成本是**步数**（13 次模型调用、24.7s），不是上下文组装。
- send 投放延迟 1.4s = 等下一次 iteration 边界，设计内；interrupt 4ms、resume 54ms
  说明编排本身不慢。

## 4. 发现的问题与处置

| # | 严重度 | 问题 | 处置 |
|---|---|---|---|
| 1 | P1 | interrupt 后 `_attach_loop` 重建工具栈，`write` 内的 `FileStateTracker` 丢失；resume 后 append 被误判 `must_read_first`，模型退化为整文件重写 | **已修**：`AgentHandle.registry` 复用同一工具注册表（`core/subagent.py`），新增测试 `test_resume_reuses_the_tool_registry...` |
| 2 | P1 | 首次真机 `max_tool_iterations=12` 在 turn2 撞上限，未写完 REPORT | 非代码缺陷：产品默认 100；真机配置已改 100。**记录**：`config.example.yaml` 的 10 对多步子代理偏低，建议后续评估子代理是否继承父上限 |
| 3 | P1 | 测试脚本自身断言错误（`notes.md` 大小写、`open_turn_cleared` 写死） | 已修：真实读取 `NOTES.md`、捕获 interrupt 时刻的 `open_turn`、校验 6 行与完成 notice |
| 4 | P2 | 内存路径 `resume` 未清 `pending_approval/pending_posture/write_ahead` 等 | 已修：统一 `_clear_runtime_markers`（内存/磁盘两条路径） |
| 5 | P2 | sidecar 只存 `workdir`，worktree 目录消失后跨进程 resume 无兜底 | 已修：sidecar 增加 `workspace`，resume 回退到它 |
| 6 | P2 | 被 interrupt 的 in-flight 模型调用没有计入 usage | 记录：turn 只在关闭时记 usage；不是执行失败，后续可在中断时补记 |
| 7 | P2 | append 语义要求先 read（即使本会话刚写过） | 记录：修复 tracker 复用后同会话 append 可用；跨重启仍要求 read，属现有 stale-read 设计 |
| 8 | P2 | 测试脚本在 resume 后又发了一次同样的 ADDENDUM 指令，历史里出现两次 | 记录：是测试设计，不是产品重复 |

## 5. 工具调用观察

- 真实模型 10 次 read、5 次 write：a3 读两次、REPORT 连续读两次（内容相同）。
- 任务"每读一个 append 一次"放大了步数；改成"批量读 → 一次写 NOTES → 一次写 REPORT"
  可把 13 步降到约 4-5 步（预计 45k→13k、29s→10s）。
- append 被拒后模型会改成整文件重写；提示词可明确"append 前先 read 当前文件"，
  或让同会话写过的文件在 tracker 里直接算 clean（当前已通过注册表复用实现）。

## 6. 编排层覆盖情况

已覆盖：运行中 send、interrupt（含首轮）、resume、续跑、close、磁盘重开、
完成 notice、mailbox 投放。

未覆盖（后续补真机/测试）：续跑 turn 运行中再 interrupt、close 打断运行中、
resume 对 running 的 no-op、pool full、歧义/未知 target、worktree 隔离下的续跑、
跨**进程**（新 OS 进程）resume（本次是同进程新 runner）。

## 7. 建议（按优先级）

1. **配置/提示词**：子代理任务改成批量读取；`max_tool_iterations` 不要用 10；
   子指令写明"resume 后以 transcript 为状态，不重复已完成步骤"。
2. **代码（小）**：中断时补记 in-flight usage；sidecar 已有 workspace 兜底后，
   跨进程 resume 补一条真机用例。
3. **代码（中）**：评估子代理是否应继承父的迭代上限（Codex 子线程无上限），
   或给子代理一个独立、更高的默认值。
4. **测试**：把本次真机脚本沉淀为可选真机用例（key 缺失时 skip），覆盖第 6 节
   的未覆盖边界。

## 8. 迭代上限：调研结论与修正

**问题**：真机第一次跑子代理时，turn2 在 13 步撞上 `max_tool_iterations` 而没写完文件。
追查发现子代理**继承了父的** `ai.max_tool_iterations`（示例配置里是 10），而计划回合
本身是无限的（`-1`）。这与调研结论矛盾。

**调研（本地源码 + 官方文档）**：

| 实现 | 子代理迭代上限 | 终止/兜底 |
|---|---|---|
| Codex | **无**（`run_turn` 是裸 loop，`core/src/session/turn.rs:490`；子线程同） | `needs_follow_up=false`、token 上限+压缩、用户中断 |
| opencode | **默认无**（`steps` 可选，内置 general/explore 未设；`packages/core/src/session/runner/llm.ts:249`） | 模型停止、overflow 压缩、abort |
| dsh | **无**（`packages/core/agent-loop/src/agent.ts:341-422`） | maxTokens、maxDepth、并发槽 |
| Claude Code | **默认无**，可显式 `maxTurns` | partial output + resume、stall window |
| gemini-cli（反例） | **30 turns + 10min**（`agents/types.ts:51,56`） | 硬上限 |

结论：**"子代理默认不设迭代上限"是主流**；兜底靠上下文/token 上限 + 自动压缩 +
用户中断，而不是步数。

**我们的修正**（最小改动）：

- 新增 `ai.subagent_max_tool_iterations: int = 0`，`0` = 不设上限（默认），正数 = 显式上限。
- `SubAgentRunner._subagent_settings()` 把子代理的 `max_tool_iterations` 设为
  `-1`（0 时）或配置值；**不再继承父的 cap**。
- `TurnPolicyConfig.from_ai_config()`：`<= 0` 映射为 `-1`（"无上限"哨兵），
  不再 clamp 到 1（旧的 clamp 会把"无限"变成"一步"）。
- `config.example.yaml` 的 `max_tool_iterations` 由 10 改为 100（产品默认），并新增
  子代理字段说明。
- 测试：`tests/test_subagent_limits.py`（默认无上限、显式上限生效、父 3 步不影响子）。

## 9. 第二轮真机：7 个边界用例（全部通过）

| 用例 | 结果 | 证据 |
|---|---|---|
| 运行中再次 interrupt（续跑 turn2 中） | 通过 | `previous=running → interrupted`，26ms，无 notice |
| close 打断运行中 | 通过 | `closed=True`、行 `cancelled`、handle 已删、4ms、无 notice |
| resume running no-op | 通过 | 返回 `status=running`，turn 未被重启 |
| pool full | 通过 | 第 2 个 spawn `agents_exhausted`；对忙 agent 的 send 仍 `queued` |
| 歧义 target | 通过 | 同名两个 agent → `ambiguous_target` + 两个候选 id |
| worktree 隔离下续跑 | 通过 8/8 | worktree 创建、interrupt+resume 后保留、产物落在 worktree、close 提交并删除、分支保留、磁盘可再 resume |
| 跨 OS 进程 resume | 通过 6/6 | 进程 A 结束后，新进程 B 无 handle、从 sidecar+session 恢复、续跑成功（`turn_count=2`） |

脚本：`/tmp/realrun_agent_edges.py`、`/tmp/realrun_agent_worktree.py`、
`/tmp/realrun_agent_crossproc.py`；日志 `/tmp/real-agent-{edges,worktree,crossproc}.log`；
结果 `/tmp/real-agent-{edges,worktree,crossproc}-result.txt`。

**过程中修的一个真实缺口**：同名 agent 之前一律回 `unknown_agent`，模型无法区分
"拼错"和"重名"。新增 `SubAgentRunner.resolve_target()`（ok/unknown/ambiguous），
四个工具统一返回 `ambiguous_target` 并列出候选。
