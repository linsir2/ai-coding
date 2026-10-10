# A5 设计：工具卡的失败文案、展开、耗时、截断诚实性

> 状态：**待拍板**（未写实现代码）
> 前置：`opencode-gui-a4-design.md`（已提交 `f457bf5`）、`data-contracts.md`（错误码契约）
> 触发：实跑中用户看到 `✗ 已运行命令 · git branch --show-current` 下面直接写 `nonzero_exit`

## 0. 范围与原则

| 项 | 内容 | 性质 |
|---|---|---|
| 1 | 失败/被拒的机器码 → 中文文案 + 被拒删除线 | 缺陷修复 |
| 2 | 工具卡点击展开（完整输出 / 完整错误） | 补计划 §2.5 明写项 |
| 3 | 工具耗时 `· 1.2s` | 数据已在手，只差展示 |
| 4 | ~~`/clear`、`/resume` 入口~~ | **建议砍掉**，见 §3 |
| 5 | tail 静默截断 → 显式标记 | 诚实性 |

后端改动：**0**。新增依赖：**0**。后端已有数据：`error` 机器码、`durationMs`、`tail`（4000 字）。

## 1. 现状核准（事实，带行号）

| 位置 | 事实 |
|---|---|
| `component/toolcard.tsx:41-51` | 失败时直接渲染 `{props.part.error}`——机器码原样上屏 |
| `state/session.ts:26-32` | tool part：`status: "running"\|"ok"\|"failed"`，另有 `tail`/`preview`/`error`，**无 denied 态、无 duration、无截断标记** |
| `state/session.ts:242` | `tool.chunk` 用 `.slice(-TOOL_TAIL_CHARS)` 保留末 4000 字，**头部被丢时无任何标记** |
| `state/session.ts:251` | `preview = shorten(outputPreview, 120)`——有意为之的旧 Textual 移植 |
| `server/bridge.py:115-125` | `tool.finished` 已带 `durationMs`，前端**一次都没读**（grep 无命中） |
| `data-contracts.md:159,167,347` | **成文的错误码契约**：`denied/not_approved/stopped/skipped/unknown_tool/tool_error/mcp_error/subagent_error` + `tool_timeout/timeout/nonzero_exit/spawn_failed` + `guard_denied` |
| `core/dispatch.py:526,578` | 代码里还有 `hook_blocked`、`approval_timeout`，**文档表里没列**（文档缺口，非本文档要补的） |
| `test/reducer.test.ts:206` | 明确钉住 preview=120，注释写着 "like the old fold body"——**是移植契约，不是 bug，不许改** |
| OpenCode `index.tsx:2623` | 判「被拒」用**错误文本子串匹配**（`"rejected permission"`…）——我们有机器码，这是明确胜出点 |
| OpenCode `index.tsx:2634` | `clickable = Boolean(onClick \|\| failed())`——失败行可点开 |
| OpenCode `message-parts.tsx:238` | 被拒时 `TextAttributes.STRIKETHROUGH`（本项目 `@opentui/core` 同样有这个属性，已验证） |
| `commands.py:95,282` | `/clear` ≡ 新会话；`/resume` 无参只是在 console 列会话 |

## 2. 根因

**1（机器码上屏）**：后端把"给人看的句子"和"给程序看的码"分得很清——`ToolResult.output` 是人话，`ToolResult.error` 是码（`bash_tool.py:215` 甚至把 exit code 放进 `output` 而不是 `error`）。前端把 `error` 当句子渲染，是**渲染层用错了字段语义**。后端没错，前端也没"漏实现"，是契约被误解。

**2（点不开）**：ToolCard 没有 `expanded` 状态、没有鼠标事件。对位刚在 A4 验证可用的 `Reasoning` 折叠（per-part signal + `onMouseUp` 挂 `<text>`）。

**3（耗时）**：wire 给了 `durationMs`，reducer 没存、组件没显示。

**5（静默截断）**：`.slice(-4000)` 是有意保留尾部，但"丢了多少"这个事实被丢弃了，渲染层无法知情。

## 3. 砍掉第 4 项（`/clear` / `/resume` 入口）

| 命令 | 后端行为 | 前端现状 | 结论 |
|---|---|---|---|
| `/clear` | `REPLAction.NEW_SESSION`（`commands.py:95`） | 面板已有 `New session` → `session.create` | **完全等价，加条目=同一个功能两个入口** |
| `/resume`（无参） | 在 console 打印会话列表（`commands.py:286`） | 会话面板（`dialog-sessions.tsx`）可视化列表、可开可删 | **面板严格更好，加条目=更差的重复** |
| `/resume <id>` | 按 id 恢复 | 面板里点一下 | 同上 |

三条命令本身仍可手打（后端在、wire 在），只是不再各占一个面板条目。**砍。**

## 4. 新契约（每个都说得出作用）

### 新增 3 个纯函数，全放 `util/toolmeta.ts`

该模块自己的文档写着「Wire events carry raw params; this module is the only place that turns them into human text」——新逻辑属于这里，不新建文件。

```ts
/** 失败码 → 人话 + 语气。语气决定颜色与是否删除线。 */
export type FailureTone = "denied" | "failed"
export function toolFailure(error: string): { label: string; tone: FailureTone }

/** 毫秒 → `820ms` / `1.2s` / `2m 3s`（对位 OpenCode 的 Locale.duration） */
export function formatDuration(ms: number): string

/** tail 尾部保留后，头部是否被丢弃过（诚实性标记的渲染侧读法）。 */
export function truncationNote(dropped: boolean): string
```

映射表**照 `data-contracts.md` 抄**，不自行发挥：

| 码 | 文案 | tone |
|---|---|---|
| `denied` | 被拒绝 | denied |
| `not_approved` | 未获批准 | denied |
| `guard_denied` | 门卫拦截 | denied |
| `hook_blocked` | 钩子拦截 | denied |
| `approval_timeout` | 审批超时 | denied |
| `stopped` | 已停止 | denied |
| `skipped` | 已跳过 | denied |
| `nonzero_exit` | 命令返回非零 | failed |
| `timeout` | 命令超时 | failed |
| `spawn_failed` | 命令无法启动 | failed |
| `tool_timeout` | 工具调用超时 | failed |
| `tool_error` | 工具异常 | failed |
| `unknown_tool` | 未知工具 | failed |
| `mcp_error` | MCP 调用失败 | failed |
| `subagent_error` | 子代理失败 | failed |
| 其它/空 | 未知原因（码原样附后） | failed |

未知码的处理必须是 `未知原因（<code>）` 而不是静默吞掉——后端加了新码时用户看得见。

### 类型改动（1 处，最小）

```ts
| { kind: "tool"; ...; error: string; durationMs: number; tailDropped: boolean }
```

- `durationMs`：wire 已有，不存就是丢数据
- `tailDropped`：`tool.chunk` 里判断 `tail.length + text.length > TOOL_TAIL_CHARS` 即可，不需额外计算

**不加** `expanded` 到 Part：展开是**视图态**，不是数据（与 A4 的 reasoning 折叠同理）。

### 组件改动（1 个文件）

`toolcard.tsx`：
- 组件内 `const [expanded, setExpanded] = createSignal(false)`
- 标题行：`onMouseUp` 切换；图标 `+`/`−`（对位 OpenCode）；被拒时标题与图标加 `STRIKETHROUGH`
- 失败文案：`toolFailure(error).label` 取代裸码；`tone === "denied"` 用 `feedbackWarning`+删除线，`failed` 用 `feedbackError`
- 耗时：`· ${formatDuration(durationMs)}`（`durationMs > 0` 时）
- 展开体：`tail` 全文（左边框 + muted，对位 OpenCode 的 left-border code block）；`tailDropped` 时首行加 `truncationNote(true)`

## 5. 逐文件实施

1. `util/toolmeta.ts`：加 3 个纯函数 + 映射表（**先写测试**）
2. `state/session.ts`：tool part 加 `durationMs`/`tailDropped`；`tool.started` 初始化、`tool.chunk` 维护截断标记、`tool.finished` 落 duration；两处 `tool.finished` 分支（命中/补建）都要填
3. `test/reducer.test.ts`：补 3 条（duration 落库、tailDropped 置位、初始为 0/false）
4. `component/toolcard.tsx`：按 §4 组件改动重写
5. `test/toolcard-render.test.tsx`（新）：折叠文案、点击展开、再点收起、被拒删除线
6. E2E：真跑一次失败的工具，屏幕上不得出现 `nonzero_exit` 字样

## 6. 数据流影响

```
改前  tool.finished{error:"nonzero_exit", durationMs:5}
      ToolCard → "✗ 已运行命令 · git branch" + "nonzero_exit"（+ 耗时丢失）

改后  reducer → {status:"failed", error:"nonzero_exit", durationMs:5}
      ToolCard → "✗ 已运行命令 · git branch · 5ms" + "命令返回非零"
                点击 → 展开 tail 全文（被截断时首行标注）
      被拒时  → "⊘ 编辑文件 · a.py"（删除线）+ "被拒绝"
```

**唯一状态形状变化**：tool part 多两个字段。事件集合、IPC、后端零变化。

## 7. TDD 用例（RIPR）

| # | 用例 | R 可达 | I 交互 | P 预测 | R 显明 | 层 |
|---|---|---|---|---|---|---|
| T1 | 15 个码各自映射到文案与 tone；未知码 → `未知原因（weird_code）` | 纯函数直调 | 传字符串 | 逐条字面量 | 断言失败直接显示码与文案对照 | bun |
| T2 | `formatDuration`：0/820/1500/125000 | 纯函数 | 传数字 | `0ms`/`820ms`/`1.2s`/`2m 5s` | — | bun |
| T3 | `tool.finished` 把 durationMs 落进 part | `reduce` 纯导出 | 喂 wire | part.durationMs===5 | toMatchObject 显示 | bun |
| T4 | tail 超 4000 时 `tailDropped=true`，未超为 false | 同上 | 喂大 chunk | 布尔 | — | bun |
| T5 | 折叠行含 `· 5ms` 与中文失败文案，**不含** `nonzero_exit` | `testRender` | 渲染 ToolCard | `captureCharFrame()` | 帧内直接可比 | bun |
| T6 | 点击展开显示 tail 全文，再点收起 | `testRender`+`mockMouse` | click | 帧含 tail 片段 | — | bun |
| T7 | denied 时标题带删除线属性 | `captureSpans()` | 渲染 | span 带 STRIKETHROUGH | 属性名可见 | bun |
| T8 | 真跑一次失败工具，屏幕无机器码 | — | tmux | grep 无 `nonzero_exit` | — | E2E |

> T7 用 `captureSpans()`（`@opentui/core/testing` 提供）断言属性，不靠肉眼。harness 的既有限制（A4 §6 已记）：markdown 渲染为空白，故不断言正文排版。

**回归基线**：bun 140 → 预期 140 + 8 ≈ 148；pytest 2036/2 预存失败不变。

## 8. 架构图

```
util/toolmeta.ts（唯一"wire → 人话"的地方）
   toolFailure(code)  ──► {label, tone}     ToolCard 据此选色/删除线
   formatDuration(ms) ──► "1.2s"
   truncationNote()   ──► "…（前面还有内容被截断）"

state/session.ts  tool part
   { status, error, preview, tail, durationMs, tailDropped, … }
                              │
                              ▼
component/toolcard.tsx
   ┌ 标题行（onMouseUp 切换）  ✓/✗/⊘  已运行命令 · git branch · 5ms
   │  折叠：failure.label（denied → 黄色 + 删除线）
   └ 展开：左边框 + tail 全文（tailDropped → 首行截断说明）
```

## 9. 明确不做

| 不做 | 理由 |
|---|---|
| 改 `preview` 的 120 截断 | `reducer.test.ts:206` 明确钉住是旧 Textual 移植契约 |
| 把 `expanded` 存进 Part | 视图态不该进数据 |
| 新建 `util/tooloutcome.ts` 等文件 | 同类逻辑已集中在 toolmeta，再开文件就是重复 |
| 补 `data-contracts.md` 缺的 `hook_blocked`/`approval_timeout` | 是后端文档缺口，另行处理；本轮映射表覆盖它们已足够 |
| 在 `domain/ledger.py` 加码→中文表 | 会与前端表重复（跨语言两份映射，必然漂移）。见 §3b |

## 10. 待你拍板

| # | 事项 | 我的建议 |
|---|---|---|
| ① | 砍掉第 4 项（`/clear`/`/resume` 面板条目） | **砍**（§3 已给等价性证明） |
| ② | `ledger.py:98` 的机器码外泄 | **本轮一并做**，做法见 §3b（拍板后已定：删码留次数，不建表） |
| ③ | 被拒文案用 `feedbackWarning`（黄）而非红色 | 黄=被拦下、红=真出错，语义更准 |
| ④ | 展开体显示 `tail`（末 4000 字流）而非完整输出 | 后端 `outputPreview` 只有 500 字且已被 reducer 有损压到 120；tail 是手里唯一的 fuller 素材，**不动那条有损契约** |

## 11. 实施后自查

| 现象 | 结果 |
|---|---|
| 命令跑失败 | `✗ 已运行命令 · git branch · 5ms` + `命令返回非零`，**无 `nonzero_exit`** |
| 用户点了拒绝 | `⊘ 编辑文件 · a.py`（黄色删除线）+ `被拒绝` |
| 输出很长 | 折叠 3 行尾部，点开展开全文，头部被截时首行说明 |
| 快速命令 | 标题行显示 `· 820ms`，与 OpenCode 一致 |
| 内存 | Part 多两个标量字段，无增长风险 |

## 3b. 第 ② 项：ledger 句子的最终设计（拍板后追加）

**事实**：`ledger.updated` 只走 wire 到前端渲染（`bridge.py:148`、`agent_loop.py:967`），**没有任何路径进模型上下文**（`digest.py`/`loop.py` 无 ledger 引用）→ 它是**纯人话**，翻译/改写都是对的。

**为什么不在后端建码表**：后端建一张码→中文表，前端再建一张，就是跨语言的两份映射，必然漂移——正是本设计 §2 要消灭的东西。

**采用的做法（最小、零新表）**：`ai_coding/domain/ledger.py:98`

```
改前  f"{tool} 反复失败（{error}×{count}）：直接原因是什么？"
改后  f"{tool} 反复失败（{count} 次）：直接原因是什么？"
```

- **删的是展示，不是事实**：机器码仍留在 item id（`U-err-{tool}-{error}`，内部用、不上屏），系统零信息丢失
- **不丢用户信息**：紧跟其后的 evidence 行本来就是真实失败输出（`_tail(excerpt)`），码在那里是冗余的
- **零新表、零 wire 改动、零字符串解析**
- 无任何测试钉住这句（grep 确认），补 1 条新用例即可

**用例 T9（后端，pytest）**：`build_ledger` 在同一工具同一码失败 2 次 → `question` 含「2 次」且**不含** `nonzero_exit`。
- R：`build_ledger` 是纯函数可直接调
- I：构造 `failures` 列表
- P：断言子串
- R 显明：断言失败会同时显示实际句子与期望片段

**实施顺序**（小任务先清场，主战场在后端之后）：
1. 后端 ledger（T9）→ 2. `toolmeta` 三函数（T1/T2）→ 3. reducer 两字段（T3/T4）→ 4. `toolcard` 重写（T5-T7）→ 5. E2E（T8，同时覆盖 ledger 不再出码）
