# R22 功能矩阵（批次 1：S1 面板巡检）

> 本文件是 R22 方案 §2 功能矩阵的**第一版实测底稿**。批次 1 只记录、不修，判定口径见 §1。
> 证据全部来自 S1 场景工作区 `/workspace/eval-workspace/r22-s1`（真机 TUI，190×46，tmux 驱动），
> 帧文本落在 `/tmp/r22-shots/NN-*.txt`，关键帧已在本文件内逐字摘录，不依赖 /tmp 存活。

## 0. S1 场景与取证方式

| 项 | 值 |
| --- | --- |
| 工作区 | `/workspace/eval-workspace/r22-s1`（`src/mathx.py` + `tests/test_mathx.py` + git 仓库，起始带未提交 diff）|
| 配置 | `config.record.yaml`（`tracing.capture: full`、`max_context_tokens: 48000`、`approval_timeout: 0`）|
| 模型 | `qwen3.7-flash` via `127.0.0.1:8899`（`scripts/recording_proxy.py`，S1 专用日志 `/tmp/record/r22-s1.jsonl`，18 次请求）|
| 驱动 | `tmux send-keys` + `capture-pane -p`；每条命令一帧（`/tmp/r22-shots/01…64`，共 66 帧）|
| 轨迹 | `.ai-coding/traces/{43f3ab1a,957ab4e2}/*`（3+2 个 turn，供批次 3 复用）|

### 0.1 环境限制：bash 在本沙箱不可用（已处置）

`process_sandbox.mode: enforce` 在本机直接失败：

```
⊘ 已运行命令 · cd /workspace/eval-workspace/r22-s1 && git diff HEAD · 32.5s
被拒绝
```

轨迹里是同一件事，且**错误码是 `denied`**：

```json
{"evt":"ToolFinished","data":{"tool_name":"bash","success":false,
 "output_preview":"error: bwrap.unavailable","duration_ms":32534,"error":"denied"}}
```

- `bwrap --version` 正常，但 `bwrap: Creating new namespace failed: Operation not permitted` —— 本沙箱不允许建命名空间，`enforce` 必然失败；
- 每次 bash 调用要等 ~32s 才返回，且 UI 显示"**被拒绝**"（`denied`），语义是"有人拒绝了这次调用"，与真实原因（沙箱起不来）不符；
- 处置：`config.record.yaml` 改 `process_sandbox.mode: "off"`（注释写明这是 eval-only，产品默认仍是 `enforce`），并在本文件 **A-4** 记录该错误码问题。改后 bash 可用，S1 后半段与计划执行全部真机跑通。

## 1. 判定口径（与方案 §1 一致）

| 级别 | 含义 |
| --- | --- |
| L0 | 无入口 |
| L1 | 仅单测可用 |
| L2 | 真机可用（本场景跑通一次）|
| L3 | 可触发（同一指令两次都成立、不需要内部知识）|
| L4 | 有效（产出被后续实际使用）|

每个命令的 **6 问**：① 有入口 ② 有参数提示 ③ 有进行中态 ④ 成功反馈是人话 ⑤ 失败反馈是人话 ⑥ 长内容能滚动/退出。

## 2. 调色板 22 条命令

| # | 命令 | 形态 | ① | ② | ③ | ④ | ⑤ | ⑥ | 判定 | 证据 / 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `/help` | 对话框 | ✓ | — | — | ✓ | — | 内容不超一屏，无需滚动 | L2 | 只有键位（Model/Session/Agent 三段），**不列斜杠命令**；`Agent` 段的两行没有键位（`02-help`）|
| 2 | `/models` | 对话框+搜索 | ✓ | 搜索框 + `ctrl+a connect · enter switch · esc close` | — | ✓ 当前项 `● qwen3.7-flash 127` | ✓ 过滤无结果 → `no matches` | 列表可滚动（有 █ 指示）| L2 | `03-models` / `03b` |
| 3 | `/connect` | 两步向导 | ✓ | 第二步有占位符 `https://api.example.com/v1` | — | ✓ `enter next · esc cancel` | esc 取消干净、无残留 | — | L2 | `04/04b/04c`；未真正写配置（避免污染）|
| 4 | `/mcp` | 对话框 | ✓ | 搜索框 | — | — | ✓ **空态自带解释与下一步** | — | L2（仅空态）| `没有配置 MCP 服务器 · MCP 已关闭（配置里 mcp.enabled: false）· 先打开它才能接入 · esc close`（`05-mcp`）|
| 5 | `/sessions` | 对话框 | ✓ | `enter open · ctrl+d delete · esc close` | — | ✓ 按 Today 分组、`●` 标当前 | ✓ 无会话 → `no matches` | 有 █ | L2 | 标题被按宽度硬切、无省略号：`957ab4e2  Review the current git diff and identify any issues, bugs, or i`（`36-sessions-list`）|
| 6 | `/skills` | 对话框 | ✓ | `enter add to prompt` | — | — | ⚠ 空态只有 `no matches`，**没有** `/mcp` 那种"为什么空、怎么补" | — | L2（空态）| `07-skills`；与 `/mcp`、`/jobs`、`/memory` 的空态质量不一致（A-6）|
| 7 | `/status` | 对话框 | ✓ | — | — | ✓ Session/Model/Workspace/Max context/Max tool iterations/Stop policy | — | — | L2 | `08-status` |
| 8 | `/new` | 直接切会话 | ✓ | — | — | ✓ 新 sessionId + 侧栏归零 | — | — | L2 | `35-new`（`d0f87204`）|
| 9 | `/quit` | 退出 | ✓ | — | — | ✓ 干净退出（tmux pane 恢复 shell）| — | — | L2 | 见 §6 收尾 |
| 10 | `/review` | 真回合 | ✓ | — | ✓ 重试行 `⤷ 重试 [transport] 第 1 次，等 2s：Connection error.` | ✓ 输出 markdown **表格**（Changes Made）+ 编号小节 + Verdict | ✓ `回合未完成 · 引擎错误（1 轮）/ 原因 / 目标 / 轨迹: …/turn-1.jsonl` | 正文可鼠标滚轮滚动 | **L4** | `23`(失败态)、`30`(成功态全文)、`31`(滚轮) |
| 11 | `/budget` | 内联 | ✓ | — | — | ✓ 四态文案之一 `[budget] 本会话还没跑过回合` | — | — | L2 | `09-budget`；四态设计见 `commands.py:1315` |
| 12 | `/trace` | 内联 | ✓ | — | — | ✓ `[trace] turn 1: 0 tok (0 in / 0 out) · 2 spans · 0 model steps · 0 tool calls · 0 degraded` | ⚠ 无回合时误报 `tracing is off for this session` | — | L2（文案有错，A-1）| `12-trace`（错）→ `24-trace-after-turn`（对）|
| 13 | `/todo` | 内联 | ✓ | — | — | ✓ `[todo] 本会话还没有任务清单（开始一轮对话后建立）`（A-5 已修）| — | — | L2 | `13-todo`（旧英文）→ 批次 9-3 真机帧 160 |
| 14 | `/memory` | 对话框 | ✓ | `enter 全文 · ctrl+d delete · ctrl+t pin/unpin · ctrl+g 跨项目` | — | ✓ `0 条 · 没有记忆` | — | — | L2（空态）| `14-memory`（本场景无记忆；跨项目切换靠 ctrl+g）|
| 15 | `/config` | 内联 | ✓ | — | — | ✓ 五行限制值；`[config] default_model: qwen（qwen3.7-flash）`（A-7 已修）| — | — | L2 | `10-config`（旧 `model: qwen`）→ 批次 9-5 真机帧 164 |
| 16 | `/tasks` | 内联 | ✓ | — | — | ✓ `no background runs` + `no background shell jobs`（两类都答）| — | — | L2 | `15-tasks` |
| 17 | `/compact` | 内联 | ✓ | — | ✓ `⠙ 正在运行…`（未压缩时几乎瞬时）| ⚠ `· 7→7 条 · 0 in · 0 out · 压缩耗 0` + `L3 681→681 tok / L1 … / L2 …` + `[compact] messages 7 → 7 (removed 0)`：L1/L2/L3 无解释 | — | — | L2（文案不可读，A-8）| `32-compact`；且 `removed 0` 后上下文计量被清零（见 A-8）|
| 18 | `/undo` | 内联 + reload | ✓ | — | — | ✓ 历史回滚、**被撤销的用户消息回填到输入框**（可直接改再发）| ⚠ toast 标题写成 `Compacted` | — | L2（A-3）| `33-undo` |
| 19 | `/redo` | 内联 | ✓ | — | — | ✓ 撤销的回合整段回来（含 markdown 表格）| — | — | L2 | `34-redo` |
| 20 | `/permissions` | 对话框 | ✓ | `↑↓ pick a grant · x revoke it · p switch policy · c forget grants · r reset session rules · esc close` | — | ✓ `Policy: Confirm each call`、`base 83 · standing 0 · …`、`store /root/.config/ai-coding/grants.json` | ✓ `remembered (none)` | — | L2 | `16/21` |
| 21 | `/stats` | 内联 | ✓ | — | — | — | ✓ `[stats] no traced sessions under this workspace yet` | — | L2 | `11-stats` |
| 22 | `/export` | 内联 | ✓ | ✓ 用法写在错误里 `Available: html, otlp` | — | （未跑成功，本场景无可导出痕迹时会被 `trace` 挡住）| ✓ `[export] unknown format '(none)'. Available: html, otlp` | — | L2（错误路径）| `20-export`；成功路径需 S2 有 trace 后再补 |

## 3. 手打 7 条（不在调色板里）

| 命令 | 实测 | 判定 | 证据 |
| --- | --- | --- | --- |
| `/model` | 打开 Models 对话框（**因为模糊建议恰好把 `/model` 命中到 `/models`**）| L2（碰巧）| `38-model` |
| `/resume` | 输入 `/resume` 后建议条选中 `/export`，回车会去跑 export（A-2）；**先按 esc 关掉建议**再回车才会到后端 | **L2 但默认路径是错的** | 后端输出：`[resume] saved sessions:` + 5 行 `id  msgs=N`，**没有用法提示**（`50-resume-backend`）|
| `/agents` | 建议条命中 `/tasks` → 回车执行的是 `/tasks`（A-2）| **L1**（后端 `_handle_agents` 有实现，TUI 默认路径到不了）| `52-jobs`…`62`；`grep _handle_agents commands.py:778` |
| `/goal` | ✓ `[goal] 未立案。用法：/goal <objective> 立案；/goal off 停止。`（该 query 恰好没有模糊命中，走了后端）| L2（碰巧）| `41-goal` |
| `/plan` | 建议条命中 `/permissions` → 打开权限对话框（A-2）| **L1** | `48-plan` |
| `/readonly` | 建议条命中 `/export` → `[export] unknown format '(none)'`（A-2）| **L1** | `42-readonly`、`47-readonly2` |
| `/clear` | 建议条命中 `/permissions` → 打开权限对话框（A-2）| **L1** | `51-clear` |

> 这 7 条全部在 `app/slash.py::_ALIASES` 里（后端认），但前端 `util/slash.ts::SLASH_COMMANDS` 只收 22 条，
> 于是"前端不认识 → 建议条用阈值 0 的模糊匹配兜底 → 回车执行被选中的那一条"。**A-2 是本轮最严重的发现。**

## 4. 对话框 15 个

| 对话框 | 入口 | 空态/成功态 | 键盘 | 判定 | 证据 |
| --- | --- | --- | --- | --- | --- |
| Help | `/help`、palette | 三段键位 | esc | L2 | `02-help` |
| Models | `/models`、`/model`、ctrl+x m | 搜索 + `●` 当前 | ↑↓/enter/esc | L2 | `03` |
| Connect（两步）| `/connect` | 选 provider → 填 endpoint | ↑↓/enter/esc | L2 | `04b` |
| MCP | `/mcp` | 空态解释 `mcp.enabled: false` | esc | L2（空态）| `05-mcp` |
| Sessions | `/sessions`、ctrl+x l | Today 分组 + `●` | enter/ctrl+d/esc | L2 | `36` |
| Skills | `/skills` | 仅 `no matches` | enter/esc | L2（空态弱）| `07` |
| Memory | `/memory` | `0 条 · 没有记忆` + 跨项目提示 | enter/ctrl+d/ctrl+t/ctrl+g/esc | L2 | `14` |
| Background jobs | ctrl+x j | `没有后台任务` + 选中后 ctrl+d 停止 | enter/ctrl+d/esc | L2（空态）| `52-jobs` |
| Queued input | ctrl+x i | `没有排队输入` + `ctrl+g 提前` | enter/ctrl+d/ctrl+g/esc | L2（空态）| `54-inbox2` |
| Permissions | `/permissions`、palette | policy + 分级计数 + store 路径 | ↑↓/x/p/c/r/esc | L2 | `16/21` |
| Status | `/status`、palette | 6 行运行时事实 | esc | L2 | `08` |
| **Plan review** | 计划模式下模型 `presentPlan` | `◆ Plan review / 批准这个计划并开始执行？` + 标题 + 摘要 + 分步 `· 改什么 / · Verify:` + 选项 `1 开始执行 / 2 继续规划` | ↑↓/1/2/enter/**pgup/pgdn/ctrl+u/ctrl+d**/esc | **L4** | `56-plan-run` |
| Permission card（bash）| 任意 WARN 级工具 | `tool 'bash' requires confirmation` + `Not remembered…` 解释 + `Allow once / Reject / Stop this turn` | ←→/enter/y/n/s/esc | L2 | `25`、`34` |
| Permission card（edit）| 同上 | 同一张卡，但**多一档 `Always allow`**（可记忆）| 同上 + `a` | **L4**（grant 落盘）| `57-plan-approve`；`/root/.config/ai-coding/grants.json` 出现 `edit:/workspace/eval-workspace/r22-s1/src/mathx.py@build`（ttl 86400）|
| Command palette | ctrl+x p | 分组 + 描述 + 键位，`enter run · esc close` | ↑↓/enter/esc | L2（两个缺陷，A-9）| `60-palette`、`62`、`64` |
| Toast | `/compact`、`/undo` | `Compacted 7 → 0 messages` | 自动消失 | L2 | `33-undo` |

未覆盖：`ask` 卡（需模型主动调 `askUser`，随 S2 补）；`mcp add`（本环境 MCP 关闭，无可达路径）；`skill pending`（需 `@skill` 且有待定项）。

## 5. 异常清单（批次 4 候选）

| ID | 级别 | 现象 | 根因（已定位到代码）| 证据 |
| --- | --- | --- | --- | --- |
| **A-2** | **P1** | 手打 `/readonly`→执行 `/export`；`/plan`→打开权限对话框；`/agents`→执行 `/tasks`；`/clear`→打开权限对话框；`/resume`→执行 `/export` | `filterSlashCommands` 用 `threshold: 0` 的模糊匹配（`util/slash.ts:182-190`），任何 query 都返回至少一条；`prompt.tsx:344-364` 在建议条可见时把 `return` 绑给 `prompt.autocomplete.select`，于是回车执行**被选中项**而不是提交文本 | `42/47/48/49/51/52` |
| **A-3** | P2 | `/undo` 后的 toast 标题是 `Compacted` | 任一"同会话历史重写"都走 `event.data.compacted`，客户端把标题写死为 `Compacted`（`routes/session.tsx:596-613`）| `33-undo` |
| ~~**A-4**~~ | ~~P2~~ | ~~沙箱起不来时工具结果 `error: "denied"`，UI 显示"被拒绝"~~ **已修（批次 8-6）**：三处 `SandboxGap` 翻译统一为 `sandbox_unavailable`，后台拒单 `detail` 为 `未执行 · <原因码>`，UI 显示"沙箱不可用（命令未执行）"(✗，非 ⊘)；真机帧 156 | `bwrap.unavailable` 被映射成 `denied` 语义 | `turn-2.jsonl`；新轨迹 `7bd0766d…/turn-1.jsonl` `error:"sandbox_unavailable"` |
| ~~**A-5**~~ | ~~P3~~ | ~~`/todo` 空态是英文，其它命令是中文~~ **已修（批次 9-3）**：改为 `[todo] 本会话还没有任务清单（开始一轮对话后建立）`；任务为空时的 `(无任务项)` 原有中文不变 | `commands.py` 的 `task is None` 分支未本地化 | `13-todo`；批次 9-3 真机帧 160 |
| ~~**A-6**~~ | ~~P3~~ | ~~空态质量不一致：`/mcp` 解释"为什么空 + 怎么开"，`/skills`、`/sessions` 只有 `no matches`~~ **已修（批次 9-4）**：`DialogSelect` 区分"无数据/搜索无命中"并允许面板覆盖；skills/sessions/mcp 各给"为什么空 + 下一步"，加载中/失败不误报；真机帧 161–163 | 各 dialog 自己写空态 | `05/06/07`；批次 9-4 真机 |
| ~~**A-7**~~ | ~~P3~~ | ~~`/config` 的 `model: qwen` 与 `/status` 的 `qwen3.7-flash` 不是同一字段，读起来像"模型名被截断"~~ **已修（批次 9-5）**：`/config` 改打配置键并附解析名 `default_model: qwen（qwen3.7-flash）`；`/status` 保持运行时口径；真机帧 164 | `/config` 打印 `loop.default_model`（配置键），`/status` 打印运行时模型名 | `08/10`；批次 9-5 真机 |
| **A-8** | P2 | `/compact` 什么都没压（`removed 0`）却把上下文计量清零（`0 tokens / 0%`），像"上下文被清空了"；`L1/L2/L3` 无解释 | `last_compacted_turn` 被抬到当前轮，`live_prompt_tokens` 据此丢弃所有既有 usage（`domain/session.py:569-590`、`server/handlers.py:278-296`）| `32-compact` → `33-undo` |
| **A-9** | P2 | 命令调色板（批次 8 复核后改写）：① 首屏只露 8 条、整屏跳（PageDown/Home/End）时列表不跟随；② 输入 `agents` 在 Home 显示 `no matches`（会话内正常命中）；另发现同名双胞胎重复行（`Review`×2、`Queued input`×2） | ① 行 `<box>` 被原生渲染器量成 2 行（带 footer 且 title+desc+footer 超宽，第二行空），17 行视口只放 8 条；`ScrollBox.scrollChildIntoView` 在"目标行完全在视口外"直接 `return 0`，整屏跳不回滚；② `dialog.agents.show` 只在会话层注册（Home 没有该命令，不是匹配问题）；重复行因为 app 层与会话层各注册一份同名命令、`visibility:"registered"` 按层展开不去重 | `150–154` 帧；`/tmp/a9-rows.log`（`content` 子节点 `y/h` 实测：footer 行 h=2、无 footer h=1） |
| **A-10** | P3 | 工具卡片时长把**审批等待**算进去：`git status` 显示 `28.9s` | `duration_ms` 从发起算起，与人工等待混在一起 | `29-review-out`；**已修（批次 8-3）**：新增 `ToolFinished.approval_ms`，卡片显示 `14.1s（审批 13.9s）` |
| **A-1** | P2 | `/trace` 在"还没跑过回合"时报 `tracing is off for this session` | `_trace_report` 只看 `recorder.current_path`，把"无回合"与"没开 trace"合并（`app/commands.py:1304-1312`）；`/budget` 分了四态，`/trace` 没分 | `12-trace` → `24-trace-after-turn` |
| ~~**U-1**~~ | 观察 | ~~一次 TUI 卡在 `Working…`（`/tasks` 之后），输入被排进 inbox（`/export`，`target: next-turn`），两次 esc 无效，重启才恢复~~ **已修（批次 9-6）**：快速命令的终态可能先于 ack 到达，reducer 的 D7 守卫会把它丢掉、`busy` 永不结算。现在把未注册的终态 id 记为 `orphanTurnId`，ack 到达时结算；reducer 8 例（含 D7 保护）绿，真机连跑 `/tasks`、`/config`、`/budget` 3 轮均回到 `Ask anything…`（帧 165） | 前端 `/tasks` 命令卡 `Working…`，输入排进 inbox，两次 esc 无效，重启恢复 | `15-tasks`、`a81c9be0.json` inbox；批次 9-6 帧 165 |
| **U-2** | 观察 | U-1 竞态发生时，结算 busy 的时机在 ack 回调而不是 `turn.finished` 处理器里，因此结算前几毫秒内排进 inbox 的行不会自动派发，要等下一轮 | 队列派发只在 `turn.finished` 事件处理器里读 busy（`routes/session.tsx:687`）；没有任何"ack 结算后 flush"的触发点 | U-1 评审（`docs/r22-batch9-u1-plan.md` §6） |

## 6. 批次 1 结论

- **能用**：22 条调色板命令 **L2 19 条 / L4 3 条**（`/review`、plan review 卡、edit 授权的 grant 落盘）。计划模式端到端成立：
  `shift+tab → plan（只读）` → 模型 `presentPlan` → **Plan review 卡** → `1 开始执行` → `[plan] 步骤 1/3` → 编辑文件 → 每步 `Verify:` 用 bash 真跑 → `[plan] 完成 3 步（其中 2 步未经分步验收）· 用了 1 回合 / 52674 tokens` → `# Todos 3/3`，
  磁盘上 `src/mathx.py` 真的改成了 `def add(a: int | float, b: int | float) -> int | float:`（**L4**）。
- **不能触发**：7 条手打命令里 5 条默认路径被建议条劫持（A-2），只有 `/goal`、`/model` 碰巧可用。这是"入口有、但触发不到"的典型。
- **反馈质量**：错误态普遍是人话（引擎错误带原因/目标/轨迹路径、重试有进度行、权限卡解释"为什么不记忆"、`/mcp` 空态带下一步）。欠账集中在**空态一致性**（A-6）、**压缩文案**（A-8）、**误报**（A-1、A-3）。
- **键盘可达性**：主对话区只能鼠标滚轮滚动（PageUp/PageDown 无效，`session.tsx` 无滚动绑定）；只有 plan review 卡自带 `pgup/pgdn/ctrl+u/ctrl+d`。
- 下一批（批次 2）直接用本场景已生成的 5 个 turn 的 trace + 18 条代理请求做 §4 上下文解剖，不必重跑。

## 7. 批次 4 修复候选（按性价比，≤5 条）

1. **A-2**（P1）：建议条不得劫持"文本恰好等于已知命令"的提交；`threshold: 0` 收紧为"前缀/子串"命中。红测试：`filterSlashCommands("readonly")` 不含 `/export`；`prompt` 在 exact-command 时调用 `onSubmit`。
2. **A-8**（P2）：`removed 0` 的压缩不抬 `last_compacted_turn`；压缩输出把 `L1/L2/L3` 换成人话（或补一行说明）。
3. **A-1**（P2）：`/trace` 补第三态 `本会话还没跑过回合`（对齐 `/budget` 的写法）。
4. **A-3**（P2）：`compacted` 事件带 `kind`，toast 标题按 `undo/redo/compact` 分别写。
5. **A-9**（P2）：调色板未过滤态可滚动 + 搜索能命中 `Agents`。

> A-4 需要先定"沙箱不可用"该报哪个错误码（`ToolResult.error` 契约），再改映射；涉及 `docs/data-contracts.md`，单独走方案。

## 8. 批次 4 处置与新增发现

| ID | 状态 | 说明 |
| --- | --- | --- |
| A-1 `/trace` 三态 | **已修** | 真机验证：`[trace] 本会话还没跑过回合（跑完一轮后可看）` |
| A-2 建议条劫持 | **已修** | 真机验证 `/readonly`、`/agents`、`/plan`、`/resume` 四条全部到达后端（帧 90–93）；前端只按 name 子串匹配 |
| A-3 撤销 toast 写成 `Compacted` | **已修（单测）** | 真机复验受阻（见 A-11） |
| A-8 空压缩清空计量 | **已修（单测）** | `_freed_tokens`：token 或消息数真的变小才抬边界；真机复验受阻（见 A-11） |
| A-5 / A-6 / A-7 | 未修 | 留批次 5（A-4 的错误码契约已在批次 8-6 定下） |
| A-10 卡片时长含审批 | **已修（批次 8-3）** | `ToolFinished.approval_ms` + bridge `approvalMs` + `formatToolDuration`；真机帧 155：`14.1s（审批 13.9s）`；轨迹行同值（14145/13918）；顺带修了超时文案现在引用真的预算（`within 0.5s`） |
| A-9 调色板滚动/重复行 | **已修（批次 8-1）** | `rowLayout`+`scrollTopFor` 自己算滚动（整屏跳跟随）、行 `height={1}`（首屏 8→12 条）、`dedupeRowsByName`（同名双胞胎只剩一行）；真机帧 `150–154`。`agents` 在 Home 无命中属会话级作用域，不改；描述硬切未修（库的 `truncate` 是中间省略，见方案 1.3-3） |
| A-4 沙箱不可用被报成拒绝 | **已修（批次 8-6）** | 新码 `sandbox_unavailable`（三处 `SandboxGap` 翻译点）；后台拒单 `detail` 为 `未执行 · bwrap.unavailable`；UI 用 ✗ 而非 ⊘。真机帧 156（影子 `bwrap` 让机制真不可用）：`沙箱不可用（命令未执行）`，轨迹 `error:"sandbox_unavailable"`。顺带更正：本机 `enforce` 实际可用（回退到 `no-proc` 层） |
| O-1 重复调用守卫挡住"重读累加" | **已修（批次 8-7）** | 守卫按"结果还能不能复用"计数：`read` 的上次结果被 L2 信封 / L3 `<persisted-output>` / L4 摘要移出 `ctx.messages` 后不再占用预算，总量封顶 `2×阈值`；拒绝文案改为"换个行段读，或搜索/计数用 grep"。s3 轨迹重放：iter3 从 `guard_denied` 变为 ALLOW，第 5 次仍拒。方案 `docs/r22-batch8-o1-plan.md` |

### ~~A-11（新，P1）~~ `/resume` 之后的下一次刷新用旧 sessionId —— **已修（批次 5）**

**实测**：`/resume 957ab4e2` 成功切到该会话（页眉与侧栏都对），随后 `/compact` 在 transcript 里打出

```
[11:49:36] [ERROR] Error: ValueError: session not found: 0330a025797940398d93f8ca510ff046
    at ResponseError (.../vscode-jsonrpc/lib/common/messages.js:106:9)
```

`0330a025…` 是**启动时那个从未落盘的空会话**（`sessions/` 下没有这个文件），所以 `session_load` 抛"not found"。

**根因（已定位）**：`routes/session.tsx:558-589` 的 `turn.finished` 处理里，
`action === "new_session"` 时用事件里的新 id 调 `props.onSelectSession(id)`（父层状态，异步生效），
紧接着 `event.data["reload"] === true` 分支却用 **`props.sessionId`（还是旧的）** 调 `loadSession`。
两个分支同一次事件里竞争，reload 抢在了路由更新之前。

**修法（`docs/r22-batch5-fix-plan.md` FIX-6）**：新增纯函数 `reloadTarget(action, eventSessionId, propSessionId)`，
切换时用事件里的权威 id，事件无 id 时跳过重投影；真机帧 100–101，`/resume 957ab4e2` 后 `/compact`
不再出现 `session not found`。

### ~~A-12（新，P3）~~ 切换会话的窗口内提交仍带旧 id —— **已修（批次 8-5）**

`/resume`、`/clear` 的切换要等一次 IPC 往返（`app.tsx:253-259` 先 `await loadSession` 再 `switchTo`），
这段时间里 `buildSubmission(props.commands, props.sessionId, …)`（`routes/session.tsx:172,305`）仍用旧 id，
若恰好提交，后端 `session_command`（`handlers.py:925`）会回 `session not loaded: <旧 id>`。

修法（与上面 A-11 同一个根，但不动 id 的权威性）：Shell 用 `switchDepth` 计数暴露 `switching`，
Session 的 `dispatch` 在切换期间把行挂起（记下入队时的 id 与是否需要回声）；落地后按新 id 发送，
失败则把行放回输入框、不猜旧 id。渲染级测试：挂起→新 id、失败→回填且不发。

## 9. 批次 2 / 6 的其余真机结果（S3–S7）

| 场景 | 结果 | 判定 | 证据 |
| --- | --- | --- | --- |
| **S5a 子智能体** | `subAgent(name:'scout', task:'读 src/parser.py 并列出每个函数与行号')` → 子代理返回 markdown 表（tokenize 5 / parse 10 / render 14 / summarize 18）+ Ledger 路径；父代理直接引用该表 | **L4** | 帧 70–72 |
| **S5b 后台作业** | `sleep 15; echo JOB_DONE` 以 `run_in_background` 启动 → `job_list` 表格（id/status/PID/命令）→ 完成事件唤醒新一轮 → 模型汇报 `exit 0`；`/tasks` 事后列出 `job_65e09055 completed pid 21450 [this session] · exit 0` | **L4** | 帧 72–73 |
| **S3 压缩** | 低预算（6k）下一轮触发 3 次压缩：L3 落盘（6 条，66763→3389 tok）、L4 摘要（removed 7，70947→4382）、L3+L1（74315→3369）；摘要结构化（目标/约束/决策/工作状态/已尝试且失败/下一步/关键文件）| **L2/L4**（但摘要把"部分证据"写成"已遍历完整"，见 A-11 之外的 H11） | 帧 80–82 + `turn-1.jsonl` |
| **S6 记忆** | `memory_write` 写入 `fact#0d8e0181`（type=fact，importance=4，主题"编码规范"），工具卡显示 `✓ 已记录记忆 · 6ms`；**同一回合的下一次迭代**请求里出现 `memory=96` 字符块 | **L4**（写→召回闭环） | 帧 105 + `turn-4.jsonl` i2 |
| **S7 导出** | `/export html` → 25 KB 自包含 HTML（zh-CN，落 `.ai-coding/exports/`）；`/export otlp` → 196 KB OTLP JSON（resourceSpans 2 / scopeSpans 2 / span 带 traceId·spanId·parentSpanId）| **L2 → 已补齐（批次 8-2）**：span 名改为该 span 最早事件名（`IterationStarted`/`ToolStarted`/`ModelStepFinished`/`TurnFinished`），实测 122 span 最长 17 字符、无 `|`；新发现子代理 span 与父 span 同 id 并组（A-16） | 帧 103–104 + 导出文件；离线重导验证 |
| **S4 goal 长任务** | `/goal <objective>` 立案 → `[goal] 第 1/20 轮` 自动续跑 → 一次 edit 完成 → `[goal] 共 1 轮 · 已用 82,414 tokens · 止于：目标已申报完成`；磁盘上 `src/mathx.py` 真加了模块 docstring | **L4** | 帧 110–111 + 文件内容 |

### ~~A-13（新，P2）~~ OTLP 导出的 span 名把整轮事件用 `|` 拼起来 —— **已修（批次 8-2）**

`core/export_otlp.py:113` 原来是 `"name": "|".join(events)`，实测最长 160 字符。
现在取该 span **ts 最早**的事件名（tie 按写入顺序），事件全表仍在 `ai-coding.events` 属性；
真实 trace 离线重导 122 个 span，名字只有 5 类、最长 17 字符、无 `|`；与旧导出按 spanId 对比
51 个共有 span，`ai-coding.events` 逐字一致。方案见 `docs/r22-batch8-a13-plan.md`。

### ~~A-16（新，P2）~~ 子代理 span 与父循环 span 同名并组 —— **已修（批次 9-1）**

`export_otlp.py` 的分组键只有 `row.span`，丢掉了记录里的 `scope`（`core/trace.py` 为子代理事件写
`line["scope"]`）；父、子同名 span（`i1`/`step1`/`turn`）被并成一个，`error` 会把整组标红。

修法：轨迹写入端 `_describe` 用递归得到的 `inner_parent`，还原子循环内部树；`SpanRow` 透传 `scope`；
导出按 `(scope, span)` 分组、id 带 scope（无 scope 保持旧公式）、子代理 span 加 `ai-coding.scope`。
实测 s5 的 turn-1：14 → 21 个 span，子 `turn` 指向 spawn call；无子代理会话 143 个 spanId 零差异。
方案 `docs/r22-batch9-a16-plan.md`。

### ~~A-14（新，P2）~~ 任务清单没有随目标完成收尾 —— **已修（批次 8-4）**

修法：`update_goal(complete)` 把仍非终态的条目标成 `dropped`（收据带内容与原状态，`data.closedItems`
供轨迹），下一迭代的 `<task-state>` 变成 `进度: N/N`、`[task-gate]` 不再催。`blocked` 申报不动清单。
证据：`tests/test_goal_tools.py` 17 例（含"申报前 NUDGE / 申报后 FINISH"），S4 会话原文见
`docs/r22-batch8-a14-plan.md` §1。

S4 的 goal 回合结束后，会话里仍是 `items=[('1','in_progress'),('2','completed'),('3','completed')]`，
而模型在收尾里主动指出"任务清单状态（系统注入）仍标记第1项 in_progress，这是过期数据"——它花了一次迭代解释这条过期信息。
同时该轮以 `回合未完成 · 迭代预算用尽（10 轮）` 结束：一次 edit 就完成的工作，被"验证 + 解释过期清单"耗光了预算。
候选：`stop_policy: todo_aware` 在收敛时把未收尾项显式写进 `note`；或目标完成时把 open items 归一为 `completed`/`dropped` 并留一条 digest。

### ~~A-17（新，P3）~~ 失败卡片的标题仍说"已运行命令" —— **已修（批次 9-2）**

`toolTitle()` 只用"是否还在跑"决定 `正在/已`，不看结果，所以 `✗` 卡片写成
`✗ 已运行命令 · ls`，下一行却是"沙箱不可用（命令未执行）"。

修法：`toolTitle(verb, target, running, ok=true)`，失败/被拒用 `已尝试{verb}`；卡片传
`status === "ok"`。成功与运行中不变。真机帧 159：`✗ 已尝试运行命令 · ls`。方案
`docs/r22-batch9-a17-plan.md`。

## 10. H10 修复后的 S3 复验

见 `docs/r22-batch6-h10-plan.md` 的"完成记录"：`referenced_files` 40,138 → 3,751–4,488 字符，
请求峰值 20,267 → 6,972 token（窗口 6k 的 1.16 倍），压缩 3 次 → 2 次。
答案仍为 8/162，但原因已从"输入撑爆"转成 **H11 摘要对计数型证据有损**，与 H10 无关。

### H11（批次 7 候选，P1）摘要对"计数型证据"有损，模型会把摘要当原始数据

H10 修好之后同一任务复跑，模型的原话是"根据早期对话摘要中已完成的分块读取结果，**总计为 8 行**"（真值 162）。
链路上 L1 剪掉 14 条、L4 把 27 条压成 3 条，摘要只保留了 8 个行号，而它仍然写着"全部 12 个 chunk 已完成"。

候选方向（需独立方案）：把"计数/聚合"这类**结论型证据**写进 `<working-memory>` 的结构化槽位（不是自由文本摘要），
并在摘要 prompt 里要求"保留可复算的数字与口径"，而不是只保留结论句。

## 11. H11 修复后的 S3 验收

三项修复（摘要证据规则 / 压缩后锚点提示 / 检查点不再冒充人话）后，同一 S3 任务真机复跑：
**答案 162（真值 162）**，模型自行发起 `grep -c` 复核并在答案里写明来源与 exit_code。
详见 `docs/r22-batch7-h11-plan.md` §8。观察项：6k 窗口下一轮触发 8 次压缩；重复调用守卫与"重复读同一段来累加"冲突（O-1）——已在批次 8-7 处理，见 §8 的 O-1 行。
