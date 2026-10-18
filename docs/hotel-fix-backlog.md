# hotel 会话修复：已落地与未做

## 已落地

### 第一轮（`2cc1561`）

* `tools/glob_tool.py` — 绝对 pattern 相对化；越界返回 `workspace_escape`
  结构化错误，文案带当前工作区与 `-w` 出路。根因：`Path.glob(绝对)` 抛的
  `NotImplementedError` 不在原 `except` 里，裸透成 `tool_error`。
* `app/commands.py` — `/todo` 改发 `TaskUpdated`（`todo_write` 已在用的卡片
  通道）并打印 `render_display()`。根因：它只走 `console.line`，把 prompt
  格式的 `render()`（带 `<task-state>` 信封）当人类文本打印，卡片永不更新。
* `core/request_assembly.py` — `_environment_block` 对工作区外绝对路径给一行
  警告，复用 `core/plan_runner.py:104 outside_workspace_paths` 而非第二份
  正则。Build 回合只警告不拒绝，拒绝仍归 `/plan` 的两道门。

### 第二轮（D1-a，guard 误拦）

实测依据：12 个 trace 目录共 9 次 `guard_denied`，**全部 `duplicate_call`，
`failed_repeat` 零次**；7e48aeef turn 1 的两次是
`glob {"**/*hotel*"}`（第 2 步已成功）与
`read {"/home/.../requirements.txt"}`（第 2 步 `sandbox_violation` 失败过）。

* **`core/guard.py:DuplicateCallGuard`** — 计数从 `check()` 移到新增的
  `after()`，**只对成功调用计数**，失败删除指纹。理由：拒绝文案是 "reuse the
  earlier result"，而失败调用没有 result 可复用；让模型去复用一个错误，等于
  教它别再问。阈值语义随之从"第 N+1 次调用"变为"第 N 次成功后"。
  防护面不变：重复写文件、重复 bash 这类真攻击都是**成功**调用，仍在计数内。
  文案同步改为 `already succeeded N time(s)`，与新语义一致。

### D1-b 已尝试并撤回（证伪留档）

曾把 `guard_denied` 加进 `core/turn_books.py:_NOT_EVIDENCE_ERRORS`，理由是
"拒绝是判决不是损坏"。**实测证伪，这条路是错的**：

把 4 次相同失败 read 喂进真实 loop，ladder 从 3 次注入 strategy-gate 变成
**11 次**，回合以 `迭代预算用尽（4 轮）` 收尾，终答从 `完成` 变成
`[回合未完成 · 迭代预算用尽]`。机制：`failed_streak` 被清零 → L1 窗口的
helped 信号（`failed_streak == 0`）判为"有进展"→ 同一个 L1 nudge 反复注入
直到预算耗尽。**比原问题更糟。**

正确修法是让守卫别开火（`DuplicateCallGuard`），而不是事后给拒绝打折。
`turn_books.py` 的 `_NOT_EVIDENCE_ERRORS` 因此保持原样，并在注释里写明
`guard_denied` 为何**故意不在**其中，防止有人再犯。

### 因 D1-a 更新的既有测试

* `tests/test_guard.py::test_duplicate_guard_allows_a_retry_then_refuses` 与
  `::test_duplicate_guard_resets_between_turns` — 原先断言"只 check 不 after
  就该拒"，那正是被修的缺陷本身。
* `tests/test_orchestration_wire.py::test_the_ladder_closes_its_loop_on_the_wire`
  — r3 的 `duplicate_call` 拒绝改为断言**不**出现；结束该循环的拒绝来自
  `failed_repeat`（其本职）。ladder 仍完整走通：6 次请求、L1→L2→L3、账本
  `level 3 / helped` 不变。

### 第三轮（B，工作区外文件 + bash 红线）

调研依据（本仓库 docs，非推断）：opencode 对 cwd 外是 **ask**
（`06-opencode长任务机制.md:20`，外部目录 ask + `.env` 读 ask），
拦截点在 `permission.assert`（`03-opencode编排数据流.md:179`，
`file-access.ts:135,154`），授权粒度是 `(action, resource)` 且 resource 是
**类别**（`18-阶段二重头分析-…md:43` 的 `external_directory`），回复支持
once/always/reject（`03:180`）；dsh 是 OS 级沙箱三档
（`dsh-notes/03-子系统.md:117`），越界走 `approveEscalation`。
我们是硬拒，且**硬拒本身没守住**——审计已判定
（`2026-10-08-hotel-session-audit.md:90`）"write 沙箱边界实际被 bash 绕过，
边界形同虚设，只制造摩擦力"。

* **bash 红线收紧**（`security/permission_gate.py::bash_denied`）— 旧表是纯
  子串匹配，实测 `rm  -rf /`、`rm -r -f /`、`RM -RF /`、`chmod  777`、
  `sudo` 全部绕过；且 `cat ~/.ssh/id_rsa`、`cat .env`、`cat /etc/shadow`、
  `cat *.pem` 一律只到 ask。改为：空白归一 + 按词判定（`rm` 的 flag 集合、
  `chmod` 的八进制写法），新增凭据路径表。匹配的是**路径形状**而非词，所以
  `.env.example`、`grep password src/` 仍然只是 ask。
* **file tools 越界可审批**（`sandbox.py` / `permission_gate.py` /
  `core/dispatch.py`）— `read` 进 `_PATH_TOOLS`，gate 拿到 workspace 后能
  区分区内/区外；`Sandbox` 接受 `extra_roots` 与 `grantor`，`precheck` 的
  `sandbox_violation` 不再抢在审批之前；`grant_key` 对 read 取**父目录**
  （opencode 的 external_directory 粒度），"总是"记目录不记文件。
* **`tools.extra_workspace_roots`**（config，支持通配）— 声明式常驻授权，
  启动时由 `builder.expand_root_patterns` 展开。

### 第三轮中被自己推翻的中间设计（留档）

1. **`allow_once` 一开始写进了 `grants`** — `test_dispatch_grants.py::
   test_answering_yes_records_nothing` 立刻红了：那个集合就是"总是"的语义，
   `/permissions` 会把它当常驻授权打印。改为独立的 `_once_grants`，
   会话内、不持久、`revoke_all` 一并清空。
2. **`precheck` 一开始整体挪到审批之后** — `test_tool_dispatch.py::
   test_precheck_refusal_skips_approval_and_execution` 红了：空写入、陈旧读
   这类**工具自己的**拒绝不该因此多一次弹窗。改为按错误码区分——只有
   `sandbox_violation` 走审批，其余仍在审批前短路。

### 第四轮（D5 粘贴路径 + D6 剪贴板挂起）

**D5**：粘贴的路径没有任何消费者——`registry.py:464 _MENTION` 要求 `@` 前缀，
裸路径永不命中，`_referenced_files_block` 不生成，模型只拿到一串它读不到的
字符。opencode 在入队前物化粘贴的文件（`03-opencode编排数据流.md:37,42`）。

* `core/file_mentions.py::resolve_bare_paths` — 行内识别（用户选了行内而非
  整行，因为粘贴路径本就出现在句子中间）。三类误判各有其挡：URL 由
  `_URL_PREFIX` 回看挡掉（否则 `https://x.com/a/b.py` 会产出 `com/a/b.py`）；
  `3/4`、`1.2/3.4`、`e.g.` 由扩展名白名单挡掉；剩下的交给存在性。
  单段文件名（`main.py`）也要能匹配，所以扩展名是白名单而非"必须含 `/`"。
* 与 `@` 共用 `_referenced_files_block` 一条代码路径——两个 resolver 都返回
  工作区相对路径，预算、截断、目录列举因此只有一份。
* 区外路径**不注入**：该 block 走 `read_text` 且不过 `Sandbox`，注入等于绕过
  B 刚建立的审批门。区外仍归 `read` 工具。
* 预算降级从**丢弃**改为**截断**（`_file_content(limit=)`）。实测三个 15k 文件
  时第三个原本是 `[budget exhausted, not included]` 零内容——用户粘贴正是
  "明确要求读它"。
* 顺带修 B 引入的回归：`outside_workspace_paths` 用带 `extra_workspace_roots`
  的 sandbox，否则用户已授权的目录被环境警告误报为"工作区外"。

**D6**：`clipboard.ts::capture` 只发 SIGTERM、无 SIGKILL 升级，且
`for await (child.stdout)` 是**第二个**无界等待（孙进程持管道写端）。实测一个
`trap '' TERM` 的进程让旧实现挂过 4 秒；**原生 `timeout` + `killSignal` 也
无效**（实测仍 HUNG 4336ms）。最终方案：`detached` 进程组 + `kill(-pid,
SIGKILL)` + 外层 `Promise.race`，实测 545ms settle。

### 第四轮中被实测推翻的方案（留档）

1. **D6 用 Bun 原生 `timeout` + `killSignal: "SIGKILL"`**（调研子代理的推荐）
   —— 实测 HUNG 4336ms，与旧实现无差别。原生 timeout 只解决第一个 await，
   孙进程持有的管道让第二个 await 永远不落定。
2. **D6 断言超时返回 `null`** —— 实际杀进程组后管道正常关闭，`capture` 返回
   `Uint8Array(0)`。上层 `isPng` 对空数组返回 false，语义无误，是测试断言
   过严。

## 未做

### D3「预算早耗尽」—— 实测推翻，不做

原判断"10 步硬顶导致回合太短"是错的。`.ai-coding/traces/7e48aeef/turn-1.jsonl`
（199 行）实测：

* `IterationStarted` **25 次**，`max_iterations=15`（`config.local.yaml:50`，
  非默认 10）；
* 03:50:46 `index` 从 15 **重置为 1** —— C3 续轮（`core/turn_policy.py:263-290`）
  正常生效，长任务机制在工作；
* 终态 `TurnStatus.ABORTED / reason=cancelled` —— **人工按 Esc**，非预算耗尽；
* 真实用量 `total_tokens=335,005`（cached 135k）；侧栏 `21,299 (8%)` 是单次
  请求上下文，不是累计；
* 13 次失败：`sandbox_violation` 4、`error=None` 3、`readonly_denied` 3、
  `guard_denied` 2、`tool_error` 1。

"感觉没跑多久"的真因是**前 11 步都在试路径**（第 1 步绝对 glob，第 3/4/5 步
连试三种越界写法），有效工作从第 9 步才开始 —— 正是第一轮 P0 修掉的东西。

### D2. Thought 英文与回答脱节 —— 已拍板不做

* 位置：`ai/prompt.py:16 BASE_INSTRUCTIONS`（只约束回答语言）、
  `ai/openai_engine.py:382 reasoning_content` 直通、`server/bridge.py:102`
  透传。
* 结论：与失败链无因果；改 prompt 影响全量请求前缀缓存
  （`docs/r3-analysis.md` 实测 prefix 92%），收益不抵风险。

### D4. 跨仓库 `-w` 一键切换（TUI/IPC 契约改动）—— 优先级下降

* 位置：`server/handlers.py:session.create`、`server/world.py`、前端
  `session.ts`。
* 未做理由：第三轮之后这条路的必要性大降——`tools.extra_workspace_roots`
  让"我在另一个仓库工作"零摩擦地成为配置，审批通道也能临时放行；真要在会话
  中途切 workspace 才需要改 IPC 契约。
* 入口：`session.create` 加 `workspace` 参数 + `files.find` 复用，前后端同步。

## 观察项

第三轮之后若 trace 里出现大量 `error=not_approved` 且 `data.reason` 是
"targets ... outside workspace"，说明有人在反复碰同一个外部路径——这时该把
那个目录写进 `tools.extra_workspace_roots`，而不是调低任何阈值。

D1-a 上线后若 trace 仍出现 `error=guard_denied` 且 `data.guard=duplicate_call`，
**先确认该调用此前是否成功过**：成功过就是本守卫的正常职责（结果可复用），
未成功过则说明还有别的路径在计数，需要重新取证而不是继续放宽阈值。
区分方法：`ToolFinished.data.guard` + 该 fingerprint 在本回合的历史结果。
