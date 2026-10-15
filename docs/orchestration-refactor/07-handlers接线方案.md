# 编排层深改 · 07 handlers 接线方案（恢复事实 → UI）

> P1–P4 已把三个恢复事实落盘并经**真实链路**验证（日志级）。本篇只解决**最后一公里**：让用户**看得见**。
> 前置：`SessionService.load` 已填 `recovered_interrupted` / `pending_approval`；`session.json` 已有 `openTurn` / `undoState` 语义。

---

## 1. 目标与范围

**做**：`session_load` 把两个恢复事实传给前端；前端渲染成一条 `notice`。
**不做**（本轮）：**自动/一键续跑未完成回合**——那需要一个**新的 loop 入口**（不追加 user 消息地继续），属新能力而非接线。见 §8 待拍板。

---

## 2. 现状（改前）

| 路径 | 现状 | 缺口 |
|---|---|---|
| `handlers.initialize`（189） | **新建**会话，不恢复旧会话 | 启动时无"上次会话"可报 |
| `handlers.session_load`（236） | `sessions.load(id)` → 返回 `{id,title,messages}` | **丢掉了**刚读出来的两个恢复事实 |
| `replay.ts::loadSession`（156） | `session.load` → `messagesToState` → `SessionState` | 无 notice 通道 |
| `message.tsx:81` | **已有** `part.kind === "notice"` 渲染 | 现成，直接用 |
| `session.ts:43` | `Part` 已有 `{kind:"notice", tone, text}` | 现成，直接用 |

**结论**：渲染通道**早就存在**（`notice` part），只差后端把事实传出来、前端把它变成一条 notice。**零新事件类型、零新渲染逻辑。**

---

## 3. 方案

### 3.1 后端：`session_load` 多返回两个字段
```python
    def session_load(params):
        session = world.sessions.load(session_id)
        ...
        return {
            "id": session.session_id,
            "title": session.title,
            "messages": [...],
            # P5: 两个恢复事实（load 已算好，此前被丢掉）
            "recoveredInterrupted": bool(session.recovered_interrupted),
            "openTurn": session.open_turn or 0,
            "pendingApproval": dict(session.pending_approval or {}),
        }
```
- **为什么放响应里**：与 `session_prompt` 的 `rejected` 同一先例（"rides the response the client already awaits, so no new IPC call and no new event type"）。**不新造事件。**

### 3.2 前端：`loadSession` 把事实变成一条 notice
```ts
export async function loadSession(client, id): Promise<SessionState> {
  const data = await client.request("session.load", { id })
  const state = messagesToState(data.messages ?? [])
  const note = recoveryNotice(data)          // 纯函数，见下
  if (note) state.messages.unshift({ role: "assistant", parts: [note] })
  return state
}
```
`recoveryNotice(data)`（纯函数，**唯一新代码**）：
- `recoveredInterrupted` → `{kind:"notice", tone:"warn", text:"上次回合未完成（turn N）——重发消息即可继续"}`
- `pendingApproval` 非空 → `{kind:"notice", tone:"warn", text:"上一步在等审批：<tool> <params 摘要>（该回合已中断）"}`
- 两者都无 → `null`（**行为与改动前逐字节一致**）

> **为什么是 notice 而不是重发审批面板**：审批属于**那个已死的回合**。崩溃后没有活着的回合可以接收答案，重发面板只会拿走一个**无处可去**的回答。诚实的做法是**告诉用户发生了什么**，由他重发消息（模型重发该调用时会自然重新审批）。

---

## 4. 数据流

```
改前: session.load → {id,title,messages} → messagesToState → 界面
改后: session.load → {…, recoveredInterrupted, openTurn, pendingApproval}
      → messagesToState → recoveryNotice() → 顶部一条 warn notice
（live 会话的 approve 路径完全不动）
```

---

## 5. 文件级改动（每步）

| 文件 | 步 | 内容 |
|---|---|---|
| `server/handlers.py` | 1 | `session_load` 返回体加 3 键（读已算好的字段，**无新查询**） |
| `state/replay.ts` | 2 | `loadSession` 读响应新键 → `recoveryNotice()` → `unshift` 一条 notice |
| `state/replay.ts` | 3 | 导出纯函数 `recoveryNotice(data) -> Part \| null` |
| `test/replay.test.ts`（新或既有） | 4 | R1–R4（见 §6） |

**后端测试**：`tests/test_server_ipc.py` 加一例（`session_load` 返回两个字段）。
**零改动**：`message.tsx`（notice 渲染已有）、`session.ts`（Part 已有）、`bridge.py`（不走事件）、`AgentLoop`（不碰）。

---

## 6. TDD / RIPR 用例

| # | Red | Implement | Prove | Refactor |
|---|---|---|---|---|
| R1 | `test_session_load_reports_recovered_interrupted`（后端）：造一个 `openTurn` 的会话 → `session_load` → 响应含 `recoveredInterrupted: true` | §5.1 | 干净会话为 `false` | — |
| R2 | `test_session_load_reports_pending_approval`（后端）：造 marker → 响应含 `pendingApproval.toolName` | §5.1 | 无 marker 为 `{}` | — |
| R3 | `recoveryNotice` 纯函数：a) 崩了→warn 文案；b) 有待决→warn 文案；c) 都没有→`null` | §5.3 | 逐分支 | — |
| R4 | `loadSession` 把 notice 放在最前；无恢复事实时**不加任何东西**（回归护栏：老会话逐字节不变） | §5.2 | — | — |

---

## 7. tmux 验证方案（截图）

**为什么必须 tmux**：这次改的是**用户看得见的那条线**（后端响应 → 前端 notice），而单元测试只到 `SessionService.load` 为止。**只有真跑 TUI 才能证明 notice 真的画出来了。**

```
① 造崩溃现场（复用 P1–P4 的验证脚本，真 SIGKILL）：
     python /tmp/opencode/verify_recovery.py crash      # 崩在回合中 → 留 openTurn
     python /tmp/opencode/verify_recovery.py approval   # 崩在审批中 → 留 marker
② 起 TUI：
     tmux new -d -s rec -x 120 -y 40
     tmux send-keys -t rec 'ai-coding -w /tmp/opencode/recovery-ws' Enter
③ 打开会话面板 → 选中崩溃的那个会话（会话对话框走 session.load）
④ tmux capture-pane 截图，确认：
     - 顶部出现 warn 色 notice："上次回合未完成（turn 1）…"
     - 第二个场景额外出现："上一步在等审批：bash …"
⑤ 对照：干净会话**不出现**任何 notice
```
产出：`tmux capture-pane -p` 的文本截图 + 必要时 `tmux capture-pane -e`（带颜色）。

**注意**：`initialize` **新建**会话、不恢复旧会话，所以必须**从会话对话框进入**（那是唯一走 `session.load` 的路径）。若你希望**启动即恢复上次会话**，那是另一个功能（§8）。

---

## 8. 待你拍板

1. **notice vs 一键续跑**：本轮只做 notice（我推荐）。"批准后从断点继续"需要**新的 loop 入口**（`resume_turn`：不追加 user 消息、从 checkpointed 历史继续），是**新能力**，建议单独一轮。
2. **启动是否自动恢复上次会话**：现在 `initialize` 永远新建。要不要"启动即打开上次会话"？（那会让 notice 在启动时就可见，不用点面板。）

---

## 9. 砍掉的（说不清作用/冗余）

- **不新造事件类型**：`notice` part 与响应字段都已有先例（`rejected`、`console.line`）。
- **不做"重发审批面板"**：没有活着的回合接收答案（§3.2）。
- **不动 `bridge.py`**：恢复事实是 `session.load` 的响应，不是 live 事件。
- **不加 `session.recovery` 之类的 IPC**：一个响应字段就够。
