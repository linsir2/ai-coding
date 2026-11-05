# 第二轮真机问题 · 修改方案（2026-11-05）

本文只针对**尚未修复**的问题（R2-3～R2-10，以及 R2-2 的残余部分）。
每条给出现场证据、代码位置、方案（含取舍）、验证方式。

已修复并验证的四条见
[第二轮审计](agent-runtime-audit-2026-11-05-round2.md) §4。

---

## 优先级与依赖

| 级别 | 编号 | 主题 | 依赖 |
|---|---|---|---|
| P0 | R2-2r | 压缩闸门在 checkpoint 后用估算值，JSON 工具输出被高估 ~4x | 无 |
| P0 | R2-13 | `read` 重复返回全文，模型读自己的 trace 直到超时 | R2-2r |
| P1 | R2-3 | 沙箱私有 `/tmp`：越界写返回 `rc=0`，宿主无文件 | 无 |
| P1 | R2-6 | MCP 进度回调是同步函数，SDK 却 `await` 它 | 无 |
| P1 | R2-7 | npx MCP server 长时间 `connecting`，文档示例不可用 | 无 |
| P2 | R2-4 | `tool_search` 不在门禁允许表，被当“未知工具”审批 | 无 |
| P2 | R2-5 | job 工具是延迟工具；no-proc 下 `ps` 必然失败 | 无 |
| P2 | R2-8 | 内置工具面每步 23–31k 字符 | 无 |
| P3 | R2-9 | `grep … \| head` 之类管道被记为失败 | 无 |
| P3 | R2-10 | 模型连续多步复述同一计划 | 无 |
| P1 | R2-14 | 验收门只查 `build`/`pytest`，脚手架也算“完成” | 无 |

---

## R2-2r（P0）checkpoint 之后闸门改用估算值，JSON 被高估

### 证据

复跑 Skill 场景（`/tmp/aic-r2/logs/skill-verify.log`）：

```
START 5  END 5  msgs 26 -> 10  est 16357 -> 15371
START 9  END 9  msgs 36 -> 20  est 53027 -> 6306
START 10 END 10 msgs 38 -> 4   est 53081 -> 791
START 12 END 12 msgs 44 -> 10  est 135011 -> 2392
START 13 END 13 msgs 47 -> 5   est 151002 -> 1451
START 14 ...
```

14 次迭代 6 次压缩；`est` 的跳变（9.7k → 53k → 135k）与 provider 实测的
`prompt_tokens`（约 10–30k）相差 4 倍以上。

### 根因

[context_compression.py](file:///workspace/ai-coding/ai_coding/core/context_compression.py#L1457-L1466)：

```python
gate_observed = 0 if view is not None else observed_tokens
```

一旦装过 checkpoint，闸门就**丢掉 provider 的实测值**，改用
`_history_tokens()`（字符数 ÷ 4）。对 JSON/日志类工具体，这个估算会高估
3–4 倍，于是每次新读入一个几十 KB 的文件都“超过 16k 上限”。

这条分支本来是为了修“装完 checkpoint 后 observed 还是旧的大数”，但它把
**单位**和**时序**两件事混在了一起。

### 方案

用“这份 observed 是否描述当前请求”来判断，而不是用“有没有 checkpoint”：

1. 在 `ContextManager` 记一个请求序号/形状指纹：每次 `compress_if_needed`
   返回前，记下 `self._observed_describes_view = True`；任何装入
   checkpoint 的路径先置 `False`。
2. 闸门取数：
   ```python
   observed = int(observed_tokens) if observed_tokens > 0 else 0
   tokens = observed if observed > 0 and self._observed_describes_view else _history_tokens(effective)
   ```
3. 估算路径加一个“工具输出折扣”：`_history_tokens` 对 `role == "tool"` 的
   正文按 3 字符/token 计（而不是 4），并对已 L3 持久化的信封只计信封。
   这是兜底，不让估算单独决定一次付费摘要。

更保守的替代（改动更小、立刻可做）：**当 observed 与估算都可用时取较小者**。
依据是 provider 的计数是实测下限，估算高估时不该由它单独开闸。

### 风险

- 取 observed 可能在“observed 描述的是旧请求”的那一步漏压一次；`max_tokens`
  预留 16k 的余量仍然在，且下一轮 observed 会刷新。
- 折扣系数需要一组离线基准钉住，避免把真正的超限也压下去。

### 验证

- 新增单测：checkpoint 存在 + observed 低于 ceiling → `should_compact` 为
  False；observed 高于 ceiling → True。
- 复跑 Skill 场景：14 次迭代的 `CompactionStarted` 应 ≤ 2，回合应在 300s
  内自然结束。

---

## R2-13（P0）同文件重复读取仍返回全文

### 证据

- 复跑 Skill 第二回合：22 次检索里 14 次是只读，模型反复读
  `turn-2.jsonl`（自己正在写的 trace），直到 300s 超时；最终
  `prompt_tokens` 262,646。
- 搜索门禁确实响了（第 57 条 user 消息：`[search-gate] … 最近 4 次检索都在
  重复已经看过的内容`），但已经是第 17 次迭代。

### 根因

`read` 对“同一区间的第二次读取”仍然返回全文。R2-12 的修复只让**统计**
不再把它算作新证据，没有减少**字节**。在一个“诊断压缩”的任务里，模型读的
正是自己的 trace —— 读取本身让 trace 变大，于是下一轮又有“新内容”。

### 方案

在 `read` 工具层做**内容级去重**（而不是只做统计）：

1. `ToolContext` 增加 `read_receipts`（`path -> [(start, end, version)]`），
   由 loop 从 `session.read_ledger` 传入（已有数据结构）。
2. `read` 在返回前判断：请求区间被已读区间**完全覆盖**且文件 `version`
   未变 → 返回
   ```
   [未返回正文：第 3-17 行在本回合已读过，文件未变化]
   ```
   而不是正文。
3. 部分重叠时只返回**缺口**行，并在首行说明跳过了哪些行。
4. 文件 `version` 变化（`mtime:size:ino`）时照常返回全文 —— 这正是
   “trace 在增长”的情形，但配合第 3 条的缺口返回，增量会很小。

### 风险

- 模型可能因为“看不到正文”而重复请求；所以回执里必须写明**为什么**以及
  “文件已变化时请指定 offset 读缺口”。
- 与 stale-read 保护共用同一份 ledger，注意写入/编辑后要清空对应条目
  （`note_effect` 已有失效逻辑）。

### 验证

- 单测：同区间二次读 → 无正文；文件 version 变化 → 返回正文；部分重叠 →
  只返回缺口。
- 复跑 Skill 场景：第二回合应在 300s 内结束，且 trace 文件不再被整份重读。

---

## R2-3（P1）沙箱私有 `/tmp` 的“假成功”

### 证据

enforce + deny 与 enforce + inherit 都复现：

```
echo escape > /tmp/aic-r2/outside-deny.txt; echo rc=$?
→ [tool<] bash OK 210ms  rc=0
宿主: ls: cannot access '/tmp/aic-r2/outside-deny.txt': No such file or directory
```

模型据此总结“文件边界未拦截”。

### 根因

[process_sandbox.py](file:///workspace/ai-coding/ai_coding/security/process_sandbox.py#L147-L183)
两个构造层都带 `--tmpfs /tmp`。这是**故意**的（宿主 `/tmp` 不能进沙箱），
但工具结果没有告诉模型这件事。

### 方案

**A. 结果级提示（推荐，默认开启）**

`shell_parse.command_views()` 已经能给出每条命令的重定向目标
（`CommandView.redirects`）。在 `BashTool.execute` 返回前：

1. 用 `command_views(command)` 收集所有 redirect 目标；
2. 展开 `~` 与相对路径后，判断目标是否落在
   `Sandbox.live_roots()` ∪ `process_sandbox.writable_paths` 之内；
3. 若有目标落在 `/tmp` 或 `/var/tmp` 等被 tmpfs 覆盖的目录，追加一段：

   ```
   [sandbox] /tmp 是沙箱私有层（--tmpfs /tmp）：这次写入只存在于本次调用，
   宿主看不到，下一次 bash 也不会保留。需要跨调用持久化请写到工作区，
   例如 .ai-coding/tmp/。
   ```

4. 同一回合只提示一次（避免每条命令都刷屏）。

**B. 可选持久化（配置项）**

新增 `process_sandbox.persistent_tmp: bool = false`。为 true 时把
`--tmpfs /tmp` 换成 `--bind <workspace>/.ai-coding/tmp /tmp`（每次调用前
`mkdir -p`）。这样 `/tmp` 跨调用可见、可在宿主检查，代价是同一 workspace
的所有 sandboxed 进程共享这个目录。

**取舍**：A 零风险、只改提示；B 改变隔离语义，必须默认关闭并写进
`config.example.yaml` 的注释（说明“两个并发会话会共享 workspace 的 tmp”）。

### 验证

- 单测：`echo x > /tmp/y` 在 mock backend 下产生提示；写工作区内不产生。
- 真机：S1 场景重跑，模型总结应变成“被沙箱挡住”，而不是“边界未拦截”。

---

## R2-6（P1）MCP 进度回调是同步函数

### 证据

调用 `progress-local/slow_task(steps=5)`：

```
ERROR [root] Progress callback raised an exception: 'NoneType' object can't be awaited  （×5）
[tool<] mcp_call OK 262ms  did 5 steps
```

### 根因

[.venv/.../mcp/shared/session.py:416](file:///workspace/ai-coding/.venv/lib/python3.14/site-packages/mcp/shared/session.py#L416)
是 `await callback(...)`；而
[mcp.py:942](file:///workspace/ai-coding/ai_coding/core/mcp.py#L942) 的
`_progress` 是 `def`，返回 `None`。

### 方案

```python
async def _progress(progress: float, total: float | None = None, message: str | None = None) -> None:
    ...
```

一行改动。`live.emit` 本身是同步回调，放进 async 函数里没问题。

同时补一条真机测试：`tests/test_mcp_progress.py` 目前按环境跳过
（`AI_CODING_MCP_STDIO=1` 才跑）；本轮沙箱里可以把它打开跑一次，确认
`ToolChunk(stream="progress")` 真的到达 observer。

### 验证

- 单测（伪 session）：回调被 `await` 且 emit 了 chunk。
- 真机：`progress-local/slow_task` 不再打 ERROR，observer 收到 5 个
  `stream="progress"` 的 chunk。

---

## R2-7（P1）npx MCP server 卡在 connecting；示例不可用

### 证据

- `everything-npx` 在 t+0…27.5s 始终 `connecting`，`detail` 只有“正在连接…”，
  模型 50s 时调用得到 `MCP server 'everything-npx' is still connecting.`
- 同期 `broken`（`/bin/false`）2.5s 内 `failed: Connection closed`。
- 直接跑 `npx -y @modelcontextprotocol/server-everything` 4.8s 就打印
  `Starting default (STDIO) server...`；但 MCP `initialize` 在 10–25s 内无响应。
- `docs/how-to-run.md` §R7 的示例正是这个包。

### 根因（两部分）

1. **可见性**：`connecting` 行不显示子进程 stderr，也不显示已等待时长；
   只有失败后才拼 `stderr_tail`（[mcp.py:1597-1599](file:///workspace/ai-coding/ai_coding/core/mcp.py#L1597-L1599)）。
2. **示例质量**：该包的 stdio 启动器在本环境不能完成握手（未知原因，
   需要单独定位）；文档把它当成“能用的例子”。

### 方案

**A. 连接期可见（小改动）**

`servers()` 的 `connecting` 行：

- `detail` 改为 `正在连接（第 N 次，已等待 Xs）`；
- 若 `stderr_tail()` 非空，追加最后一行（截断 120 字符）。

**B. 首步等待（中等改动）**

`MCPManager` 增加 `async def wait_ready(timeout: float)`：在 `open_all()`
之后、第一次组装请求之前等最多 `min(3s, startup)`，让 catalog 在第一个模型
步骤就能列出已就绪的 server。避免“模型第一眼看不到已连接的工具”。

**C. 示例替换（文档）**

`docs/how-to-run.md` 与 `config.example.yaml` 的 npx 示例改为
**本仓库自带的本地 stdio server**（`scripts/mcp_echo_server.py`，本轮已验
9ms 调用成功），并把 npx 写法降级为“需要能联网且包版本可用”的可选示例。

**D. 定位 npx 包（调研项）**

单独起一个 issue：用 `AI_CODING_MCP_STDIO=1` 跑
`tests/test_mcp_stdio.py`，再抓一次 `initialize` 的 stdout/stderr 原始字节，
确认是协议版本、Node 版本还是包自身启动器的问题。在结论出来前不改默认
timeout（npx 冷启动实测 ~27s，压到 30s 会误杀）。

### 验证

- 单测：`connecting` 行在 mock owner 有 stderr 时显示片段。
- 真机：`echo-local` 在第一个模型步骤就出现在 catalog 里；`everything-npx`
  失败时 detail 含原因。

---

## R2-4（P2）`tool_search` 被当成未知工具

### 证据

三次 `tool_search` 都触发
`ToolApprovalRequested reason="unknown tool 'tool_search' requires confirmation"`，
工具本身随后执行成功。

### 根因

[permission_gate.py:331-411](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L331-L411)
的 `_allow_tools` 里没有 `tool_search`；它落到
[第 582 行](file:///workspace/ai-coding/ai_coding/security/permission_gate.py#L582)
的“未知工具 → WARN”。

### 方案

1. `_allow_tools` 加 `"tool_search"`（它是纯本地、只读、`mutates_world=False`，
   见 [tool_search_tool.py:40-41](file:///workspace/ai-coding/ai_coding/tools/tool_search_tool.py#L40-L41)）。
2. 加一条**结构性**测试：遍历内置注册表里所有
   `mutates_world=False and origin=="local"` 的工具，断言它们在
   `PermissionGate().check(name, {})` 下不是 WARN。这样下一个新工具忘记加
   白名单时，测试会先失败，而不是等真机弹窗。

### 验证

单测 + 真机日志里不再出现 `unknown tool 'tool_search'`。

---

## R2-5（P2）job 工具延迟 + no-proc 下 `ps` 必然失败

### 证据

模型先用 `ps aux | grep sleep` 想确认后台任务，得到
`Error, do this: mount -t proc proc /proc`；之后才用 `tool_search` 加载
`job_list/job_kill` 并成功。

### 根因

1. `job_list`/`job_output`/`job_kill` 在
   [builder.py:591-605](file:///workspace/ai-coding/ai_coding/tools/builder.py#L591-L605)
   的 `DEFERRED_TOOL_NAMES` 里，模型看不到它们，只看到 bash 描述里提到的名字。
2. no-proc 层用 `--tmpfs /proc`，`ps`/`top`/`pgrep` 必然失败，但没有任何
   提示把模型导向 `job_list`。

### 方案

1. **后台启动回执加一句**（改动最小）：
   [bash_tool.py:631-637](file:///workspace/ai-coding/ai_coding/tools/bash_tool.py#L631-L637)
   已有 `inspect it with job_output`，补成
   `（若 job_* 还没加载，先 tool_search("job") 一次）`。
2. **失败时引导**：`bash` 返回 `nonzero_exit` 且输出含
   `mount -t proc` / `/proc` 不可读时，追加
   `[sandbox] 这个沙箱没有 /proc（no-proc 层），ps/top/pgrep 不可用；
   用 job_list 查看后台任务。`
3. 可选：把三个 job 工具从延迟集合里移除（合计 schema 很小）。取舍是每步
   多几百字符，换取模型不用两步才能管理后台任务。建议先做 1+2，观察一版。

### 验证

单测覆盖两条提示文本；真机 S1 场景里模型应在 1 步内找到 job 工具。

---

## R2-8（P2）内置工具面 23–31k 字符

### 实测

在本仓库默认注册表（28 个工具）上逐项测量：

```
total 23418
  2466 bash            1945 subAgent[deferred]   1779 todo_write
  1420 spawn_agent     1139 parallel_research[deferred]  1056 write
  1017 finish_research   953 history_search    864 grep    849 merge_agent
   772 update_goal       757 start_goal        690 edit    671 update_plan
   649 wait_agent        625 read              602 shell_read  596 webfetch[deferred]
   574 send_message      526 skill[deferred]   514 subagent_list[deferred]
```

真机 `ContextAssembled.tools_chars` 为 27,181（fullstack）/ 30,833（skill）。

### 方案

1. **把低频的编排/代理控制工具改为延迟**：
   `send_message`(574)、`interrupt_agent`(478)、`close_agent`(422)、
   `resume_agent`(420)、`merge_agent`(849)、`history_search`(953)，
   合计约 3.7k 字符/步。保留 `spawn_agent`/`wait_agent`/`list_agents` ——
   它们是并行工作的主干，延迟会多一步。
2. **把“工具面超支”提示从只统计 MCP 扩展到全表面**：
   [agent_loop.py:1755-1789](file:///workspace/ai-coding/ai_coding/core/agent_loop.py#L1755-L1789)
   现在只算 `origin != "local"`。建议保留 `mcp_chars_warn` 的语义，另加
   `ai.tools_chars_warn`（默认 0=关）用于整体面，并在 `/trace` 里按工具
   列出前 5 名。
3. 可选：`bash` 的描述 2,466 字符是单项最大；把
   `network_permissions`/`run_in_background` 的说明各压到 2 行，可省约 600。

### 验证

离线测量脚本（本轮用的那段）进 CI，断言默认注册表总字符数不超过阈值；
改动前后对比表写进文档。

---

## R2-9（P3）管道提前关闭被记为失败

### 证据

`grep … | head -30` 有正常输出，工具结果仍是 `ERR:nonzero_exit`。

### 根因

bash 以 `[SHELL_ARGV0, "-c", command]` 直接执行，管道状态取最后一条
（`head` 应为 0）。实际出现 141/SIGPIPE 的场景需要单独确认，但**无论
哪种**，当前只有“0 = 成功，其它 = 失败”两档，没有“结果可用但状态非零”。

### 方案

在 [bash_tool.py:1071-1078](file:///workspace/ai-coding/ai_coding/tools/bash_tool.py#L1071-L1078)
加一档：

```python
if outcome.returncode == 0:
    success
elif outcome.returncode in (-13, 141) and output.strip():
    # SIGPIPE：下游（head/less/grep -q）提前关闭，输出本身是完整可用的
    return ToolResult(success=True,
                      output=output + "\n\n[命令因下游提前关闭而终止（SIGPIPE 141）；以上输出完整可用]",
                      data=data)
```

同时把“没有匹配”也区分开：`grep`/`rg` 退出 1 且无输出时，输出补充
`[grep: 没有匹配]`（仍 `success=True`），避免模型把“没找到”当“命令坏了”。

**边界**：只在**输出非空**时把 141 当成功；空输出 + 141 仍算失败，因为那
可能真的是命令没跑起来。

### 验证

单测：mock outcome `-13` + 非空输出 → success；空输出 → 失败；
`grep` 退出 1 → success 且带“没有匹配”。

---

## R2-10（P3）连续多步复述同一计划

### 证据

Fullstack 第 1 轮连续多步输出“我来从零构建…先完成后端…”。

### 方案（低优先，先观测）

1. `TurnBooks` 记录最近 3 条 assistant 文本的首 80 字符；
2. 连续 3 条相似度 ≥ 0.9 且没有工具调用时，注入一行
   `[narration] 你连续重复了同一段计划，请直接执行下一步工具调用。`；
3. 每回合最多一次。

风险很低（只发提示），但收益也小；建议排在 R2-9 之后。

---

## R2-14（P1）验收门把脚手架当成完成

### 证据

修复后 Fullstack 复跑第 2 轮 `TASK COMPLETE`（后端 `8 passed`、前端
`npm run build` 成功）。但 `frontend/src/App.tsx` 是 Vite 脚手架原文：

```tsx
function App() {
  const [count, setCount] = useState(0)
  return (<><h1>Get started</h1><p>Edit <code>src/App.tsx</code>…</p>
    <button onClick={() => setCount(count + 1)}>Count is {count}</button></>)
}
```

列表 / 看板 / 新建编辑 / 统计面板一个都没实现，README 也不存在。

### 根因

`scripts/eval_fullstack_task.py` 的 `_verdict()` 只看
`be_pytest == 0 and fe_build == 0`。项目已有 `session.acceptance` 机制
（`docs/features.md` R0），但本轮没有填写验收项，所以门只校验了
“可机器判定的副产物”。

这不是产品 bug，而是**验收契约**没有被使用。但真机上它表现得和 bug
一样：任务被宣布完成，实际交付缺失。

### 方案

1. **驱动侧（立刻可做）**：`_verdict` 增加“产物结构”判据：
   - 前端 `src/` 下必须存在被 `App.tsx` 引用的组件文件（至少 3 个）；
   - `App.tsx` 不得与 `npm create vite` 模板逐行相同（按模板摘要比对）；
   - 根 `README.md` 必须存在且包含后端/前端两条启动命令。
   不满足就把差异回喂成下一轮 prompt，而不是宣布完成。
2. **产品侧（更根本）**：任务开始时由调用方写
   `session.acceptance`（例如 `["/api/tasks 可 CRUD", "前端列表页渲染任务",
   "README 含启动步骤"]`），让 gate 校验**语义**而不是 build 退出码。
   可以先做一条最小可用：`acceptance` 里每项带一个可执行检查（命令行或
   文件断言），由 gate 逐项跑。
3. **文档**：在 `eval-05-fullstack-task.md` 里把这次的复现补一条，说明
   “修好停滞不等于修好验收”。

### 验证

- 驱动脚本对“Vite 脚手架 + 能 build”的仓库必须判 **FAIL**；
  对本次真实产物（如果有）判 PASS。
- 单测：给定一个脚手架 `App.tsx`，`_verdict` 返回 False。

---

## 建议的实施顺序

1. R2-6（一行）、R2-4（一行 + 结构测试）—— 立刻做，零风险；
2. R2-3 A + R2-5 提示 —— 只改文案，先把“假成功”和“找不到 job 工具”解决；
3. R2-2r + R2-13 —— 上下文/stall 的根，改完复跑 Skill 与 Fullstack；
4. R2-7 A/B/C —— MCP 可见性与文档；
5. R2-14 —— 验收契约，避免下一次“通过但没做”；
6. R2-8、R2-9、R2-10 —— 成本与体验。

每条都应带一条**能复现的**测试；真机复验沿用本轮的
`/tmp/aic-r2/scripts/runner.py` 与 `scripts/eval_fullstack_task.py`。
