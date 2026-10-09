# M3 方案：对话框系统（Dialog 宿主 + DialogSelect + Ctrl+P 面板 + 5 对话框）✅ 已实施

> 分支：`feat/opencode-gui-replica`（接 M2 之后）
> 状态：**已实施并核验**（2026-10-09）
> 基准：OpenCode v1.18.32 `packages/tui/src/ui/dialog.tsx` + `ui/dialog-select.tsx` + `component/command-palette.tsx`（源码逐行核对 + 实机 Ctrl+P 截图对比）
> 原则：最小化、第一性、只改根源；每个 UI 元素对应真实后端能力；库优先；TDD

---

## 验收结果（2026-10-09 实机）

- **Ctrl+P 面板**：标题+esc、搜索自动聚焦、模糊过滤、分组、`●`、右置快捷键、半透明背板（Home 透出）✅；与 OC 面板结构一致（OC 多的 "Suggested" 分组按砍项不做）
- **Models**：`●` 标记当前、回车经 `model.set` 切换、prompt 元数据随之更新 ✅
- **Sessions**：`session.list` 按天分组（Today/日期）、`●` 当前；选中 `session.load` → **历史重放**到转录、可继续对话 ✅
- **Skills**：`skills.list` 结构化（name/description/phase）✅
- **Status**：`config.summary` + hello 真实键值 ✅
- **Help**：keymap 注册表分组参考（含快捷键）✅
- **门控**：对话框打开时 ctrl+c 关闭对话框而非退出；base 命令禁用 ✅
- **refocus**：关闭对话框后焦点回 prompt（可立即输入）✅

### E2E 抓到并修复的真实问题
1. **thunk**：`dialog.open(<X/>)` 会立即执行组件函数（在 keymap 事件处理器、Provider owner 之外）→ 上下文取不到、抛错被吞。改为 `open(() => <X/>)` 惰性创建
2. **refocus 链**：palette→dialog 链中重复捕获到将被销毁的搜索框 → `EditBuffer is destroyed`。改为「previous 仅链首捕获；refocus 时若已有新对话框则跳过；focus 容错」
3. **hydrate 不生效**：`on(..., {defer:true})` 跳过挂载初值 → 加载会话时转录为空。去掉 defer
4. **provider 不更新**：切到别的 provider 的模型时元数据标签不更新 → `model.set` 返回新 provider，App 跟踪
5. **palette 空**：base 层门控后 `visibility:"reachable"` 看不到 base 命令 → 改用 `visibility:"registered"`
6. **行内换行/裁切**：title+description 竞争宽度 → 合并进一个 flexGrow 文本（截断）、`wrapMode:"none"`、footer 独立右置
7. **session 命令漏 title**：面板显示成命令名 → 补 `title`

### 命名修正
`session-group.ts` 实际导出 `dayCategory(timestamp, now)`（比 `groupByDay` 更小、不重复分组逻辑），分组仍由 `groupOptions` 完成。

---

## 0. 验收标准

1. `Ctrl+P` 打开命令面板：标题 + 搜索框自动聚焦 + 模糊过滤 + 分组 + `●` 当前项 + 右侧快捷键提示；回车执行
2. 面板可打开 5 个对话框：**Models / Sessions / Skills / Status / Help**；`esc` 关闭
3. Models 切换真实生效（`model.set`），prompt 元数据随之更新
4. Sessions 选择 → `session.load` → **历史重放**到转录区并可继续对话
5. 对话框打开时 base 命令全部失效（esc 不再中断回合、ctrl+t 不再切推理、ctrl+c 关闭对话框而非退出）
6. 关闭对话框后焦点回到 prompt 输入框
7. 与 OpenCode 同尺寸截图对比（面板/列表结构一致）；`pytest`/`ruff`/`mypy`/`bun test`/`tsc` 全绿；无回归

---

## 1. 范围

### 做什么
- **Dialog 宿主**：Provider + 单对话框 overlay（半透明背板、宽度预设、`open/close/isOpen`、esc/ctrl+c 关闭、关闭后 refocus、modal 门控）
- **DialogSelect**：可复用列表对话框（搜索/模糊/分组/导航/滚动到选中项/footer 提示/`●` 当前项）
- **命令面板**：`Ctrl+P`，数据来自 keymap 命令注册表（进程内真实）
- **5 个对话框**：Models（`model.set`）、Sessions（`session.list/load` + 重放）、Skills（`skills.list`）、Status（`config.summary`）、Help（keymap 注册表）
- **历史重放转换器**：持久化 `ChatMessage` → parts（M2 声明延后到 M3）
- **后端**：`skills.list` 改为结构化；`initialize` 增 `modelKey`

### 不做什么（延后，各有理由）
| 项 | 归属 | 理由 |
|----|------|------|
| slash `/` 自动补全 | M5（计划 M5 行） | M3 用 Ctrl+P 打开对话框 |
| 多级对话框栈 / drill-down（如设置子项） | 需要时再加 | M3 无嵌套对话框；单对话框 `open/close` 足够（OC 的 `stack[]` 对 M3 是 YAGNI） |
| 会话删除 / 重命名 / 新建 | 后续 | 需要 footer action 交互，M3 先只做“切换” |
| 主题切换对话框 | 后续 | 当前仅 dark/light 两套，条目薄 |
| memory/trace/budget/permissions/mcps/tasks 对话框 | 后续 | 按拍板：只做计划 6 个 |
| 背板点击关闭 / 鼠标交互 | M5 | M3 键盘优先 |
| Toast / 队列 / 审批面板 / 两段式 esc | M5 | 计划 M5 |

### 砍掉（说不清用途 / 冗余）
| 项 | 理由 |
|----|------|
| 自建 `useKeymapCommands/useKeymapShortcuts` 模块 | 库自带 `useKeymap()`/`useKeymapSelector`/`reactiveMatcherFromSignal`（已核 d.ts），自建=造轮子 |
| OC 的 mode 栈（`setData(MODE.key,…)`） | 库的 `enabled` 字段 + 在 `useBindings` 工厂里读 `isOpen()` 即可门控，效果相同 |
| `skills.list` 的 `catalog` 文本字段 | 前端无人消费、无测试；模型侧走 registry 不进 handler；直接换成结构化 |
| MCP 状态 handler/访问器 | Status 对话框用 `config.summary` + hello 已足够真实；MCP 面板后续再说 |
| 面板 “Suggested” 分组 | OC 的推荐逻辑依赖其 suggest 元数据；我们无此数据，不造 |

---

## 2. 现状与复用契约（全部读码核实）

### OpenCode 复刻目标
| 契约 | 位置 | 已核实内容 |
|------|------|-----------|
| Dialog 宿主 | `ui/dialog.tsx:20-250` | `DialogProvider` + store `stack[]`；overlay `position:absolute zIndex=3000` 全屏；背板 `RGBA.fromInts(0,0,0,150)`；非居中 `paddingTop=height/4`；宽度预设 `medium=60/large=88/xlarge=116`（`dialogWidth`）；`maxWidth=width-2`；内框 `backgroundColor=background.base` `paddingTop=1`；esc/ctrl+c 弹栈 + `refocus()`（`setTimeout(...,1)` 回 `renderer.currentFocusedRenderable`） |
| DialogSelect | `ui/dialog-select.tsx` | 头部（标题 BOLD + 右 `esc` muted）；搜索 input 自动聚焦；`fuzzysort.go(needle, options, {keys:["title","category","searchText"], scoreFn:(r)=>r[0].score*2+r[1].score+r[2].score, threshold})`；按 `category` 分组；`●` 当前项；高亮行 `action.primary.focused`；footer `title+label`；导航 `up/down/ctrl+p/ctrl+n/pgup/pgdn/home/end/return`；`scrollToSelection`；`maxHeight=min(rows, floor(h/2)-6)`；`contentWidth=width-12`；滚动条隐藏 |
| 命令面板 | `component/command-palette.tsx` | 数据 `Keymap.useCommands()`（=库 `getCommandEntries({visibility:"reachable"})`）；只收 `command.palette` 非空且非面板自身；行=title/description + 右 keybind；选中 `dialog.clear()` + `command.run()` |
| 快捷键格式化 | `context/keymap.tsx:277` | `formatKeySequence`/`formatCommandBindings`（库 `@opentui/keymap/extras`）；`getCommandBindings({visibility:"registered", commands})` |

### 我方库能力（`@opentui/keymap@0.5.17`，已核 d.ts）
- `useKeymap()` → keymap 实例；`useKeymapSelector(selector)` → 响应式派生视图；`reactiveMatcherFromSignal(signal)` → 适配 `ReactiveMatcher`
- `useBindings` 的 Layer 支持 `enabled`（`createDefaultOpenTuiKeymap` 默认注册该字段；`Enabled = boolean | (()=>boolean) | ReactiveMatcher`）
- `getCommandEntries({visibility:"reachable"})` → `{command, bindings}[]`；`Command` 带 `[key:string]:unknown`（`palette/title/group/desc` 可自由携带）；`registerMetadataFields` 已注册 `desc/group/title/category`
- `getCommandBindings({visibility:"registered", commands})` → `Map<id, ActiveBinding[]>`
- `fuzzysort@4.0.2`：`go(needle, objs, {keys, scoreFn, threshold})` → `KeysResults`，每项 `.score`/`.obj`（与 OC 用法一致）
- `@opentui/core`：`RGBA.fromInts`；`ScrollBoxRenderable.scrollChildIntoView(childId)`；`InputRenderable.focus()/value`；`renderer.currentFocusedRenderable`
- `@opentui/solid`：`<input>`/`<box>`/`<scrollbox>`/`<text>` 原语；**无** `useRenderer`（renderer 由 index.tsx 以 prop 下传）
- 主题 token：`backgroundRaised`（对话框底）、`backgroundRaisedHigh`（高亮/聚焦行）、`actionPrimaryFocused`、`hueAccent`（分组标题）、`textBase/textMuted`、`feedbackError` —— 一一映射

### 后端现状（复用）
| 契约 | 位置 | 内容 |
|------|------|------|
| models | `handlers.initialize` | 返回 `models:[{name,key}]`（`cfg.name or key`）；`model.set` 按 **key** 查 `loop.models` |
| sessions | `handlers.session_list/session_load/delete` | list 行 `{id,title,createdTime,lastAccessTime,preview,count}`；load `{id,title,messages}`，消息=`_message_to_dict` |
| 消息形状 | `domain/session.py:40` | `{role,content,timestamp,toolCallId?,toolName?,toolCalls?[{id,name,arguments}],reasoning?}` |
| 重放语义 | `ui/blocks.py` | `_strip_envelope`（去 `<tool_output>`/`<data>`）、`tool_outcome_from_message`（`ok="false"` 判失败）、`tool_card_identity`、`_short` 120 —— TS 同值移植 |
| config | `handlers.config_summary` | `{model(key), workspace, maxContextTokens, maxToolIterations, stopPolicy}` |
| skills | `skills/registry.py` | `names()`（排序）、`meta(name)`→`{name,version,status,description,phase,pattern_dna}`、`catalog_text()` |

---

## 3. 架构与数据流

```
index.tsx: renderer → <KeymapProvider keymap><App renderer/></KeymapProvider>
App
 └─ DialogProvider(renderer)              ← src/dialog/context.tsx
     ├─ <Show when={route==="session"}> Home | Session </Show>   （children，正常渲染）
     └─ <Show when={element()}>            ← overlay（absolute zIndex 3000）
          backdrop RGBA(0,0,0,150)
          └─ <DialogFrame width=dialogWidth(size) bg=backgroundRaised>
               └─ {element()}             （Palette|Models|Sessions|Skills|Status|Help）

useDialog(): { open(el, size?), close(), isOpen() }

门控（库 enabled 字段，在 useBindings 工厂里读信号）：
  App/Session 的 base 层:  enabled: () => !dialog.isOpen()
  DialogProvider 宿主层:    enabled: () =>  dialog.isOpen()   （esc/ctrl+c → close）
  DialogSelect 层:          随对话框组件挂载（无嵌套对话框，无需门控）

面板数据流（进程内，真实）：
  keymap.getCommandEntries({visibility:"reachable"})
    → 过滤 command.palette 且非自身 → 行{title,desc,group,footer=快捷键}
    → 回车 keymap.runCommand(name)

对话框数据流（RPC，真实）：
  Models   : initialize.models (+modelKey) → 选中 model.set{name:key} → App 更新 modelName/modelKey
  Sessions : session.list → 按天分组 → 选中 session.load → messagesToState() → App 设 sessionId+hydrate → Session 应用
  Skills   : skills.list(结构化) → 只读列表
  Status   : config.summary + hello → 只读键值
  Help     : keymap 命令+快捷键 → 只读参考
```

**单向数据流**（与 M2 同构）：对话框只读 RPC/注册表 → 用户动作 → 调 RPC 或回调 → App 信号更新 → 路由重渲染。

---

## 4. Dialog 宿主（`src/dialog/context.tsx`，新）

```ts
export type DialogSize = "medium" | "large" | "xlarge"
export function dialogWidth(size: DialogSize): number   // 60 | 88 | 116

interface DialogApi {
  open(element: JSX.Element, size?: DialogSize): void
  close(): void
  isOpen(): boolean
}
export function DialogProvider(props: { renderer: CliRenderer; children: JSX.Element })
export function useDialog(): DialogApi
```
- 状态：`element` signal + `size` signal；`open` 先记 `renderer.currentFocusedRenderable` 再 `setElement`；`close` 置 null 并 `setTimeout(1ms)` 回焦
- overlay：`<box position="absolute" left={0} top={0} width="100%" height="100%" zIndex={3000} alignItems="center" paddingTop={height/4}>`，背板 `backgroundColor={RGBA.fromInts(0,0,0,150)}`；内框 `width=dialogWidth(size) maxWidth={width-2} backgroundColor={theme.backgroundRaised} paddingTop={1}`
- 宿主按键：`useBindings(() => ({ enabled: isOpen(), commands:[{name:"dialog.close", desc:"Close dialog", run: close}], bindings:[{key:"escape",cmd:"dialog.close"},{key:"ctrl+c",cmd:"dialog.close"}] }))`
- `renderer` 经 prop（index.tsx→App→Provider）传入，用于 refocus

---

## 5. DialogSelect（`src/util/dialog-select.ts` 纯函数 + `src/component/dialog-select.tsx` 渲染）

**纯函数（可 bun 测）**
```ts
export interface SelectOption {
  title: string; value: string
  description?: string; category?: string; footer?: string
  current?: boolean; onSelect?: () => void
}
export function filterOptions(options: SelectOption[], query: string, threshold?: number): SelectOption[]
export function groupOptions(options: SelectOption[]): { category: string; options: SelectOption[] }[]
export function flattenGroups(groups: { category: string; options: SelectOption[] }[]): SelectOption[]
export function moveSelection(current: number, delta: number, count: number): number   // 环绕
```
- `filterOptions`：空查询原样返回；否则 `fuzzysort.go(q, options, {keys:["title","category","description"], scoreFn:(r)=>r[0].score*2+r[1].score+r[2].score, threshold})`（title 权重 ×2；默认阈值 0.5，面板 0.7）
- `groupOptions`：无 category → 单组；否则按出现顺序分组

**组件**
- props：`{ title, options, onSelect?, footer?, emptyText? }`
- `query` signal + `<input>`（onMount `focus()`，`onInput`→setQuery）
- `createMemo` filtered→groups→flat；`selected` signal（flat 索引，过滤变化时归零/夹取）
- `useBindings`：`dialog.select.prev/next/page_up/page_down/home/end/submit` → `up/down/ctrl+p/ctrl+n/pageup/pagedown/home/end/return`（`moveSelection` 环绕）
- 选中变化 → `scrollbox.scrollChildIntoView(rowId)`
- 渲染：头部（`title` BOLD + 右 `esc` muted）；搜索 input；scrollbox（每组 category 标题 `hueAccent` BOLD + 行：`●`当前 / title（高亮行加粗）/ 右侧 footer）；底栏（左 `footer` 或 `enter select · esc close`）
- 高亮行背景 `backgroundRaisedHigh`；`onSelect(option)` 缺省用 `option.onSelect`

---

## 6. 命令面板（`src/component/command-palette.tsx`，新）

```ts
const keymap = useKeymap()
const entries = useKeymapSelector((km) => km.getCommandEntries({ visibility: "reachable" }))
const shortcutMap = useKeymapSelector((km) => {
  const names = km.getCommandEntries({ visibility: "reachable" }).map((e) => e.command.name)
  const bindings = km.getCommandBindings({ visibility: "reachable", commands: names })
  return new Map(names.map((n) => [n, formatCommandBindings(bindings.get(n), {}) ?? ""]))
})
const options = createMemo(() => entries()
  .filter((e) => e.command.palette && e.command.name !== "command.palette.show")
  .map((e) => ({
    title: String(e.command.title ?? e.command.name),
    value: e.command.name,
    description: e.command.desc ? String(e.command.desc) : undefined,
    category: String(e.command.group ?? ""),
    footer: shortcutMap().get(e.command.name) ?? "",
  })))
// <DialogSelect title="Commands" options={options()} onSelect={(o) => { dialog.close(); keymap.runCommand(o.value) }} footer="enter run · esc close" />
```
- 单一 `useKeymapSelector` 产出快捷键 Map（**不能**在 `For` 里逐行调 hook）

---

## 7. 五个对话框

| 对话框 | 文件 | 打开时 | 行 | 动作 |
|--------|------|--------|----|------|
| Models | `dialog-models.tsx` | props `models`+`currentKey`（来自 App） | title=`name`，value=`key`，current=`key===currentKey`，category="Models" | `model.set{name:key}` → App 更新信号 → close；失败→footer 红字 |
| Sessions | `dialog-sessions.tsx` | `session.list` | title=`title`（空则 id 前 8），description=`preview`，category=按天（`session-group.ts`），current=`id===currentId` | `session.load` → `messagesToState` → `props.onSelect(id, state)` → close；失败→footer 红字 |
| Skills | `dialog-skills.tsx` | `skills.list` | title=`name`，description=`description`，category=`phase || "Skills"` | 只读（回车 close） |
| Status | `dialog-status.tsx`（自定义，小） | `config.summary` + hello | 键值行：Session(title/id)、Model(provider·model)、Workspace、Limits(maxContext/maxToolIterations/stopPolicy) | 只读 |
| Help | `dialog-help.tsx`（自定义，小） | keymap 注册表 | 按 group 分组的 `快捷键 — title` 参考 | 只读 |

- Status/Help 用同一个 DialogFrame（Provider 提供），自绘头部（标题+esc）+ 内容 + 底栏，**不复用 DialogSelect**（只读信息不是“选择”）
- 尺寸：Palette/Models/Status `medium`；Sessions/Skills/Help `large`

---

## 8. 历史重放转换器（`src/state/replay.ts`，新，纯函数）

```ts
export interface WireMessage {
  role: string; content: string; timestamp?: string
  toolCallId?: string; toolName?: string
  toolCalls?: { id: string; name: string; arguments: string }[]
  reasoning?: string
}
export function messagesToState(messages: WireMessage[]): SessionState
```
规则（移植 blocks.py）：
1. `role==="user"` → 新建 `{role:"user", parts:[text]}`
2. 其余 → 归入**当前 assistant 消息**（自上一个 user 起），保证“一轮一条 assistant”与实时一致：
   - assistant：`content`→text part（streaming=false）；`reasoning`→reasoning part；`toolCalls`→每个建 tool part（status `running`，verb/target 由 `toolmeta` 从 `JSON.parse(arguments)` 派生，解析失败 target=""）
   - tool：按 `toolCallId` 在当前 assistant 消息内找 tool part → `ok="false"` 判 `failed`，否则 `ok`；preview=`shorten(_stripEnvelope(content),120)`
3. 孤立 tool（无匹配 call）忽略；空 assistant（无文本无调用）跳过
- 移植 `_stripEnvelope`（去 `<tool_output ...>` 首行、`<data>…</data>` 段、`</tool_output>`）
- 无 `busy`（`busy:false`）

---

## 9. 会话切换接线（App/Session/dialog）

- App 新增信号：`modelName`（初值 hello.model）、`modelKey`（初值 hello.modelKey）、`hydrate: {key:string; state:SessionState} | null`
- App 命令：
  - `dialog.models.show` → `dialog.open(<ModelsDialog models={hello.models} currentKey={modelKey()} onSelect={switchModel}/>)`
  - `dialog.sessions.show` → `dialog.open(<SessionsDialog client={client} currentId={sessionId()} onSelect={onSessionLoaded}/>, "large")`
  - `dialog.skills.show` / `dialog.status.show` / `dialog.help.show` / `command.palette.show` → 对应对话框
  - `app.exit`（ctrl+c）
- `switchModel(key,name)`：`await client.request("model.set",{name:key})` → `setModelKey/setModelName` → close（失败→对话框 footer）
- `onSessionLoaded(id,state)`：`setSessionId(id)`；`setHydrate({key:id,state})`；`setRoute("session")`
- Session 新增：`createEffect(on(() => props.hydrate, (h) => { if (h && h.key === props.sessionId) setState(h.state) }, {defer:true}))`；prompt 元数据用 `props.model`（App 传 `modelName()`）
- Home/Session 的 base 层加 `enabled: () => !dialog.isOpen()`

---

## 10. 每文件改动

### Python
**EDIT `ai_coding/server/handlers.py`（+6 −4）**
- `initialize` 增 `"modelKey": str(getattr(world.config, "default_model", "") or "")`
- `skills_list` 改为结构化：`{"skills": [{"name": n, "description": meta.get("description"), "phase": meta.get("phase") or ""} for n in registry.names()]}`（registry 为 None → `{"skills": []}`）

**EDIT `tests/test_server_ipc.py`（+2）**
- `test_initialize_returns_hello` 增 `modelKey` 断言
- 新 `test_skills_list_is_structured`（fake registry 注入 `world.loop.skills`）

### TypeScript（新 10 文件，改 4 文件）
| 文件 | 内容 |
|------|------|
| `src/dialog/context.tsx` 新 | DialogProvider/useDialog/overlay/宿主按键 |
| `src/util/dialog-select.ts` 新 | 4 个纯函数 |
| `src/component/dialog-select.tsx` 新 | 列表对话框渲染 + 导航 + 滚动 |
| `src/component/command-palette.tsx` 新 | 面板 |
| `src/component/dialog-models.tsx` 新 | 模型切换 |
| `src/component/dialog-sessions.tsx` 新 | 会话切换（含分组/错误） |
| `src/component/dialog-skills.tsx` 新 | 技能只读 |
| `src/component/dialog-status.tsx` 新 | 状态只读 |
| `src/component/dialog-help.tsx` 新 | 帮助只读 |
| `src/state/replay.ts` 新 | messagesToState + _stripEnvelope |
| `src/util/session-group.ts` 新 | `groupByDay(rows, now)` |
| `src/app.tsx` 改 | DialogProvider 包裹；modelName/modelKey/hydrate 信号；命令+元数据+门控；路由接线 |
| `src/routes/session.tsx` 改 | hydrate effect；base 层门控；model 用 prop |
| `src/index.tsx` 改 | `<App renderer={renderer} .../>` |
| `frontend/test/dialog-select.test.ts` 新 | 过滤/分组/导航 |
| `frontend/test/replay.test.ts` 新 | 转换器 |
| `frontend/test/session-group.test.ts` 新 | 按天分组 |

**零新增依赖。**

---

## 11. TDD 用例清单

### Python（test_server_ipc.py）
1. `initialize.modelKey == "fake"`（default_model key）
2. `skills.list` 结构化：注入 fake registry（names→[a,b]，meta→description/phase）→ 断言 `{skills:[{name,description,phase}]}`；registry=None → `{skills:[]}`

### frontend/test/dialog-select.test.ts（~9）
3. `filterOptions`：空查询原样；"mod" 命中 title；title 权重 > description（构造同分场景）
4. `filterOptions` 阈值：无匹配 → 空
5. `groupOptions`：有 category 分组保序；无 category 单组
6. `flattenGroups`：拼接顺序 = 组顺序×组内顺序
7. `moveSelection`：下移、上移、首行上移环绕到末、末行下移环绕到首、count=0 安全

### frontend/test/replay.test.ts（~9）
8. user → user 块；assistant text → text part（streaming false）
9. 一轮多步：`[user, assistant(text+tools), tool, tool, assistant(text)]` → **一条** assistant 消息，parts=[text, tool, tool, text]
10. tool 成功：`tool` 消息 `ok="true"` → tool part `ok` + preview
11. tool 失败：首行含 `ok="false"` → `failed`
12. `_stripEnvelope`：去 `<tool_output>`/`<data>` 段；preview 取净文本
13. reasoning → reasoning part
14. toolCalls arguments 非法 JSON → target=""（不抛）
15. 孤立 tool 消息忽略；空 assistant 跳过
16. busy=false

### frontend/test/session-group.test.ts（~3）
17. 今天 → "Today"；昨天/更早 → `YYYY-MM-DD`；无 lastAccessTime → "Earlier"（或空组）

### E2E（tmux，人工核对 + 截图）
18. Ctrl+P：面板出现、输入过滤、回车打开 Models；切换后 prompt 元数据变化
19. Sessions：列表按天分组、`●` 当前；选中 → 转录重放（工具卡已结算、推理折叠）、可继续对话
20. 门控：对话框打开时 esc 不中断、ctrl+c 关闭对话框不退出；关闭后焦点回输入框
21. 与 OpenCode Ctrl+P 同尺寸截图对比

---

## 12. 方案自检（假设 → 验证 → 回退）

| # | 假设 | 验证 | 回退 |
|---|------|------|------|
| A1 | `useBindings` 工厂里读 `isOpen()` 能响应式门控 base 命令 | E2E 20 | 用库 `reactiveMatcherFromSignal(isOpen)` 作 `enabled`；再不行用 OC 的 mode 栈 |
| A2 | 宿主层按键（↑↓/enter）先于聚焦 input 拦截 | E2E 18 | input `onKeyDown` 里转交导航 |
| A3 | `useKeymapSelector` 在命令增删（Session 挂载）时刷新面板 | E2E 18（进会话后 Ctrl+P 应含 session 命令） | 面板打开时一次性 `getCommandEntries`（不响应式） |
| A4 | `fuzzysort.go(keys=[title,category,description])` 对缺字段安全、返回 `.obj` | 用例 3-4 | 仅用 title 单键 |
| A5 | `scrollChildIntoView(rowId)` 生效 | E2E 19（长列表） | 用 `scrollTo(row*rowH)` |
| A6 | 背板 RGBA 半透明正常渲染 | E2E 18 | 不透明 `backgroundRaisedHigh` |
| A7 | 重放渲染与实时一致 | E2E 19 对比同轮实时截图 | 调整转换器规则 |
| A8 | `model.set` 后 prompt 元数据即时更新 | E2E 18 | 重拉 initialize |

**最终模样走查**：Home 按 Ctrl+P → 60 宽面板浮在屏幕中上、半透明背板、标题 "Commands" + 搜索框；输入 `sess` → 只剩会话命令；回车 → 88 宽会话列表按 "Today/日期" 分组、当前会话带 `●`；回车 → 转录区出现历史（用户块、markdown、已结算工具卡、`+ Thought`），可继续提问；esc 逐层关闭；关面板后光标回到输入框。会话中 ctrl+t/esc 在面板打开时不触发。

**复杂度盘点**：Python 2 文件小改；TS 新 11 + 改 4；测试 3 个 TS 纯逻辑文件 + 2 个 Python 用例；无新依赖。

---

## 13. 被否方案与砍掉的东西

| 项 | 理由 |
|----|------|
| 用库内建 `<select>`/`<tab_select>` 渲染列表 | 无分组/模糊/●/footer，外观与 OC 不符 |
| 每个对话框各写简易列表 | 6 份重复、无模糊、面板无搜索 |
| 照搬 OC mode 栈 | 库 `enabled` 字段等效，更重 |
| 自建 keymap 查询/快捷键封装 | 库已提供 `useKeymap/useKeymapSelector/extras` |
| OC 多级对话框栈 | M3 无嵌套，`open/close` 足够 |
| 面板 “Suggested” 分组 | 无 suggest 元数据 |
| `skills.list` 保留 `catalog` | 无人消费 |
| MCP 状态 / 主题切换 / 会话增删 | 按拍板与最小化延后 |

## 14. 实施顺序

1. Python：`skills.list` 结构化 + `initialize.modelKey`（用例 1-2 红→绿）→ pytest/ruff/mypy
2. TS 纯逻辑：`util/dialog-select.ts`（用例 3-7）→ `state/replay.ts`（8-16）→ `util/session-group.ts`（17）→ bun test 绿
3. Dialog 宿主 + DialogSelect 组件 + 门控（接线 app/session/index）
4. 面板 + 5 对话框
5. tmux E2E（18-21）对照 OC；修实际问题；文档状态 + 提交

预计：1.5–2 个专注日。
