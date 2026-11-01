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
