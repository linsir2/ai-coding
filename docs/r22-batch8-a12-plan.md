# 批次 8-5 方案（R22·A-12 切换窗口内的提交仍带旧 id）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 5 项、`docs/r22-feature-matrix.md` §8 A-12。

## 1. 调研：窗口是怎么来的

后端的两条"切会话"路径都是**先切、后回复**：

- `server/handlers.py::session_load`（512-518）：读到文件就把 `state.session` / `state.session_id`
  换成新的，再返回消息；
- `/resume`、`/clear` 的 `turn.finished(action="new_session")`：命令在 `handle_input` 里已经切过，
  事件只是事后通知。

前端是**先回复、后切**：`app.tsx:253-259` 的 `onSelectSession` 先 `await loadSession(...)`，
再 `switchTo(id, state)`；`switchTo` 才更新 `props.sessionId`。在 `await` 期间：

- 路由还是旧会话，`submit` / `dispatch` 用 `props.sessionId` 组装
  `{id: 旧id, text}`（`routes/session.tsx:172,305`）；
- 后端 `session_prompt`（753-767）与 `session_command`（906-926）都校验
  `session_id == state.session_id`，回 `session not loaded: <旧id>`；
- 用户看到的是 `submit-failed`，输入没有丢进新会话。

窗口长度 = 一次 IPC 往返（本机毫秒级，远端/大历史可达百毫秒）。`/resume`、`/clear`、
会话面板切换、新建会话、删除 live 会话都会经过它（后三者是同步 `switchTo`，窗口更短）。

## 2. 方案（前端：切换期间把提交挂起，落地后按新 id 发）

**核心：** 路由不再"猜 id"，而是等切换结束。Shell 暴露一个 `switching` 访问器，
路由在它为真时把提交放进一个**内存暂存**；切换落地（`props.sessionId` 变化且
`switching` 变假）后，用新 id 真正发送。

### Shell（`app.tsx`）

```ts
const [switching, setSwitching] = createSignal(false)

const onSelectSession = async (id: string) => {
  setSwitching(true)
  try { switchTo(id, await loadSession(props.client, id)) }
  catch { /* 保持当前会话 */ }
  finally { setSwitching(false) }
}
```

`onNewSession` 同理；`<Session switching={switching} …>`。

### 路由（`routes/session.tsx`）

1. 现有 `dispatch` 体改名 `dispatchNow`；新的 `dispatch`：

   ```ts
   const deferred: { text: string; images: PromptImage[] }[] = []
   const dispatch = (text: string, images: PromptImage[] = []) => {
     if (props.switching?.()) {
       deferred.push({ text, images })
       return
     }
     dispatchNow(text, images)
   }
   ```

   这样 `submit`、palette 命令、回合结束的排队回放、plan-run 自动派发都被同一个闸门覆盖。

2. 落地/失败两路都要放行：

   ```ts
   createEffect(() => {
     const switching = props.switching?.() ?? false
     void props.sessionId          // 追踪 props 更新
     if (switching || deferred.length === 0) return
     const [first, ...rest] = deferred.splice(0, deferred.length)
     dispatchNow(first.text, first.images)   // 新 id 已就位
     for (const row of rest) queueText(row.text)
   })
   ```

   - 暂存项记下"入队时的 sessionId" 与"是否要回声"：放行时 `props.sessionId !== from` 才算切换成功，
     用新 id 发，并按调用方意图重放 `submitted` 回声（实现时改成"挂起期间**不**先写回声"，
     所以失败路径不会留下幽灵用户行与卡死的 `busy`——评审阻塞项）；
   - 切换失败（id 没变）：**不发**——评审指出 `new_session` 路径上后端已经切过会话，
     旧 id 已非 live，按旧 id 发只会第二次失败并丢输入。改为 `setPromptSeed(text)` 把这一行放回
     输入框并 toast 提示（图片无法回填，罕见路径，记在方案里）；
   - 多于一条时只放行第一条，其余走既有队列（`session.inbox` 不校验 id，落进当前 live 会话）。
   - 顺序：flush 的 effect 必须建在 hydrate effect（`session.tsx:748`）**之后**，否则回声先写、
     hydrate 后抹。

3. 不在 `submit` 里单独判断：它内部调用的就是 `dispatch`，一个闸门足够；`editingInboxId`
   分支（编辑旧会话的排队行）保持原样——那本来就不花回合，失败是静默的 `ok:false`。

### 闸门的覆盖范围（评审更正）

- 会话面板（`openSessions` → `DialogSessions` → `onSessionLoaded`）**不经过 `await` 窗口**：
  面板打开期间焦点在它的搜索框，路由收不到提交；选中时 `session.load` 在面板自己的 await 里完成，
  回来直接同步 `switchTo`。原方案把它算进窗口是错的。
- `state.busy` 分支（steer / queueText）不经过 `dispatch`，但这两条后端路径都**不校验 id**
  （`session_steer` 用 `state.session`；`session_inbox` 无 id），所以它们本来就免疫这个竞态。
- 因此本轮只覆盖真正带旧 id 的两条：`/resume`/`/clear` 的 `turn.finished(new_session)` 触发的
  `onSelectSession`，以及它的 `reload` 并发分支期间的用户提交。

### 不做

- 不改后端的 id 校验（严格是对的：真正的客户端失步应当报错并让前端重载）；
- 不改 `switchTo`/`loadSession` 的先后顺序（先拿到数据再切，才不会闪空屏）；
- 不给删除 live 会话单独加闸门：它的 `switchTo` 在删除应答之后同步执行，窗口内提交会被后端
  以 `session not loaded` 拒绝，**修复收益低、改动面大**。
  （实现时把**新建会话**也纳入了闸门——`session.create` 同样是"后端先切、前端后切"的 await，
  与 `loadSession` 同形；`onNewSession` 现在也计数。）

## 3. 测试（先红后绿，渲染级）

在 `test/session-card-composer.test.tsx` 的 `_mount` 上增加可选的
`{ sessionId?: () => string; switching?: () => boolean }`，新增：

| 测试 | 断言 |
| --- | --- |
| 切换期间提交挂起 | `switching=true` 时 `keymap.runCommand("session.review")` → `client.sent` 里没有任何 `session.command`，更没有 `{id:"s1"}` |
| 落地后用新 id 发 | 改 `sessionId→"s2"`、`switching→false` → 等到一条 `session.command`，`params == {id:"s2", text:"/review"}`；全程没有 `id:"s1"` 的请求 |
| 切换失败不猜 id | `switching→false`、id 不变 → **不发**任何带 id 的请求；输入被放回输入框（帧里能看到），有 toast |

## 3.1 完成记录（2026-10-29）

- 实现了闸门（`dispatch` 返回"是否已发"）+ 暂存（`from`/`echo`）+ flush effect（建在 hydrate
  effect 之后）+ 失败回填；Shell 用计数而不是布尔。
- 评审并入：挂起期间不写回声（失败不留幽灵行/卡死 busy）；失败不按旧 id 猜；新建会话也纳入闸门；
  文档更正"会话面板也要过窗口"的说法（它没有窗口）。
- 测试：`test/session-card-composer.test.tsx` 新增 2 例（挂起→新 id；失败→回填且不发），
  全量前端 552 例 + `tsc` 干净。
- 未做（明确）：删除 live 会话的窗口；`switchDepth` 本身的单测（新例注入 props 覆盖了行为路径）。

## 4. 验收

1. 上述 3 例 + 既有前端 550 例全绿、`tsc` 干净；
2. 不改后端，`pytest` 不受影响（只跑一次全量门禁确认）；
3. 真机复验成本高（窗口毫秒级）：以渲染级测试 + `session.load`/`session_command` 的 id 校验
   在两端的时序为证据；不再用 tmux 抢键。
