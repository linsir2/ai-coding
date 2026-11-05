# 长任务审计：工具被拒、写缓存、下载、压缩与前端

> 运行时间：2026-11-05；基线 `f8461c6`。
> 触发原因：用户反馈"工具调用容易全被拒绝（写缓存、写工具、下载），压缩后上下文容易失效"。

## 0. 方法与边界

本轮有两部分：

1. **确定性复现**（无模型凭据时）：`ScriptedEngine` 发出真实工具调用，其余全是真的
   `AgentLoop`/`ToolDispatcher`/`PermissionGate`/`Sandbox`/`WriteCoordinator`/
   `EffectLedger`/`TaskJournal`。
2. **真机**（后补 `DASHSCOPE_API_KEY`）：`qwen3.7-flash` + 真实引擎 + 真实工具，
   `process_sandbox.mode=warn`（本机无 bwrap），AutoApprove；覆盖 3 轮、23 次工具
   调用，另做一次强制压缩诊断。

前端只跑了 `bun test` + `tsc`，另外在安装 `tmux` 后做了真实 TUI 验证
（`bwrap`/`tmux` 起初缺失，后补装：bubblewrap 0.9.0、tmux 3.4）。

复现脚本（未入库）：`/tmp/aic-audit/*.py`、`/tmp/aic-live/*.py`；证据 JSON 与日志
在同目录。

全量后端 `pytest` 通过（仅 bwrap 相关用例 skip）；前端 571 pass、`tsc` 干净。

## 1. 结论先行

用户感知的"工具老被拒绝"，在本轮复现到 **3 条确定性原因**：

1. `todo_write` 的完成门禁在"先写文件、后建 todo"时**误判为缺少回执**（P0，已修复）。
2. `todo_write add` **静默忽略 `status`**，计划状态与模型认知不一致（P0，已修复）。
3. 文件工具与进程沙箱的**可写面不一致**：`~/.cache` 能用 bash 写，不能用
   `write` 写（P1，未改）。

另外两条环境/策略因素会放大"全被拒"的观感：默认 `process_sandbox.mode=enforce`
且机器没有 `bwrap` 时**所有 bash 都不可用**；`write/edit/bash` 默认都要人工批准，
拒绝一次就表现为工具失败。

压缩在真机下**可以正常工作**：40→3 条消息，L1 删 33、L4 删 4，摘要生成成功，
文件回执与读账本保留，压缩后继续读文件/列 todo 正常。但发现一个配置陷阱：
`max_context_tokens` 低于固定 prompt/tool 开销时会反复压缩，详见 P1-6。

## 2. 问题清单

### P0-1 `todo_write`：先写文件再建条目，完成被误拒（已修复）

| 项 | 内容 |
|---|---|
| 状态 | **已修复**：`CompletionGate.receipt_floor` 统一供 `update_plan`/`todo_write`
  使用；新增 `write → add → complete` 回归用例；真机已复验通过 |
| 症状 | `write a.txt` → `todo_write add(files=["a.txt"])` → `update completed` → `completion_unverified: 缺少回执: a.txt`，尽管文件刚写过 |
| 证据 | `/tmp/aic-audit/focused.py` case_order；`/tmp/aic-audit/restart.py` |
| 根因 | `_add` 无条件把 `started_seq` 设为 `effects.sequence + 1`（[todo_write_tool.py](file:///workspace/ai-coding/ai_coding/tools/todo_write_tool.py#L216-L221)）；`CompletionGate` 要求回执 `seq >= started_seq`（[completion.py](file:///workspace/ai-coding/ai_coding/core/completion.py#L70-L74)）；`_update` 没有 `update_plan` 那样的"回执下界"修正 |
| 对照 | `update_plan` 有 `_receipt_floor`，同样的"先写后完成"能通过（[update_plan_tool.py](file:///workspace/ai-coding/ai_coding/tools/update_plan_tool.py#L219-L234)） |
| 影响 | 模型常见的"先改代码、再补 todo"会连续失败两次，条目被置 `blocked`；用户读起来就是"写工具被拒绝" |
| 建议 | 把 `_receipt_floor` 逻辑复用到 `todo_write._update`（有回执时把边界下调到最早回执），并加一条"write → add → complete"回归用例 |

### P0-2 `todo_write add` 静默忽略 `status`（已修复）

| 项 | 内容 |
|---|---|
| 状态 | **已修复**：`add` 接受 `pending`/`in_progress`；`completed` 与 `skipped` 返回可操作错误；新增两条用例 |
| 症状 | `add(status="in_progress")` 返回成功，但条目仍是 `pending` |
| 证据 | `/tmp/aic-audit/restart.py`：保存前 `('1','pending',2,...)` |
| 根因 | `_add` 只读 `content/depends_on/files/verify`，不读 `status`（[todo_write_tool.py](file:///workspace/ai-coding/ai_coding/tools/todo_write_tool.py#L162-L221)）；schema 里 `status` 的说明写着"for 'update'"，但仍在同一个对象里 |
| 影响 | 模型以为已开始；计划卡显示 pending；同时 `started_seq` 已在 add 时设定，进一步触发 P0-1 |
| 建议 | 二选一：add 接受 `in_progress`（其余拒绝），或对 add 携带 `status` 给出可操作错误；schema 按 action 拆分 required/allowed |

### P1-1 `write` 覆盖已存在文件必须先 `read`（已修复提示）

| 项 | 内容 |
|---|---|
| 状态 | **已修复提示**：`write`/`edit` 共用 `tools/stale_read.py`；提示现在明确"先读，或新文件无需读，编辑用 edit" |
| 症状 | `write exists.txt` → `cannot write ... you must read the file first (stale-read protection)` |
| 证据 | `/tmp/aic-audit/friction.py` write_existing |
| 根因 | 明确的 stale-read 保护（[write_tool.py](file:///workspace/ai-coding/ai_coding/tools/write_tool.py#L160-L176)） |
| 影响 | 长任务/重启/resume/子代理中，模型常直接整文件覆盖；每次被拒要重读一轮。压缩后模型看不到旧内容，更容易触发 |
| 建议 | 保留保护，但把提示改成"先 read 或改用 edit"；对"文件在上一轮已读过、只是被压缩"的情况，可在提示里带文件版本与行数 |

### P1-2 `edit/write` 在 bash 或外部改动后要求重读（已修复提示）

| 项 | 内容 |
|---|---|
| 状态 | **已修复提示**：共用 `stale_message`，明确"上下文里的副本是旧版本"，并保留 `stale_read` 码 |
| 症状 | `read f.txt` → `bash: echo changed > f.txt` → `edit f.txt` → `file has been modified externally; re-read it first` |
| 证据 | `/tmp/aic-audit/friction.py` stale_read |
| 根因 | 文件版本（`mtime:size:inode`）与读账本不一致即拒绝（[edit_tool.py](file:///workspace/ai-coding/ai_coding/tools/edit_tool.py#L180-L189)、[write_tool.py](file:///workspace/ai-coding/ai_coding/tools/write_tool.py#L167-L176)） |
| 影响 | 后台 job / bash 写文件是长任务常态；每个此类改动都会让后续 edit 失败一次 |
| 建议 | 安全语义不变；可以自动在工具结果里附带"当前文件前 N 行/版本"，减少一次往返。若 `old_string` 仍唯一命中，可考虑允许并提示，但不建议默认放开 |

### P1-3 文件工具不能写 `process_sandbox.writable_paths`（如 `~/.cache`）（已修复提示）

| 项 | 内容 |
|---|---|
| 状态 | **已修复提示**：`SandboxViolation` 对 cache 路径追加"缓存已重定向到 `<ws>/.ai-coding/cache`，写到那里"；不扩大文件工具边界 |
| 症状 | `write ~/.cache/x` 被拒（工作区外，需要授权）；进程层按配置会把 `writable_paths` 绑定为可写（本机无 bwrap，未实测） |
| 证据 | [config/models.py](file:///workspace/ai-coding/ai_coding/config/models.py#L248-L256)（`writable_paths` 只描述进程层）；[sandbox.py](file:///workspace/ai-coding/ai_coding/security/sandbox.py#L65-L68)（文件工具根 = workspace + extra roots）；[world.py](file:///workspace/ai-coding/ai_coding/server/world.py#L221-L225) |
| 影响 | 用户说的"写缓存被拒"：进程层可写、write 不能写，模型会反复试 `write` 到缓存目录 |
| 建议 | 二选一：把 `writable_paths` 同步进 `Sandbox.extra_roots`（注意与 read denylist 的重叠），或在 `write` 的拒绝提示里明确"缓存请用 bash 或 `<ws>/.ai-coding/cache`" |

### P1-4 默认 `enforce` + 无 `bwrap` ⇒ 所有 bash `sandbox_unavailable`（已修复诊断）

| 项 | 内容 |
|---|---|
| 状态 | **已修复**：`build_process_sandbox` 在 enforce 且机制不可用时打一条 warning，点名原因与两个出口（装 bwrap / 改 warn） |
| 症状 | `bash echo hello` → `error: sandbox.backend.missing`，`error` 码为 `sandbox_unavailable`；`write` 正常 |
| 证据 | `/tmp/aic-audit/focused.py` case_sandbox_enforce；[jobs.py](file:///workspace/ai-coding/ai_coding/core/jobs.py#L295-L344)、[bash_tool.py](file:///workspace/ai-coding/ai_coding/tools/bash_tool.py#L619-L629) |
| 影响 | 在无 bwrap/无 user namespace 的机器上，安装、下载、curl、git 全部不可用；用户观感就是"工具全被拒" |
| 建议 | `config init` / 启动时探测 bwrap 并给出一次性明确诊断（"bash 将被拒绝，因…；设 `process_sandbox.mode: warn` 或安装 bwrap"）；当前只有配置注释说明 |

### P1-5 `write/edit/bash` 默认 WARN，拒绝一次就是工具失败（已修复配置项）

| 项 | 内容 |
|---|---|
| 状态 | **已修复**：新增 `security.auto_approve_tools`（用户层配置，默认空）；命中时走 standing ALLOW，红线/显式 deny 仍优先；项目层被过滤，不能自我授权 |
| 症状 | 审批回调返回 NO 时，bash 与 write 都返回 `not_approved` / "用户未批准这次调用" |
| 证据 | `/tmp/aic-audit/run_audit.py` S3；[permission_gate.py](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L1-L8) |
| 影响 | 长任务里模型连续写文件/跑命令，审批面板频繁出现；漏点、超时或误拒都会被模型读成"工具坏了" |
| 建议 | 产品层提供"本次任务内一律允许 write/edit/bash"的一键授权；面板显示"还剩 N 个同类请求"，避免逐个点 |

### P1-6 `max_context_tokens` 低于固定开销时会反复压缩（已修复）

| 项 | 内容 |
|---|---|
| 状态 | **已修复**：`ContextManager` 记录"压缩后没有实质变短"的输入规模；`should_compact` 在该规模以下不再重开，直到历史继续增长（滞回）。真机复验：同样的 3000 预算从 3 分钟未结束变为正常完成，压缩 40→3（`reason=compacted`），压缩后回合 7.5s |
| 症状 | 把 `ai.max_context_tokens` 设成 3000（低于 system prompt + 工具 schema 的固定开销）后，第一轮 3 分钟仍未结束；工作区在几分钟内出现 3 个 `summary_*.txt`，进程持续做压缩 |
| 证据 | `/tmp/aic-live/compact.log`；进程被手动停止；同样任务在 `max_context_tokens=0`、`max_messages=12` 下压缩 40→3、正常完成（`/tmp/aic-live/diag.log`） |
| 根因 | 预算按"整个请求"估算，固定开销已超过预算，`_should_run` 每步都为真；启动日志只有一条 warning（"far below model's declared window"），不会拒绝该配置 |
| 影响 | 用户把预算调小以"省上下文"时，反而进入压缩抖动：极慢、频繁摘要、上下文反复重写 |
| 建议 | 启动时计算固定开销下限：低于它直接拒绝或自动抬到下限并明确提示；压缩后若"压缩前后 token 几乎不降"应短路并告警 |

### P2-1 两次完成失败后条目变 `blocked`（已修复提示）

- 状态：**已修复提示**：错误文案明确"仍可补回执后再次完成"，`data.recoverable=True`；状态机不变。
- 症状：无回执的条目第二次 `completed` 后返回 `completion_blocked`，状态置 `blocked`。
- 证据：`/tmp/aic-audit/focused.py` case_two_failures_blocked；补写文件后仍可完成。
- 影响：模型看到 `blocked` 可能放弃该条目；实际上它是可恢复的。建议提示里明确"补回执后仍可完成"，或不要改状态、只累计计数。

### P2-2 前端：`merge_skipped` 的 tone 与成功语义不一致（已修复）

- 状态：**已修复**：`merge_skipped`/`nothing_to_merge` 改为 `denied` 色调（⊘），不再画成红色失败。
- 原问题：`MergeAgentTool` 把 `merge_skipped` 视为成功（`_SUCCESS`），但
  [toolmeta.ts](file:///workspace/ai-coding/ai_coding/frontend/src/util/toolmeta.ts) 的
  `FAILURES` 把它标成 `failed`。

### P2-3 手动压缩可能"什么都没做"且没有可见原因（已修复）

- 状态：**已修复**：`compact_now` 报告新增 `reason`（`compacted` /
  `nothing_to_compact` / `summary_failed` / `busy` / `empty` / `no_context` /
  `error`），`/compact` 对非成功原因打印一行中文说明。
- 原问题：第一轮真机（32 条消息，`max_messages=30`）`compact_session` 返回
  `removed=0`，headless 驱动里看不到原因；诊断轮在 `max_messages=12` 下正常压缩
  40→3。

## 3. 真机结果（qwen3.7-flash）

| 场景 | 结果 |
|---|---|
| 第 1 轮 10 次工具调用（write/read/edit/bash 缓存/bash 下载/todo 两次 add+complete/update_plan） | 10/10 成功；`write → todo add → complete` 通过（P0-1 修复生效） |
| 第 2 轮 6 次新调用（list/read/bash 检查/read/read） | 全部成功 |
| 第 3 轮 7 次新调用（压缩后 read/list/bash/read/read） | 全部成功 |
| 缓存目录写入 | `.ai-coding/cache/npm/probe.txt` 写入成功，bash 回执 `observed` |
| 下载 | `dl.html` 577 字节，bash 回执 `observed` |
| 回执 | `notes.txt` 来自 edit（`exact`），缓存/下载来自 bash（`observed`） |
| 强制压缩（`max_messages=12`） | 40→3 条；L1 删 33、L4 删 4；摘要生成成功；`CompactionEnded` 事件完整 |
| 压缩后回执/读账本 | 回执仍为 1 条；读账本保留；压缩后 read/list 正常 |
| `CompactionFailed` | 未出现（诊断轮） |

**确定性复现（脚本化模型）补充验证**

| 场景 | 结果 |
|---|---|
| `write` 新文件、`edit` 已读文件 | 成功 |
| bash 写工作区内缓存目录 | 成功 |
| `curl` 下载到工作区（sandbox off） | 成功 |
| bash 改文件后生成 `observed` 回执，todo 完成 | 成功 |
| 会话保存/加载 | `startedSeq`、`effectsVersion=2`、`fileReceipts` 全部保留 |
| 前端 `bun test` / `tsc` | 571 pass / 0 fail / 无类型错误 |

**未能验证**：交互式 TUI 的审批面板与 "working" 状态、bwrap 下的网络墙与
`writable_paths` 行为（本机无 bwrap）。

## 4. 前端问题记录

1. 静态检查未发现类型/单测问题；`sandbox_unavailable`、`not_approved`、
   `completion_unverified`、`conflict`、`verify_failed` 等都有中文标签。
2. 工具卡正文对沙箱失败显示原始 reason（如 `sandbox.backend.missing`），标签正确但正文
   偏技术；建议正文用"沙箱不可用（缺少 bwrap）"，reason 放详情。
3. 审批面板未在真机验证：如果一次任务里连续出现多个 write/bash，需要确认"是否支持
   批量/会话级批准"，否则会被用户感知为"一直拒绝"。
4. `merge_skipped` 色调与成功语义不一致（P2-2）。

## 5. 修复状态

| 项 | 状态 |
|---|---|
| P0-1 完成回执下界 | 已修复 + 用例 + 真机复验 |
| P0-2 add 状态契约 | 已修复 + 用例 |
| P1-1 / P1-2 stale-read 提示 | 已修复提示（安全语义不变） |
| P1-3 缓存路径提示 | 已修复提示（不扩大文件工具边界） |
| P1-4 enforce 无 bwrap 诊断 | 已修复（启动 warning） |
| P1-5 逐次审批 | 已修复（`security.auto_approve_tools`，项目层不可授予） |
| P1-6 低预算压缩抖动 | 已修复（滞回，不再每步重压） |
| P2-1 blocked 可恢复提示 | 已修复提示 |
| P2-2 merge_skipped 色调 | 已修复 |
| P2-3 压缩 no-op 原因 | 已修复（`reason` + 命令输出） |

**仍未验证**：鼠标拖选复制（tmux 无法注入鼠标选区；无选区 Ctrl+C 已验）。

## 6. 安装 bwrap/tmux 后的真实复验

环境：Ubuntu 24.04，`bubblewrap 0.9.0`、`tmux 3.4`；bwrap 选中 `no-proc` 层
（`--proc` + `--unshare-pid` 在当前 host 不可用）。

### 6.1 沙箱矩阵（enforce + network deny）

| 场景 | 结果 |
|---|---|
| 工作区内 `bash: echo hi > ok.txt` | 成功 |
| `bash` 写 `~/.cache/...` | 成功（`writable_paths` 默认 `~/.cache`） |
| `curl https://example.com`（deny） | 失败，提示"不是主机 DNS 故障，需要 `network_permissions=inherit`" |
| 同一 curl + `network_permissions="inherit"` | 成功，`577 dl2.html` |
| `write` 工具写 `~/.cache/...` | `sandbox_violation`，提示缓存重定向到 `<ws>/.ai-coding/cache` |

`tests/test_process_sandbox.py` 全量通过（真实 bwrap 用例不再 skip）。

### 6.2 新发现并修复：kill 不能结束 no-proc job 的子进程

- 症状：真实 bwrap 下运行 `test_a_sandboxed_background_job_can_be_killed` 失败
  （`7 == 2`）：面板已报停止，沙箱内 shell 仍在写 heartbeat。
- 根因：`no-proc` 层没有 `--unshare-pid`，且 `--new-session` 让 shell 离开
  原进程组，`killpg(job.pid)` 打不到它。
- 修复：`JobRegistry.kill` 先按 `/proc/*/status` 的 PPid 收集后代，再依次
  SIGTERM/SIGKILL（`core/jobs.py` 的 `_descendants` / `_signal_tree`）；新增
  `test_descendants_walks_the_proc_ppid_tree`。修复后该用例通过。

### 6.3 交互式 TUI（tmux）

| 场景 | 结果 |
|---|---|
| 写文件触发审批 | 面板显示"△ Permission required / 写入文件 · 路径 / Allow once · Always allow · Reject · Stop this turn" |
| 按 `a`（Always allow） | 写入执行，随后 read 返回 `hello`，最终答复正确 |
| 长命令运行中 | 状态栏显示 `Working…`，工具卡显示 `正在运行命令`；结束后回到 `Ask anything…` |
| 运行中按 Esc | 当前 turn 结束并回到输入框；后台 job 继续运行，完成通知随后到达（符合"Esc 只取消当前 turn"） |
| 无选区 Ctrl+C | 退出 TUI，无残留进程 |
| 鼠标拖选复制 | 无法在 tmux 中注入选区，未自动验证 |

### 6.4 新 UI 观察（P2-4，未修）

- 审批面板出现时，工具卡已显示"正在运行命令"（Spinner）。实际命令尚未获批、
  也未启动。建议把"等待审批"作为独立状态（或暂停 Spinner），否则用户会以为
  命令已经在跑。

