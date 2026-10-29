# 批次 5 修复方案（R22·A-11）

> 输入：`docs/r22-feature-matrix.md` §8 A-11（真机帧 96–99）。只做 A-11，其余（A-9 调色板、A-4 错误码、A-10 时长、H10 引用注入）各走独立方案。

## FIX-6（P1）`/resume`·`/clear` 之后的重投影用了旧 sessionId

**实测**：`/resume 957ab4e2` 切会话成功（页眉/侧栏都对），紧接着 `/compact` 在 transcript 里打出

```
[ERROR] Error: ValueError: session not found: 0330a025797940398d93f8ca510ff046
        at ResponseError (.../vscode-jsonrpc/lib/common/messages.js:106:9)
```

`0330a025…` 是**启动时那个从未落盘的空会话**（`sessions/` 下没有这个文件），`session_load` 因此抛错。
`/clear`、`/resume` 之后任何"触发重投影"的命令（`/compact`、`/undo`）都会撞上。

**根因**（`ai_coding/frontend/src/routes/session.tsx:558-589`，同一次 `turn.finished` 里两条分支抢跑）：

```ts
if (action === "new_session") { props.onSelectSession(id) }      // 父层状态，异步生效
if (event.data["reload"] === true) { await loadSession(props.client, props.sessionId) }  // 这一行仍读旧 id
```

**改法（最小、可单测）**：
- 新增纯函数 `reloadTarget(action, eventSessionId, propSessionId)`（放 `util/history.ts` 旁，或同文件）：
  `action === "new_session"` 且有事件 id → 用事件 id；否则 → 用 `propSessionId`（`/undo`、`/redo` 是同会话重写，
  必须留在当前会话）。
- reload 分支改为 `loadSession(props.client, reloadTarget(...))`。
- 不合并两个分支：`onSelectSession` 负责切标签页，reload 负责重投影正文，各自职责不变。

**测试**（`frontend/test/history.test.ts` 或新增 `session-switch.test.ts`）：
1. `new_session` + 事件 id `abc` + 旧 id `old` → `abc`；
2. `new_session` 但事件没带 id（老后端）→ 退回 `old`（不猜）；
3. `reload`（undo/redo）→ 恒为 `old`；
4. `continue` + 事件 id 存在但没切会话 → `old`（只有 new_session 才换目标）。

**风险**：若后端某天在 `new_session` 时 `sessionId` 与 `onSelectSession` 不一致，两个视图会分叉——所以
测试 1 与 `routes/session.tsx:565-567` 的 `onSelectSession` 用**同一个** `id` 变量，避免二次求值。

**真机复验**：`/resume <id>` → `/compact` 必须打出 `── 压缩 ──` 块且不出现 `session not found`；
顺带把 FIX-3（`/undo` toast）与 FIX-4（空压缩不清零）一起复验，这两项此前被 A-11 挡住。

---

## 完成记录（A-11 已落地并真机复验）

| 项 | 改动 | 测试 | 真机复验 |
| --- | --- | --- | --- |
| FIX-6 | `frontend/src/util/history.ts::reloadTarget(action, eventSessionId, propSessionId)`；`routes/session.tsx` 的 `turn.finished` 重投影改用事件里的权威 id，`new_session` 但事件无 id 时**跳过**重投影（交给路由自己的 load）；投影失败的 IIFE 补 `catch` → 走 `submit-failed` 的通知行，不再抛未处理 rejection | `frontend/test/history.test.ts` +4（切换用事件 id / 无 id 跳过 / undo·redo 留本会话 / 普通命令不变）；全量 **529 通过**、`tsc` 干净 | `/resume 957ab4e2` → `/compact`：`[compact] messages 8 → 8 (removed 0)`，**transcript 不再出现 `session not found`**（帧 100–101）|

### 顺带复验了被 A-11 挡住的两项

- **FIX-4（A-8）**：同一次 `/compact` 之后，会话文件里的 `lastCompactedTurn` **仍是 2**（`turnCount=2`、`msgs 8`），
  即"什么都没压"不再抬边界 —— 计量不会被误清零（帧 101 + 会话文件前后对比）。
- **FIX-3（A-3）**：`/undo` 的 toast 现在显示 **`Undone  8 → 1 messages`**（此前是 `Compacted 7 → 0`），帧 102。

### 评审并入（第二轮）

| 评审意见 | 处置 |
| --- | --- |
| 事件缺 `sessionId` 时"退回旧 id"会重演崩溃 | **已改**：返回 `null`，跳过重投影 |
| 重投影 IIFE 无 `catch`，失败成为未处理 rejection | **已改**：`.catch` → `submit-failed`（走既有的 `[ipc]` 通知行）|
| 删掉 `new_session` 分支的 reload 是否更简 | 未采纳：会丢掉本地队列迁移（`session.tsx:579-591`），且父层 load 失败被静默吞 |
| 切换窗口内 `buildSubmission` 仍用旧 id（后端会报 `session not loaded`）| **记录为新条目 A-12**（见功能矩阵 §8），留批次 6 |

## 遗留

- A-9（调色板滚动/搜索）、A-4（沙箱错误码）、A-10（工具时长含审批等待）、A-12（切换窗口内的提交）、H10（`referenced_files` 超预算）各走独立方案。
