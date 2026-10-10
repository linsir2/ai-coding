# A4 设计：转录顺序 / Thought 交互 / 布局层级 / 会话入口 / 权限对话框

> 状态：**待拍板**（未写一行实现代码）
> 前置：`opencode-gui-a3-design.md`（A3 已提交 `210929d`）
> 触发：4 条实测抱怨 —— ①顶部历史会话关不掉 ②输入框越过右侧栏 ③`+ Thought` 点不开 ④正文结束后多出一个 `+ Thought`

## 0. 范围与原则

| 原则 | 落地 |
|---|---|
| 只改根源 | 每处改动对应一个已定位的根因，无顺手重构 |
| 复用不造轮 | 命中的既有件：`shorten`、`onMouseUp`、`permissions-dialog.ts` helpers、`dialog.open`、`DialogSelect` |
| 说不清就砍 | §3 列出被砍掉的 5 个"看起来该做"的东西及理由 |
| 零后端改动 | 本轮全部前端。后端契约（`permissions.state` / `session.command`）在 A3 已就位 |

**改动文件 11 个**（含 2 新增、1 重命名），后端 0 个。

---

## 1. 现状核准（事实，带行号）

| 文件 | 现状事实 |
|---|---|
| `state/session.ts:192-208` | `message.finished` 先 finalize text part，再 `if (reasoning) next = [...next, {kind:"reasoning"}]` —— **reasoning 被追加到 parts 末尾** |
| `state/session.ts:202` | `else if (authoritative !== null)` —— `text:""` 的纯工具步会造出一个空 text part |
| `state/replay.ts:82-87` | 重载路径同构：先 push text 再 push reasoning —— 同一个错序的第二处实现 |
| `server/bridge.py:96-102` | 每个 assistant step 一条 `message.finished{text, reasoning}`；空 text 真实存在（part 预览处 `if not inner.text: return None`） |
| `component/reasoning.tsx:19` | `props.expanded` 由上层传入；组件内无状态、无鼠标事件 |
| `routes/session.tsx:73` | `const [thinking, setThinking] = createSignal(false)` —— **视图级单值**，`ctrl+t` 一次切全屏 |
| `routes/session.tsx:293-317` | `<box row flexGrow>` = scrollbox(`flexGrow`) + Sidebar(`width=42`) |
| `routes/session.tsx:318-345` | composer 是上面那行的**兄弟节点** → `Prompt` 的 `width="100%"`（`prompt.tsx:154`）横跨全终端，底边框 `▀▀▀▀` 从右侧栏底下穿过 |
| `component/session-tabs.tsx` | 只有 `onSelect` / `onNew`，**无任何关闭入口**；数据源 `app.tsx:162 tabs()`（最近 5 条） |
| `app.tsx:162-191, 361-374, 393` | 假 tab 全套：`cycleSession` / `selectTab` / `session.next` / `session.prev` / `session.tab.select.1..5` + `<leader>1..5` 键 |
| `app.tsx:352-359` | `session.permissions` → `startSession("/permissions ")`（从 Home 开新会话只为打一行字） |
| `routes/session.tsx:189` | 会话层同名命令 → `setPromptSeed("/permissions ")` |
| `component/dialog-status.tsx` | A3 刚把权限区块塞在这里；props 带 `sessionId`（逐会话语义），而权限是 workspace 全局 |

**鼠标可行性已实测**：tmux 发 SGR 序列点击 `+ New` → 真的建了新会话（`session-tabs.tsx` 的 `onMouseUp` 生效）。B3 的点击方案**不需要任何新基建**。

---

## 2. 根因（第一性原理）

**B4（错序 + 尾部 thought）** —— 我们的 wire 把「一个 assistant step 的 reasoning + text」压成**一条** `message.finished`；OpenCode 是逐 part 推送，顺序由服务端天然保证。客户端要自己重建顺序，而重建规则写错了：`append` 应为 `insert-before`。同一条规则在实时流（reducer）与重载流（replay）各实现一次，两处都错 → **必须同时改两处，这是同一契约的两个实现点，不算冗余**。空 text part 是同一处判断的第二个错误（`!== null` 应为"非空"）。

> **实施中发现的第二个根因（视图层，比上面那个更隐蔽）**：`message.tsx` 的 `PartView` 在**函数体里** `switch (props.part.kind)` 分支，而转录用的是 `<Index>`——它按**位置**复用实例。`message.finished` 落地时，位置0 的 part 从「流式 text」变成「reasoning」，实例被复用、函数体重跑不了，于是旧分支继续生效：`props.part.text`（此刻是推理全文）被当**正文 markdown** 渲染。
> 症状正是用户看到的「一大段没有 `+ Thought` 头的散文」——包括「输出结束后最后还来一个 thought+」。修法：`PartView` 改用 `<Switch>/<Match>`（Solid 的分支是响应式的），不是 `switch` 语句。
> 证据（临时诊断，已撤除）：reducer 产出 `PRODUCED ["reasoning","text"]`，而同一时刻渲染层只调用了 `PART text`。

**B3（点不开）** —— 折叠态被放在**视图级单值**（`ctrl+t` 切全屏），而它属于**每个 part**。`reasoning.tsx` 只渲染、不持有状态、也没绑交互 → 无处可点。契约纠正：折叠是 part 的属性。

**B2（输入框越界）** —— 层级错误。composer 应该是左列的**最后一个子节点**，而现在它是"左列+右栏"那一行的兄弟。参照 OpenCode `routes/session/index.tsx:1365-1372`：`flexGrow` 左列内含 transcript 与 `Prompt`，Sidebar 是外层 row 的兄弟。

**B1（关不掉）** —— 组件背了"tab"语义，后端却没有 tab 这个概念（单 live 会话，见 `handlers.py:150-179`）。于是渲染出一排看得见、点得动、唯独关不掉的假 tab。**这不是缺功能，是模型错位**——按已定决策，不做 tab，改为会话面板入口（`<leader>l` 已存在）。

**权限** —— 全局策略挂在逐会话面板上，且两个 palette 入口都是"开个会话打一行字"。按已定决策提升为 app 层全局对话框。

---

## 3. 新契约清单（含"为什么需要"与"复用了什么"）

### 新增（2 个）

| 契约 | 作用 | 复用 | 砍它的代价 |
|---|---|---|---|
| `reasoningHeadline(text, cap=72)` | 折叠态标题：取首个非空行、切到第一个句末、`shorten` 截断 | `util/toolmeta.ts:37 shorten` | 折叠后只剩 `+ Thought` 三个字，等于没信息（用户已明确要首句） |
| `DialogPermissions{client, liveSessionId}` | 全局权限对话框；`liveSessionId` 只是派发通道 | `util/permissions-dialog.ts` 全部 helpers + `dialog.open` | 全局策略继续挂在逐会话面板上（已定否决） |

### 改名 / 改语义（2 个）

| 契约 | 从 | 到 | 理由 |
|---|---|---|---|
| `Reasoning` props | `{text, expanded}` | `{text, alwaysOpen}` | `expanded` 由上层给 = 视图级；`alwaysOpen` = 视图级模式开关，单 part 的展开收进组件内部 |
| `MessageView` / `PartView` prop | `thinking: boolean` | `alwaysOpen: boolean` | 同上，名字即语义 |

### 删除（1 个）

`SessionTabs`（49 行 + `app.tsx` 的 `tabs()`/`cycleSession`/`selectTab`/`session.next`/`session.prev`/`session.tab.select.1..5` + `<leader>1..5`）→ 换成一个只显示当前会话、点击打开面板的 `SessionHeader`。

### 明确砍掉（5 个看起来该做但不做的东西）

| 砍掉 | 理由 |
|---|---|
| reasoning 耗时显示 | wire 无时间戳；要加就得动后端 → 越界。用户也只选了首句 |
| OpenCode 的 `Thinking: xxx` spinner 态 | 后端 reasoning 只在 step 结束整块送达（无 delta），该分支永远是死代码 |
| OpenCode 的 `**bold**` 标题正则（`thinking.ts:11`） | 我们的模型不写这个约定，正则几乎恒为 null = 死代码 |
| 空 reasoning 的 UI 侧兜底 `<Show when={content()}>` | 已在 reducer/replay 源头拦掉；两处守卫是冗余 |
| `session.new` / `<leader>n` | 与本次改动无关，保留 |

---

## 4. 逐文件实施（每步只做一件事）

### 步骤 1 — `state/session.ts`（B4 根源 a）
`message.finished` 的重建规则改为：

```
index = 最后一个 streaming text part 的下标
有 reasoning(trim 后非空) → 插入到 index 处（即该步 text 之前）
无 index 且 authoritative 非空 → [...parts, reasoning?, text]
无 index 且 authoritative 为空/缺失 → [...parts, reasoning?]   // 纯工具步不再造空 text part
```

### 步骤 2 — `state/replay.ts`（B4 根源 b）
82-87 两段 push 调换：先 reasoning 后 text。同一契约的第二个实现点。

### 步骤 3 — `util/reasoning.ts`（新，B3）
```ts
export function reasoningHeadline(text: string, cap = 72): string {
  const line = text.split("\n").map((s) => s.trim()).find(Boolean) ?? ""
  if (!line) return ""
  const stop = line.search(/(?:[。！？!?]|[.](?=\s|$))/)
  return shorten(stop >= 0 ? line.slice(0, stop + 1) : line, cap)
}
```
`.` 只在**后跟空格或行尾**时才算句末 —— 否则 `qwen3.7`、`e.g.` 会被误切（有专门用例钉住）。

### 步骤 4 — `component/reasoning.tsx`（B3）
- 组件内 `const [expanded, setExpanded] = createSignal(false)`
- `open = () => props.alwaysOpen || expanded()`
- 折叠头：`+ Thought` +（非空时）` · <headline>`
- 展开态：`- Thought` + 左边框 markdown（现有标记不动）
- `onMouseUp` 绑在头部 box 上，`alwaysOpen` 时不响应（对位 OpenCode `toggleable={inMinimal()}`）
- `text` 为空 → 返回空（防御，源头已拦）

### 步骤 5 — `component/message.tsx`（B3 接线）
`case "reasoning": return <Reasoning text={...} alwaysOpen={props.alwaysOpen} />`；`MessageView` prop 改名并透传。

### 步骤 6 — `routes/session.tsx`（B3 语义 + B2 层级）
1. 信号改名 `thinking` → `alwaysThinking`（`ctrl+t` 与命令 id 不变）
2. 布局：把 scrollbox 与 composer 包进同一个左列
```tsx
<box column width=100% height=100%>
  <SessionHeader title id onOpen />
  <box row flexGrow=1 minHeight=0>
    <box column flexGrow=1 minHeight=0>      {/* 左列 */}
      <scrollbox flexGrow=1 …>…</scrollbox>
      <box composer padding…>                {/* 权限/提问面板随它一起进来 */}
        …Prompt…
      </box>
    </box>
    <Show when={width>=100}><Sidebar width=42/></Show>
  </box>
  <StatusBar />
</box>
```
3. `SessionProps`：删 `sessions` / `onNewSession`，加 `onOpenSessions: () => void`。
   **更正**：`onSelectSession` **必须保留**——`/clear`、`/resume` 的 `new_session` 事件靠它跟随后端切走的新会话（`session.tsx:213`），它不是 tab 配套。实施时按原设计删掉会直接打断这两条命令。
4. 删 `session.permissions`（`setPromptSeed` 那条）—— 见 §9 待拍板 ③

### 步骤 7 — `component/session-header.tsx`（B1，git mv 自 session-tabs.tsx）
```tsx
export function SessionHeader(props: { title: string; id: string; onOpen: () => void })
```
一行：当前会话标题 + `id.slice(0,8)`，整行 `onMouseUp` → `onOpen()`（打开会话面板）。**只显示当前会话**，不再列历史 → 没有"关不掉"的假象；历史在 `<leader>l` 面板里。

### 步骤 8 — `component/dialog-permissions.tsx`（新，权限）
从 `dialog-status.tsx` 整体搬 A3 的权限区块：`permissions.state` 读、`p`/`c` 派发 `session.command{liveSessionId}`、footer 提示。props 用 `liveSessionId` 命名 —— **名字即"这只是传输通道，不是这个面板的会话"**。

### 步骤 9 — `component/dialog-status.tsx`（权限移出）
删权限区块 + `permissions.state` 请求 + `p`/`c` bindings + 不再需要的 import。回到纯只读状态面板（其余 6 行不动）。

### 步骤 10 — `app.tsx`（接线 + 删假 tab）
- 删：`tabs()`、`cycleSession`、`selectTab`、`tabSelects`、`session.next`、`session.prev`、`session.tab.select.*`、`<leader>1..5` bindings
- 删：`session.permissions`（`startSession("/permissions ")`）
- 加：`dialog.permissions.show`（group `System`，`palette: true`）→ `dialog.open(() => <DialogPermissions client liveSessionId={sessionId()} />)`
- 加：`<SessionHeader>` 的 `onOpen` 回调 → 复用现有 `dialog.sessions.show` 的同一个 `dialog.open(...)`（提到一个局部函数，两个入口共用，不复制 JSX）
- `Session` 调用处：删三个已废 prop，传 `onOpenSessions`
- `refreshSessions` / `currentTitle` **保留**（sidebar 标题依赖，见 §8 中窄屏说明）

---

## 5. 数据流影响

**转录（改前 → 改后）**
```
改前  message.finished{text:"答案", reasoning:"因为"}
      parts: [text(答案), reasoning(因为)]        →  正文在上，thought 吊在末尾

改后  parts: [reasoning(因为), text(答案)]        →  与 OpenCode 的 part 原生顺序一致

两步回合（用户截图场景）
改前  [text(""), reasoning, tool, text("长答案"), reasoning]   ← 空 part + 尾部 thought
改后  [reasoning, tool, reasoning, text("长答案")]
```
重载路径同步：`messagesToState` 产出的顺序与实时流逐字一致（这是"刷新页面后顺序不能变"的隐含契约）。

**权限（改前 → 改后）**
```
改前  palette → Permissions → Home 开新会话 → 往里灌 "/permissions "
      或   状态面板(逐会话) → p/c → session.command{该面板的 sessionId}

改后  palette → Permissions → 全局对话框 → p/c → session.command{liveSessionId}
      状态面板回到只读（Session/Model/Workspace/Context/Stop policy）
```

**其余**：IPC 方法集合不变、wire 事件集合不变、`Part` 类型不变、`SessionState` 不变。**零后端改动、零协议改动。**

---

## 6. TDD 用例（RIPR）

> **实施中修正**：原判断「没有组件渲染测试基建」是错的——OpenTUI 自带 `@opentui/solid` 的 `testRender` 与 `@opentui/core/testing` 的 `mockMouse`（`captureCharFrame()` 可断言画面）。用它为 `Reasoning` 写了 3 条真渲染测试（折叠行/点击展开/点击收起/alwaysOpen）。
> **harness 的已知边界**：它走 solid 的 **server build**，没有细粒度响应式——在渲染外部改 signal 不会触发重渲，所以「位置复用导致分支不换」这一类用例无法在此表达（已在测试文件里注明，改由 tmux 实测覆盖）。另外 harness 里 `<markdown>` 渲染为空白，故断言只覆盖本组件自己画的 marker 与首句，不断言库自己的正文。

| # | 用例 | R 可达 | I 交互 | P 预测 | R 显明 | 层 |
|---|---|---|---|---|---|---|
| T1 | `message.finished puts the step's reasoning before its text` | `reduce` 纯导出 | 喂一条 wire | `[reasoning, text]` | `toEqual` 失败时直接显示顺序差异 | bun |
| T2 | `a tool-only step leaves no empty text part`（`text:""`） | 同上 | 喂空 text | 只有 reasoning | 显示多出来的空 text part | bun |
| T3 | `whitespace-only reasoning produces no part` | 同上 | `reasoning:"  \n "` | 只有 text | — | bun |
| T4 | `a two-step turn keeps each reasoning in front of its own output` | 同上 | 工具步+正文步 | `["reasoning","tool","reasoning","text"]` | 打印 kind 序列 | bun |
| T5 | `messagesToState` 重载时 reasoning 在 text 前 | `messagesToState` 纯导出 | 传持久化消息 | `["reasoning","text"]` | — | bun |
| T6 | `reasoningHeadline`：中文/英文句末截断、`qwen3.7` 不误切、超 cap 截断、空与纯空白返回 `""`、跳过前导空行 | 纯函数 | 直接调用 | 逐条字面量 | — | bun |
| T7 | 权限对话框：palette 可达；`p` 后面板与 transcript 同时更新；`<leader>s` 的状态面板**不再**含权限 | 无渲染层 | tmux | 面板文本 | 抓 pane grep | E2E |
| T8 | 布局：宽终端下 composer 底边框右端 < 终端宽；窄终端（<100）行为不变 | 无渲染层 | tmux 量列 | 边框止于 Sidebar 左缘 | 打印该行内容与长度 | E2E |
| T9 | 会话头：只显示当前会话；点击打开面板；`<leader>1..5` 不再响应 | 无渲染层 | tmux | — | — | E2E |
| T10 | Thought 点击展开/再点收起；`ctrl+t` 仍是全局展开 | 无渲染层 | tmux SGR 鼠标（已实测可达） | 展开后出现左边框正文 | — | E2E |

**T1 会替换现有用例** `message.finished with reasoning appends a reasoning part`（`reducer.test.ts:67`）——它把 bug 顺序钉成了契约，必须改。这是有意改动现存断言，不是新增重复覆盖。

**回归基线**：bun 129 全绿 → 预期 129 - 1（被替换）+ 5 新增纯逻辑 = 133；pytest 2036 passed / 2 预存 failed / 2 skipped 不变；tsc / ruff / mypy 绿。

---

## 7. 架构图

**布局（改后）**
```
┌────────────────────────────────────────────────────────────┐
│ SessionHeader   当前会话标题 · id8        (点击→会话面板)   │  全宽
├──────────────────────────────────────┬─────────────────────┤
│ 左列 flexGrow=1                     │ Sidebar width=42   │
│  ┌────────────────────────────────┐  │  标题               │
│  │ scrollbox flexGrow=1           │  │  Context tokens    │
│  │  [reasoning] tool [reasoning]  │  │  N% used           │
│  │  [text]                        │  │  dir:branch        │
│  ├────────────────────────────────┤  │                     │
│  │ composer ┃ Ask anything…       │  │  ← 底边框止于左列   │
│  │          ┃ model · provider    │  │    右缘（不再穿右栏）│
│  └────────────────────────────────┘  │                     │
├──────────────────────────────────────┴─────────────────────┤
│ StatusBar  ~/dir:branch   tokens(N%)  ctrl+p  version       │  全宽
└────────────────────────────────────────────────────────────┘
```

**折叠状态所有权（改后）**
```
app/shell                session route            每个 reasoning part
─────────                ────────────            ────────────────────
ctrl+t ───────────────► alwaysThinking(bool) ──► open = alwaysOpen || expanded()
                                                     ▲
鼠标 onMouseUp（头部）────────────────────────────────┘ per-part signal
alwaysOpen=true 时该 part 不可单独切换（对位 OpenCode toggleable=inMinimal）
```

**权限（改后）**
```
Shell(app 层)
 ├─ palette: Permissions ──► DialogPermissions{client, liveSessionId}
 │        读: permissions.state {policy, grants[], storePath}
 │        写: session.command{liveSessionId, "/permissions auto|confirm|clear"}
 │             └─► 后端 handle_input ─► _install_providers (I1 不变)
 └─ palette: Status ─────► DialogStatus（纯只读，6 行，会话/模型/工作区/上下文）
```

---

## 8. 实施完成的最终形态（自查：逐条对照你的抱怨）

| 你的抱怨 | 实施后 | 怎么验 |
|---|---|---|
| 顶部历史会话关不掉 | 顶部只剩**当前会话**一行（标题+id），点击进面板；历史在 `<leader>l` 里可选可删（A3.3 的 `ctrl+d`） | T9 |
| 输入框右边太长、穿到右栏底下 | composer 进入左列，底边框止于 Sidebar 左缘；窄终端行为不变 | T8 |
| `+ Thought` 点不开、看不到思考 | 每个 part 独立，点一下展开思考全文，再点收起；折叠时显示推理首句 | T6 + T10 |
| 正文结束后多一个 `+ Thought` | reasoning 归位到它自己那一步的正文之前；纯工具步不再产生空 part | T1/T2/T4 |

**顺带说清两件我核实过、但不在本次范围的事**：
1. 宽终端下会话标题会同时出现在顶部会话头和右侧栏 —— OpenCode 也是两处都有（`index.tsx` header + sidebar）。窄终端（<100）时右侧栏不渲染，标题仍在顶部会话头，不会丢。
2. `ctrl+t` 从"全局切换"变成"全局常开"语义（OpenCode 同名行为）：关着时每个 thought 可单独点开；开着时全部展开且不可单独收起。

---

## 9. 待你拍板

| # | 事项 | 我的建议 | 不做会怎样 |
|---|---|---|---|
| ① | 删掉 `session.next` / `session.prev` / `<leader>1..5` / `session.tab.select.*`（假 tab 的全部配套） | **删**。它们只服务于假 tab，留着就是"能按快捷键跳到一个看不见的 tab" | 留着一堆指向不存在 UI 的命令键 |
| ② | 两个 `session.permissions` 入口（Home 的 `startSession("/permissions ")` + 会话层的 `setPromptSeed("/permissions ")`） | **删**，只留新的 Permissions 对话框。`/permissions` 仍可在输入框手打 | 同一个功能三个入口，且两个是"开个会话打一行字" |
| ③ | `SessionTabs` → `SessionHeader` 重命名（git mv） | **改名**。一个叫 SessionTabs 却不渲染 tab 的组件是谎，会误导后来人 | 文件名与内容长期不符 |
| ④ | T1 要**改掉**现有用例 `message.finished with reasoning appends a reasoning part`（它把 bug 顺序钉成了契约） | **改**。这是修正错误契约，不是新增重复覆盖 | 旧断言与新行为互斥，必须二选一 |
| ⑤ | `reasoningHeadline` 的 cap 默认 72 字 | 沿用现有 `NESTED_CHARS=120` 体系但取 72（单行标题太长会换行破版）。可改 | — |

---

## 10. 风险与不做的事

- **E2E 覆盖 4 条交互**（点击展开、composer 量宽、面板入口、权限切换），无自动化断言层。这是本项目既有现状（M2–A3 的交互验证都是 tmux），我不打算为此新建渲染测试基建。
- tmux 发鼠标事件已在本次实测可用（T10 的前提成立）。
- 不碰后端、不碰协议、不碰 `Part`/`SessionState` 类型、不动 `dialog-status` 其余 6 行、不动 StatusBar/Sidebar 内部。
- 全程一个改动一个验证点；任何一步的 E2E 不过就停，不叠加下一个修复。