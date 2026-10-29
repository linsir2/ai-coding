# 批次 8-1 方案（R22·A-9 命令调色板）

> 只出方案，不改代码。依据：`docs/r22-batch8-queue.md` 第 1 项、`docs/r22-feature-matrix.md` §5 A-9，
> 以及本轮的**真机复现**（190×46 tmux，r22-s1，`config.record.yaml`，会话内与 Home 各一次）。

## 1. 复现结果（先量后改）

队列里的 A-9 写了两条：① 未过滤态只显示 8 条、`↑↓` 不滚动、无滚动指示；② 输入 `agents` 显示 `no matches`。
本轮在同一台机器、同一配置下逐条重跑，结论和队列不一致——所以先把"到底还剩什么"钉死。

### 1.1 滚动：逐条 ↓ 能滚，整屏跳（PageDown/PageUp）不能

- 未过滤态视口 = `floor(46/2)-6 = 17` 行，其中 5 个分组头 + 4 个分组间空行，**首屏正好露出 8 条命令**。
  这与队列的"只显示 8 条"一致，但它只是分组间距占位，不是高度算错。
- 连按 12 次 `↓`：列表**会**滚（首行从 `Model` 组变成 `Queued input`），滚动条（右缘 `▀`/`▄`）也在动。
- 按一次 `PageDown`：**列表不动**（帧仍是首屏 `Model` 组），选中项已经跳到第 11 条（视口外）。
- 按 `End`：跳到末屏，正常。

根因在 `@opentui/core` 的 `ScrollBox.scrollChildIntoView`：

```js
// node_modules/@opentui/core/index.bun.js:14458-14473
if (elementStartOutside && elementEndOutside) {
  return 0;               // ← 目标行完全在视口外时，方向无法判定，干脆不滚
}
```

逐条 `↓` 时目标行每次只越界一行（`elementStart` 还在视口内），所以算得出 1 行增量；
`PageDown` 一次跳 10 行，目标行整体落在视口外，直接 `return 0`。**这就是"↑↓ 不滚动"的真身**：
不是按键没接上，是整屏跳转时滚动补偿失效。

### 1.2 `agents`：不是搜索 bug，是命令不在当前路由注册

- 会话路由内输入 `agents`：匹配到 `Agents  List agents and switch the current o…`（正常）。
- Home 路由内输入 `agents`：`no matches`。原因是 `dialog.agents.show` 只在 `routes/session.tsx` 注册
  （`session.tsx:450`），Home 根本没有这一行；**命令作用域问题，不是大小写/分组名匹配问题**。
- 队列引用的 `64-palette-agents` 帧与 `61` 帧里 `Cycle agent` 的描述是 Home 的
  （"Choose the agent the next session starts with"），说明那几帧是在 Home 上取的；`fuzzysort`
  对 `agents`/`agent` 的评分实测正常（`Agents` 精确命中，score 1.0）。

结论：② 不修代码；在方案文档里把"该命令是会话级"写清楚，避免下轮再当 bug 报。

### 1.3 新发现（同一次复现里看到，属于 A-9 的可见缺陷）

1. **同名命令重复成两行**：`Review` 出现两次（完全相同），`Queued input` 出现两次
   （`View or delete queued…` 与 `View, edit, or delete q…`）。根因：app 层与会话层各注册了一份
   `session.review` / `dialog.inbox.show`（`routes/session.tsx:409-437,474-487` 的"双胞胎"注释），
   keymap 的 `visibility:"registered"` 视图**按层展开、不按名去重**（`getRegisteredCommandView`），
   调色板把每条 entry 都映射成一行。
2. **行高被量成 2 行**：真机里 `list.content.getChildren()` 的实测（`y/h`）显示，
   带 footer 且 title+description+footer 超出行宽的那些行 `height=2`（第二行是空行），
   没有 footer 的 `Connect a provider` / `Review` 是 `height=1`。视口 17 行 ÷ 2 行/条 ≈ 8 条，
   **这才是"只显示 8 条"的根因**，不是 `maxList` 算错。测试渲染器不复现（同样的行 `height=1`），
   是原生渲染器的 flex 测量行为。
3. **描述被硬切在词中**：`Change the active mod`、`Open a saved sessio`、`List background shel`。
   `<text wrapMode="none">` 没有开 `truncate`，于是按宽度截断且没有省略号（见 1.4 的撤销说明）。

## 2. 方案（三项，都是小改）

### FIX-A9-1 用"行偏移"自己算滚动位置（`util/dialog-select.ts` + `component/dialog-select.tsx`）

不再依赖 `scrollChildIntoView`（库的行为在整屏跳转时是错的），改为：

1. 新增纯函数 `rowLayout(startsHeader)`，**一次算出**每一行的内容行号 `tops` 与内容总高 `height`
   （首组头占 1 行；非首组头占"1 空行 + 1 文本行"；每条命令 1 行）。
   组件里的 `listHeight()` 改成 `min(layout.height, maxList())`，与 `tops` 同源，杜绝两套计数漂移。
   两组各 2 条的 `tops` 是 `[1,2,5,6]`（第二组头前有 1 空行），`height = 7`。
2. 新增纯函数 `scrollTopFor(currentTop, rowTop, viewport)`：
   - 行在视口上方（`rowTop < currentTop`）→ 顶到 `rowTop`；
   - 行在视口下方（`rowTop + 1 > currentTop + viewport`）→ 底对齐 `rowTop + 1 - viewport`；
   - 否则不动；结果夹到 `>= 0`。
3. 组件里的 `createEffect` 改成
   `list.scrollTop = scrollTopFor(list.scrollTop, tops()[selected()], Math.max(1, list.viewport.height), blockTop)`。
   视口高度取**实际 viewport**（而不是再算一遍 `listHeight()`），因为首帧布局前 `scrollSize` 可能还是 0，
   这时的赋值会被库夹成 0；取实际高度可以让后续每次选中变化都按真实窗口重算。
   `blockTop` = 该行所属分组的组头起始行（首条命令时为 `rowTop-1`，首组为 0），向上滚动对齐到组头，
   避免 Home/PageUp 把 `Model` 这类组头切掉半行。

这样 `↓`/`PageDown`/`Home`/`End` 走同一个判据，不再受库函数"整屏外返回 0"的影响。

> 评审确认过：`scrollTop` 的单位就是内容行（`scrollSize = content.height`），setter 会自行取整/夹取，
> `stickyScroll` 默认关闭，不会把位置抢回去。

4. 行 `<box>` 显式 `height={1}`。这是让 1.3-2 的"2 行高"回到 1 行——行本来就是单行设计
   （`wrapMode="none"`），原生渲染器只是在内容超宽时多给了个空行。改后实测 `content` 的
   每个子节点 `h=1`，`scrollHeight` 29 与 `rowLayout` 的 `height` 完全一致，视口 17 行能露出 12 条命令。
5. （评审补）行高钉死为 1 后，标题里自带的换行会被裁掉：`singleLineText()` 把 `\n` 折成空格，
   排队输入、记忆这类多行标题仍保留全部词（只是排成一行）。`maxHeight` 与 `height` 冗余，删掉。

### FIX-A9-2 同名命令去重（`dialog/commands.ts`）

新增纯函数 `dedupeRowsByName(rows)`：按 name 保留**最后注册**的一条，输出顺序沿用首次出现的位置。

- 为什么保留最后：层按注册顺序展开（app 先、路由后），后注册的就是当前路由更贴近的那份
  （会话内的 `Review` 会走当前会话，`Queued input` 的编辑会落回 composer；Home 上只有 app 那一份）。
- 在 `useCommandRows` 的 selector 出口调用一次，`CommandPalette` 与 `DialogHelp` 同时受益，
  不在各消费者里各写一遍。
- 风险：目前只有路由双胞胎重名；若以后真的需要两个同名命令，keymap 的按名解析本身也分不清，
  去重反而让"调色板里有两条一样的东西却只能跑一条"的假象消失。

### ~~FIX-A9-3 描述省略号~~（试做后撤销）

原计划给标题/描述拆两个 `<text>` 并开 `truncate`。实测 OpenTUI 的 `truncate` 是**中间省略**：

```text
abcdefgh...rstuvwxyz
Sign in required · No redirect URIs provided… → Sign in requ...on code grant
```

它会把既有文案的**短语中间**截掉——`test/dialog-mcp-render.test.tsx` 里"Sign in required"
直接变成 `Sign in requ...`，而那几条帧断言是有契约意义的（面板必须说清楚状态）。因此撤回：
描述仍在行尾硬切，和本轮之前一致；这是纯观感问题，不属于 A-9 报的两条，留给以后需要时再做
（要做也只能是"按显示宽度自己截 + 尾部 …"，不能直接用库的 `truncate`）。

### 不做（明确排除）

- 不放大 `maxList`（17 行是半屏上限，首屏 8 条是分组间距的必然结果；改大只是把问题推到下一屏）；
- 不在 Home 补一个会话级 `Agents` 的调色板双胞胎（切 agent 的 Home 语义是 `home.mode.cycle`，已存在）；
- 不改 `footer` 提示文案（`enter run · esc close` 已有；导航键在 Help 里有）。

## 3. 测试（先红后绿）

| 测试 | 断言 |
| --- | --- |
| `test/dialog-select.test.ts`：`rowLayout` | 两组各 2 条 → `tops=[1,2,5,6]`、`height=7`；空表 → `tops=[]`、`height=0`；无分组 → `[0,1,2]`；首条无分组、次条有分组 → `[0,3]` |
| 同上：`scrollTopFor` | 目标行整体在视口下方 → `rowTop+1-viewport`（PageDown 场景）；整体在上方 → `rowTop`；视口内 → 原值；负数夹到 0 |
| `test/commands.test.ts`（新建）：`dedupeRowsByName` | 重名保留后一条；首次出现顺序不变（重名不相邻也成立）；无重名原样返回 |
| 回归 | `dialog-mcp-render`（7 例）等现有 DialogSelect 渲染测试保持原样通过（FIX-A9-3 撤销后不应有文案变化） |
| 真机帧 | `PageDown` 后首行不再是 `Model`；`End` 到末屏；`Review`/`Queued input` 各只剩一行 |

## 4. 验收口径

1. 会话内：连按 `PageDown` 两次，帧的首行分别变化；`PageUp` 回到顶部；
2. 过滤 `review` / `queued`：各只有一条；
3. 前端 `bun test` 与 `bun run typecheck` 干净；不触碰后端。
