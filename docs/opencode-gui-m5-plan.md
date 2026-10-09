# M5 方案：六个交互细节 ✅ 已实施

> 分支：`feat/opencode-gui-replica`（接 M4 之后）
> 状态：**已实施并核验**（2026-10-09）
> 基准：OpenCode v1.18.32 源码：`config/keybind.ts`、`context/keymap.tsx`、`prompt/display.ts`、`routes/session/permission.tsx`、`ui/toast.tsx`、`util/permission.ts`
> 原则：最小化、第一性、只做相关；**每个元素对接真实后端数据，不无中生有**；库优先；TDD

---

## 1. 两段式 Esc（中断确认）

- **后端**：零改动。`session.interrupt`（handlers.py:186-192）两层取消（cancel event + future.cancel）M2 已核验。
- **前端**：
  - `util/esc.ts`（新，纯函数）：`escStep(armed)` → 第一击布防、第二击中断。
  - `routes/session.tsx`：`interruptArmed` signal + 5s 定时器；`turn.finished` 时解除布防。
  - `component/status-bar.tsx`：armed 时以 `feedbackWarning` 色显示 "Press esc again to interrupt"。
- **E2E 实测**：运行中第一击 → 状态栏黄字提示；第二击 → `[turn] 已中断（Esc），任务未收尾`，且 `sleep 30` 子进程组被杀死（pgrep 无残留）。

## 2. Toast（右上角通知）

- **后端**：零改动。`provider.retried` / `provider.fallback` / `background.done` 已在 bridge 映射（M2）。
- **前端**：
  - `util/toast.ts`（新，纯函数）：`enqueueToast` / `dequeueToast`（不可变队列）。
  - `component/toast.tsx`（新）：`ToastProvider` + `useToast`；右上角绝对定位、`raisedHigh` 底、按 variant 分色边框、5s 自动消失、悬停暂停/继续、队列一次只显示一条。
  - `routes/session.tsx`：三个事件 → `toast.show(...)`；`ToastProvider` 挂载在 `app.tsx`（**注意**：Provider 不能与消费者同组件，见踩坑 §7）。
- **E2E 备注**：retry/fallback/后台子代理无法自然触发（需故障注入），toast 由纯函数单测覆盖；渲染路径与已验证组件同风格。

## 3. Leader 键位（ctrl+x）

- **前端**：
  - `keymap-addons.ts`（新，**不依赖渲染器**）：`LEADER_KEY = "ctrl+x"`、`LEADER_TIMEOUT_MS = 2000`、`registerAppKeymapAddons`（`registerTimedLeader`，同 OpenCode `context/keymap.tsx:136-146`）。
  - `keymap.ts`：`createDefaultOpenTuiKeymap` 已装默认 parser/enabled/metadata 字段，仅追加 leader（**删除了原先重复注册的两行**）。
  - `app.tsx` 绑定（对照 OpenCode 权威表 `config/keybind.ts`）：`ctrl+x m` models / `ctrl+x l` sessions / `ctrl+x n` new / `ctrl+x s` status / `ctrl+x q` exit / `ctrl+x 1..5` 切 tab（新 `session.tab.select.N` 命令，复用 `tabs()`/`selectTab`）。
  - `dialog/commands.ts`：`formatCommandBindings` 传 `tokenDisplay: { leader: "ctrl+x" }`，面板显示 `ctrl+x m` 而非原始 token。
- **TDD**：`test/keymap.test.ts` 用库自带 `@opentui/keymap/testing` 的测试 host 真按键：ctrl+x 后 m 才派发、单独 m 不派发、格式化输出 `ctrl+x m`。
- **E2E 实测**：ctrl+x m → Models；ctrl+x s → Status；ctrl+x l → Sessions；ctrl+x 2 → 切到第 2 个 tab（侧栏标题同步）；命令面板显示 `ctrl+x m/l/n`。

## 4. 输入队列（busy 时排队，轮次结束后自动执行）

**方案修正（基于证据）**：原批准方案含"后端 `session.queue` + 桥接 `InputSteered` → `queue.updated`"，实施前核验后发现三处硬伤，**砍掉后端部分，改为纯前端本地队列**：

1. `InputSteered.total` 是**累计已投递**计数，不是 pending 数；且事件在 `drain()` 之后发出、不携带文本 —— 无法驱动 "N pending" 显示。
2. 若自动执行走 steering（`steering.put` 注入 + 又发 `session.prompt`）会**双重投递**：新轮第 0 次迭代先吸收 `[steering]`，又收到同文本 prompt，模型看到两遍。
3. 被复刻的旧 Textual 行为就是**前端本地队列**（`ui/textual_app.py:173-223`：`_enqueue` 入 deque + 提示，`_turn_done` 弹一条 `call_later(_dispatch)`）；后端 `SteeringQueue` 在旧 UI 里**从未被喂过**（仅构建未使用），OpenCode 默认 delivery 也是 queue（回合结束后执行）。

**实现**：
- `state/session.ts`：`SessionState` 加 `queued: string[]`；新增 `queued`（追加用户消息 + 入队，不重复追加）与 `queued-dispatched`（弹首条 + busy 置真）动作；`submitted`/`submit-failed` 改为展开 state（原先会丢字段）。
- `state/replay.ts`：`messagesToState` 补 `queued: []`。
- `routes/session.tsx`：busy 时 `apply({kind:"queued"})`；`turn.finished` 后若有队列 → `queued-dispatched` + 派发下一条（每结束一轮跑一条，与旧 UI 一致）；Prompt 上方显示 `N queued`。
- **TDD**：reducer 新增 4 例（排队、turn.finished 保留、弹首条不重复追加、submit-failed 保留）。
- **E2E 实测**：慢任务运行中发第二条 → 消息入转录 + `1 queued`；第一轮结束后自动执行第二条（"好"），队列清空、状态栏刷新。

## 5. Slash 自动补全

**方案修正**：原批准方案的"后端 `commands.list`（暴露 slash.py 的 21 命令）"砍掉 —— 那 21 条是旧 REPL 词汇，其中 14 条在本 TUI **无对应动作**，列出来选中只能把 `/trace` 之类当纯文本发给模型（假功能）。改为**前端目录表**：只收录 7 个真能执行的命令，每条指向一个 keymap 命令。

- `util/slash.ts`（新，纯函数）：`SLASH_COMMANDS`（help/models/sessions/skills/status/new/quit）、`slashQuery(text, cursor)`（移植 OpenCode `slashTriggerIndex` 规则：词首 `/`、无空格、无第二个斜杠，路径不触发）、`filterSlashCommands`（fuzzysort，threshold 0 同 OC 命令模式）。
- `component/prompt.tsx`：`onContentChange` 刷新建议；`useBindings` 层（`enabled: visible()` + `target: textarea`）绑定 up/down/tab/return/escape；选中清空输入并 `keymap.runCommand(...)`；`onOverlay` 上报可见性。
- `routes/session.tsx`：`suggesting` signal 门控会话层（列表打开时 esc 归列表、interrupt 绑定静默）。
- **TDD**：`test/slash.test.ts` 12 例（触发边界：`/hel`、`x /he`、`a/b` 路径、空格截断、双斜杠、光标截断；过滤排序；目录表契约）。
- **E2E 实测**：输入 `/` 出 7 行列表；`he` 过滤（help 居首）；Esc 隐藏且保留文本；`/models` + Enter 直接打开 Models 对话框（输入清空，未发给模型）。

## 6. 审批面板（Permission，IPC 双向请求）

- **后端**：
  - `world.py`：`AutoApproveCallback` → `CLIApprovalCallback`；`AppWorld` 新增 `approval` 字段（供 IPC 层装 provider）。
  - `handlers.py`：新增 `_wire_approval(world, service)` —— provider = `service.call("approval.request", {toolName, params, reason})`；答案为 CLI 既有词汇（`yes`/`always`/`no`/`stop`），同一解析器服务两个前端，异常即 fail-closed（`security/approval.py:88-98`）。`service.call`（M0.5 就为审批造好）首次投入使用。
  - `bridge.py`：**删除** `ToolApprovalRequested` → `approval.requested` 映射 —— 审批改走请求通道，事件流不再重复，且旧 notice 文案"已自动批准"在真面板出现后已是假话。`ToolApprovalRequested` 事件本身保留（trace 观察者仍消费）。
  - `security/approval.py`：零改动（provider 解析复用）。
- **前端**：
  - `util/permission.ts`（新）：`PERMISSION_OPTIONS`（Allow once/Always allow/Reject，OpenCode 原文 `util/permission.ts:162`）→ 决定 `yes/always/no`。
  - `component/permission.tsx`（新）：`△ Permission required` 面板（复用 prompt 边框中括号样式）；←/→/h/l/tab 选择、enter 确认、y/a/n 直答、esc 拒绝（fail-closed）；复用 `moveSelection`、`toolVerb/toolTarget`。
  - `app.tsx`：`onRequest("approval.request")` → Promise 挂起，面板决断即 resolve；`turn.finished` 清理挂起面板（resolve "no"）；Shell 层在 pending 时静默；`ToastProvider` 上移到此。
  - `routes/session.tsx`：pending 时以面板替换 Prompt；会话层门控。
  - `state/session.ts`：删除 `approval.requested` notice（含其测试，改为"不再出现在转录"）。
- **TDD**：
  - Python：`test_server_ipc.py` 新增往返测试（客户端 Endpoint 注册 `approval.request` → 服务端 `await world.approval.approve(...)` 答案映射 `ALWAYS` 且 params 为 `{toolName,params,reason}`）+ 未知答案 fail-closed；`test_server_bridge.py` 的旧映射测试改为"钉住不再上事件流"。
  - TS：`test/permission.test.ts` 3 例（标签原文、决定词表、reject=no）。
- **E2E 实测（真实模型 + 真 WARN 工具）**：
  - bash `sleep 20` → 面板出现（工具卡同时 running）；→→ 选择高亮（`-e` 捕获 `#fab283` 底 + 粗体）→ Enter 拒绝 → 卡片 `not approved by user`，模型收到拒绝继续。
  - `echo grant-test` → 按 `a`（always）→ 执行；write `m5-grant.txt` → `a` → `~/.config/ai-coding/grants.json` 落盘 `write:m5-grant.txt`；**重复 write 同路径 → 免审批直接执行**（文件内容确认为第二版）。
  - bash 的 always 不落盘是**既有策略**（`grant_key` 对 bash 返回 None，_NEVER_REMEMBERED），行为正确。

---

## 7. 踩坑记录（防回退）

1. **Provider 不能与消费者同组件**：`const toast = useToast()` 在 `Session` 体内、`<ToastProvider>` 却包在同一组件的 return 里 → context 查找必然失败（提供者只对子组件生效），运行时弹 Solid 错误浮层。修复：Provider 上移到 `app.tsx` 包住 `<Session>`。教训：**tsc/bun test 均不查 context 运行时错误，必须 E2E**。
2. **JSX 属性里的 `>`**：`fg={a ? b : c}` 中 `>` 会被 JSX 解析器误读为标签结束符（tsc 报 "Did you mean '{'>'}'"），把三元提出为变量即可。
3. **leader 注册必须与渲染器解耦才可测**：`@opentui/keymap/opentui` 运行时引入 `@opentui/core`（原生渲染器），测试会拖入终端依赖；把 `registerAppKeymapAddons` 拆到 `keymap-addons.ts` 后用库自带 `@opentui/keymap/testing` 真按键验证。
4. **binding 默认消费按键**：`preventDefault !== false`（keymap 编译期默认），所以自动补全层绑定的 up/down/return/esc 不会同时进 textarea —— 无需自造拦截逻辑。
5. **事件字段语义要核验**：`InputSteered.total` ≠ pending（详见 §4），照着方案写会做出错误显示。

## 8. 测试与核验

| 项 | 结果 |
|----|------|
| Python 全量 `pytest` | 见提交说明（2 个预存在 `loop_digest` 失败；M5 新增 4 例全绿） |
| `ruff`（改动文件） | All checks passed（另 67 个既有测试文件告警为历史遗留） |
| `mypy`（strict） | Success: no issues found in 126 source files |
| `bun test` | 101 pass / 0 fail（M5 新增 23：esc 2 + toast 4 + keymap 4 + slash 12 + permission 3 … 计 25，替换 1） |
| `tsc --noEmit` | 干净 |
| E2E（tmux 140×42，真实模型） | 两段式 esc（含子进程组击杀）、队列自动派发、slash 列表/过滤/执行、leader 五组绑定 + 数字切 tab、审批面板三决定 + grant 落盘免审批 —— 全部实测通过 |

**最终模样走查**：`/` 弹出 7 条命令列表（选中橙字加粗）；ctrl+x 后 2s 内按 m/l/s/n/1..5 直达；busy 时提交出现 `1 queued` 且回合后自动执行；第一击 esc 状态栏黄字 "Press esc again to interrupt"、再击中断；WARN 工具在 prompt 位弹出 `△ Permission required`（←/→ 高亮、esc 拒绝、a 记住）；`ctrl+p` 面板每行尾部显示 `ctrl+x m` 之类真实快捷键。
