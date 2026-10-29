# 批次 9-6 方案（R22·U-1 `/tasks` 之后卡在 `Working…`）

> 只出方案。依据：`docs/r22-feature-matrix.md` §5 U-1、帧 `15-tasks`、
> `server/handlers.py::session_command` 与 `frontend/src/state/session.ts` 的 turn 归属逻辑。

## 1. 调研：卡住的确定性机制

`/tasks` 是一个**快速命令**，走 `session.command`：

1. 前端先 `apply({kind:"command-started"})` → `busy=true`，`turnId` 仍是 `undefined`
   （`routes/session.tsx:193`）；
2. 后端 `session_command` 先把 `turn_id += 1`，`service.spawn(run_command())`，然后**立即返回**
   `{turnId}`（`handlers.py:931-932/993-996`）；
3. 前端在 `.then` 里才 `apply({kind:"turn-started", turnId})`（`routes/session.tsx:196-201`）；
4. reducer 的 `turn.finished` 有 D7 守卫（`state/session.ts:847-852`）：

   ```ts
   const ending = num(data["turnId"])
   if (ending !== 0 && state.turnId !== ending) return state   // 丢弃
   ```

   命令很快时，`run_command` 的终态事件可能在 ack 之前到达；此时 `state.turnId` 是
   `undefined`、`ending` 是正整数，条件成立 → **事件被丢弃，`busy` 永远为 true**。
   之后输入被当作"下一轮"排进 inbox，Esc 只清 cancel 不清 busy，只能重启。

D7 的本意是"旧回合的终态不能解开新回合的 busy"（`tests/turn-ownership.test.ts:24-31`），
所以不能简单删掉守卫；缺的是"终态早于 ack 到达时，把它的 id 记下来，等 ack 到了再结算"。

## 2. 方案（reducer 状态机，一个可选字段）

`SessionState` 增加：

```ts
/** U-1：终态先于 ack 到达的回合 id。ack 迟到时用它结算，而不是永远 busy。 */
orphanTurnId?: number
```

`turn.finished`：
```ts
const ending = num(data["turnId"])
if (ending !== 0 && state.turnId !== undefined && state.turnId !== ending) return state
if (ending !== 0 && state.turnId === undefined) {
  // 这个终态属于本客户端刚发起、ack 还在路上的回合：记下 id。
  return state.busy ? { ...state, orphanTurnId: ending } : state
}
```
（无 id 的终态保持现有行为：直接结算。）

`turn-started`：
```ts
if (action.turnId === state.orphanTurnId) {
  // 它已经结束了；ack 只是迟到。
  return { ...state, busy: false, turnId: undefined, orphanTurnId: undefined }
}
return { ...state, turnId: action.turnId, orphanTurnId: undefined }
```

- **D7 仍然成立**：旧回合终态(1) 先到 → orphan=1；新回合 ack(2) 到 → id 不匹配，清 orphan、
  注册 2、busy 保持 true。`turn-ownership.test.ts` 既有用例全部不变。
- **U-1 修复**：终态(5) 先到 → orphan=5；ack(5) 到 → 匹配，busy=false。
- 无定时器、无协议变更、无后端改动；终态事件仍只认 id。

## 3. 影响面

- 前端 reducer 与状态类型；`initialState()` 不需要新字段（可选）；
- replay/加载路径用整份 state 覆盖，orphan 自然不存在；
- 后端、IPC、轨迹、导出均不变。

## 4. RIPR

### Red（`tests/turn-ownership.test.ts`）
| 用例 | 断言 |
| --- | --- |
| `test_an_ending_that_arrives_before_its_ack_settles_on_the_ack` | `command-started` → 终态(5)（busy 仍 true）→ `turn-started(5)` → busy=false（今天为 true） |
| `test_an_older_ending_does_not_settle_the_new_turn` | `command-started` → 终态(1) → `turn-started(2)` → busy=true、turnId=2、orphan 清空 |
| `test_an_orphan_ending_while_idle_is_ignored` | busy=false 时收到终态(5) → busy 仍 false、orphan 不设置 |
| 既有 4 例 | 不改，继续通过（D7 保护） |

### Implement
`state/session.ts` 三处（接口字段、`turn.finished`、`turn-started`）。

### Prove
`bun test test/turn-ownership.test.ts test/reducer.test.ts test/plan-prelude-busy.test.tsx`；
全量 `bun test` + `bun run typecheck`。

### Refactor
无新 helper、无定时器；一个可选字段 + 两个分支。

## 5. 真机与验收

1. 真机回归：在 `r22-s1` 连续执行 `/tasks`、`/config`、`/budget` 各多次，composer 每次都回到
   `Ask anything…`（不再停在 `Working…`）；帧存 `/tmp/r22-shots/165-*`；
2. 竞态本身无法在真机稳定复现（取决于 ack 与事件谁先到），以 reducer 级红/绿用例为准；
3. 前端全量 + `tsc`；后端不受影响。

## 6. 完成记录

- 实现：`SessionState.orphanTurnId`；`turn.finished` 在 `turnId` 未注册且 busy 时只记住 id
  （不写 transcript，避免把旧回合的 marker 记到新回合，D7）；`turn-started` 命中该 id 时结算。
- 评审：两轮。第一轮指出"把整条终态延后写 transcript"会违反 D7（旧回合 marker 污染新回合）——
  已回退为"只延后 busy、不碰 transcript"；第二轮确认主路径可合并。
- 测试：`turn-ownership.test.ts` 8 例（U-1 主例在旧实现上红）；`reducer`/`plan-prelude-busy`
  共 82 例绿；前端全量 **566 通过 / 0 失败**，`tsc` 干净。
- 真机（帧 165）：`/tasks`、`/config`、`/budget` 连跑 3 轮，composer 每轮都回到
  `Ask anything…`。
- **遗留 U-2（新，P3 观察）**：本竞态发生时，ack 结算 busy 的时机不在 `turn.finished` 事件处理器里，
  因此"结算前几毫秒内被排进 inbox 的行"不会自动派发，要等下一轮；永久 `Working…` 已消除。
  详见矩阵 §5 U-2。
