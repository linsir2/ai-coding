# 编排层深改 · 08 一键续跑（resume_turn）方案（待拍板）

> 目标：notice 从"只告诉你"变成"点一下就从断点继续"。P5 只做了前半。
> 原则（沿 06/07）：搭现有车，不新造机制；先问卷，后代码。

---

## 1. 已确认的事实（证据，都有行号）

| # | 事实 | 证据 |
|---|---|---|
| F1 | 回合唯一入口是 `process_input(session, text)` → `_run_turn` | `handlers.py:291`、`agent_loop.py:382` |
| F2 | `_begin_turn` **必然**补一条 user 消息 + `turn_count+1` | `agent_loop.py:742`、`746` |
| F3 | 回合内每批工具跑完 `write_back`（补 tool 结果）→ `checkpoint` 落盘 | `turn_close.py:164`、`184` |
| F4 | 崩在审批 await 上时：**文件里只有"上一次完整批"之前的内容**。本批的 assistant 消息只在内存（`agent_loop.py:1313`），`note_approval` 只写 marker、**不 save 会话**（P3 修过的那个坑） | `agent_loop.py:1048` 注释 |
| F5 | 服务端审批 = `world.approval` 的 `set_provider`， panel 答完 `approve()` 才返回 | `handlers.py:63-75`、`dispatch.py:367` |
| F6 | resume 时历史天然干净：以上次 tool 结果结尾，无半批载具（F4 的直接推论） |

## 2. 核心洞察（决定方案形状的一句话）

**续跑不需要"审批重入"机制。** 历史停在"上次完整 tool 结果"，续跑只要**开一个不补发 user 消息的新回合**，模型从 history 自然往下走：

- 情形 A（只崩过）：模型直接给出答案，结束。
- 情形 B（有待决审批）：模型大概率重新走到同一个 WARN 调用 → `dispatch._decide` → **活的** `approval_provider` → panel 再次弹起 → 用户批准 → 回合继续。marker 的使命到此结束。

即：**续跑 = `_run_turn` 减掉 `_begin_turn` 的 append/counter 两行，其余照旧**。已完成的工具不重跑（结果已在 history 里，模型不会重发）——这是 checkpoint 机制白送的。

## 3. 选项

### S1 自然收敛（推荐）：新 IPC `session_resume` + `loop.resume_turn(session)`

- 后端：`resume_turn` = `_run_turn` 的变体（或一个 `append_user: bool` 开关——待定，见 Q1）：跳过 `add_message`/`turn_count+1`，但保留 `_begin_turn` 其余重置（`guards.reset`、`reporter.begin_turn`、`continuations=0`、`open_turn`）。
- 前端：notice 变成可点按钮（或会话面板加"续跑"），调 `session_resume`。
- 代价：约一个方法 + 一个 handler + 测试。**零新事件、零审批重构、零回放机制。**
- 诚实边界：依赖"模型会重新走到同一调用"。WARN 判定是确定性的（gate 看 tool 名 + params），所以**问题是否重现是确定的**，不确定的是模型走不走到——几乎一定会（history 摆在那）。

### S2 精确重入：跳过模型，直接重演 marker 里的调用

- resume 时不调模型，直接用 marker `{toolName, params}` 弹 panel；批准后执行工具、把结果 append 进 history、再开一个普通回合让模型收尾。
- 否决理由（先摆出来，拍板时可推翻）：省一次模型调用，但要新写"执行单个调用并缝合 history"的代码，而模型本来就要为结果做下一步——**省的只是一次生成，多的是整套缝合机制**。06 的"最小化"原则下不划算。

### S3 双轨：A 用 S1，B 用 S2

- 否决理由同上 ×2：两套机制维护，收益只是一次模型调用。

### S4 纯前端重发：把最后一条 user 消息重发成新回合

- 后端零改动。但 history 多一条重复 user 消息，`turn_count` 虚增，且 marker 残留需另行清理规则。**用数据污染换零后端**，不推荐。

## 4. 影响面（S1，钉死）

| 类别 | 数量 |
|---|---|
| 新 IPC | 1（`session_resume`，对标 `session_prompt` 但不收 `text`） |
| loop 新入口 | 1（`resume_turn`，或 `_run_turn` 加开关——Q1 定） |
| 前端 | notice 可点击（`replay.ts` + 渲染处），约 10 行 |
| 事件/审批/回放机制 | 0 新增 |

## 6. 拍板结果（2026-10-15 问卷）

- **Q1**：`_run_turn(append_user=False)` 开关，prompt/resume 共体。
- **Q2**：`turn_count` 不动（同一回合的继续，`open_turn` 保持 N）。
- **Q3**：resume 开始清掉旧 marker（新回合重走 WARN 会重写）。
- **Q4**：notice 本体可点击——键盘 TUI 无 mouse 前例，诚实翻译为 notice 自带 `ctrl+x r` 提示 + `<leader>r` 绑定 `session.resume`（palette 同步可见）。
- **Q5**（未问，默认接受、可反悔）：模型极小概率没重发同一调用时，残留 marker 下次 load 照常 notice——行为自洽，不做"忽略"按钮。

## 7. 实施后 addendum（tmux 实证倒逼的两次修正）

- **Addendum-1 · resume 注记**：B 情形实测——续跑后模型走了另一条路（直接收尾），死回合的待决计划**静默消失**。修法：resume 只在 marker 存在时追加一条 synthetic 说明（`[resume] 上一步在等审批：…；如仍需要，重新发起`，对标 `[steering]`），可见、可落盘，模型显式定夺。无 marker（情形 A）保持安静。
- **Addendum-2 · notice 只承诺能做的**：崩在首个 checkpoint 之前的审批等待——`open_turn` 从没落盘（P2 搭车设计），连 user 消息都不在文件里，后端 guard 会拒绝。此时 notice 不带 action、不带按键提示（只告知）；`action:"resume"` 只给 `recoveredInterrupted` 为真的情形。承诺与能力对齐。
- **附带抓到的真 bug**：验证脚本用 8 位前缀 `load` + 改名 + `save`，把两个现场的 history 清空了——`load` 的前缀回退返回的是 `list_sessions` 的去 bodies 行对象。产品调用点目前安全（CLI 用全 id 重载、前端发全 id），但修法是结构性的：`load` 前缀回退改走全 id 重载，加回归测试。

## 8. tmux 实证（真模型 qwen3.7-flash，真 SIGKILL 现场）

| 情形 | 结果 |
|---|---|
| A 续跑 | 模型接着 history 往下走（"文件内容只有一行 hello，请问接下来…"）；roles `[user,assistant,tool,assistant]`，无重复 user 消息，`open_turn` 已清 ✅ |
| B 续跑（无注记版） | 模型直接收尾，没重发 `echo hi` → 发现静默消失问题 → 加注记 ✅（现象本身符合"panel 属于死回合"的既有论证） |
| B3 续跑（有注记版） | `[resume]` 注记落盘（第 4 条，synthetic），marker/`open_turn` 已清 ✅；模型仍判定 `echo hi` 不需要而收尾——注记保证了"显式定夺"而非"静默消失"，机制已闭环；重发与否则是模型行为，不在代码保证内（诚实边界） |
| 干净会话按 `ctrl+x r` | 无 action → 前端 no-op；后端 guard（`open_turn is None → ValueError`）为后盾 ✅（单元测试覆盖） |

## 5. 待拍板（问卷在 chat 里，以问卷为准）

- **Q1**：`resume_turn` 独立方法 vs `_run_turn(append_user=False)` 开关？
- **Q2**：`turn_count` 续跑时 +1 还是不动？（`open_turn` 语义跟着走：不动则崩了还是同一 N；+1 则 notice 显示新 turn）
- **Q3**：旧 marker 在 resume 开始时清掉（新回合会重问），还是留到被覆盖/回答？
- **Q4**：前端落点：notice 本体可点击 vs 会话面板加"续跑"按钮？
- **Q5**：情形 B 万一模型没重发同一调用（极小概率），残留 marker 下次 load 又 notice——可接受，还是要一个"忽略"操作？
