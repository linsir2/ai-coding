# A7 设计：会话落在 workspace、stderr 可辨、子任务通知带 id、拒绝说人话

> 状态：**待拍板**（未写实现代码）
> 前置：A4 `f457bf5`、A5 `cf0283d`、A6 `1ef8517`
> 范围：上一轮/本轮调研发现、已确认仍然存在的 4 个缺口

## 0. 四项一览

| # | 缺口 | 证据 | 性质 | 后端/前端 |
|---|---|---|---|---|
| ① | 会话文件落在**进程 CWD**（`./sessions/`），不在 workspace | `session_service.py:59` 默认相对路径；`world.py:84` 无参构造；实测项目根已积 115 个 json | workspace 隔离 | 后端 |
| ② | stderr 与 stdout 完全无法区分 | wire 每块带 `stream`，前端 `tool.chunk` 只读 `callId`/`text`；实测 `out-line out2 err-line err2` 混排 | 丢弃了自带能力 | 前端 |
| ③ | 子任务完成通知丢 `runId` 与 `truncated` | `bridge.py:205,209` 发了，前端 notice 没用 | 通知不完整 | 前端 |
| ④ | 拒绝类工具的 output 是英文固定串 | `dispatch.py:558,592,601` + `_denied:495`；实测 ledger evidence 显示 `error: not approved by user` | 文案 | 后端 |

## 1. ① 会话落在 workspace

### 现状
```
world.py:84        sessions = SessionService()                    ← 无参
session_service.py:59   def __init__(self, root = Path("sessions"))
cli.py:172         default_root = Path(workspace)/".ai-coding"/"traces"   ← 邻居的约定
```
`build_world(path, workspace, skills)` 手上有 `workspace`，紧邻的 `_make_recorder(app_cfg, workspace, None)` 已经按 `<workspace>/.ai-coding/<kind>` 解析。**只有 sessions 是例外**，而它恰恰是用户数据。

### 设计
`world.py:84` 改为显式传根目录，与 traces 同构：
```
sessions = SessionService(Path(workspace).expanduser().resolve() / ".ai-coding" / "sessions")
```
**不改 `SessionService.__init__` 的默认值**——测试里 20+ 处 `SessionService(tmp_path / "sessions")` 显式传参，不受默认值影响；默认值只服务「没传就落到 CWD」这个危险默认，而生产唯一调用点会被改掉。改默认值反而会让所有测试的意图（显式根）变得多余。

### 效果
- 会话与 trace 同处 `<workspace>/.ai-coding/`，删 workspace 即删全部本地状态
- 项目根不再被应用数据污染
- `.gitignore` 的 `sessions/` 条目从此对本项目无用（其它项目的遗留，不删——越界）

### 风险
**已存在的旧会话会「消失」**（还在旧 CWD 下）。`session.list` 只扫新根。这是迁移问题，见 §6 待拍板 ①。

## 2. ② stderr 可辨

### 现状
`ToolChunk.stream ∈ {stdout, stderr, progress}`（`events.py:98`），从 `subprocess_runner.py:173` 的读取线程一路到 wire。前端：
```
state/session.ts:241  case "tool.chunk": 只取 callId 与 text
toolcard.tsx:47,84    tail.split("\n")  —— 不知道每行来自哪个流
```

### 设计：`tail: string` → `lines: TailLine[]`
```
type TailLine = { text: string; stream: "stdout" | "stderr" | "progress" }
tool part: { …, lines: TailLine[], linesDropped: boolean }
```
- `tool.chunk` 收到一块文本：按 `\n` 切开，**未闭合的残段与下一个块拼接**（不能每块都切，否则跨块的行会被腰斩），每段带上 `stream`
- 4000 字上限改为对 `lines` 的累计字符数；溢出丢最老的行并置 `linesDropped`
- 卡片：stderr 行用 `feedbackError` 色，stdout 用 `textMuted`，`progress` 用 `textMuted`（不特殊）

**为什么换形状而不是加字段**：`tail: string` 已经无法表达「每行属于哪个流」。加一个 `stderrTail: string` 会丢掉交错顺序——而顺序正是排错时要的（「先编译错误后 3 行警告」）。

### 影响面（诚实清单）
| 位置 | 改动 |
|---|---|
| `state/session.ts` | Part 类型、`tool.started` 初始化、`tool.chunk` 重写（含跨块拼接与裁剪）、`replay.ts` 初始化 |
| `component/toolcard.tsx` | 运行中 3 行、展开全文，两处按 `line.stream` 染色 |
| 测试 | `reducer.test.ts` 2 处 `tail: ""` → `lines: []`；A5 的 tailDropped 用例 1 条改写；`toolcard-render.test.tsx` 3 处 |

### 明确不做
- 不区分 `progress` 与 stdout 的**内容**——只染色，不解析
- 不给 stderr 行加图标（OpenCode 也没加，它干脆不区分）

## 3. ③ 子任务通知带 id 与截断标记

### 现状
`bridge.py:202-212` 发了 `runId` / `truncated`，前端 `state/session.ts:137-142` 的 notice 只用 `name/status/originSession/summary/ledgerPath`。

### 设计
沿用 A5 已立的诚实性模式（`truncationNote`）：
```
✓ 子任务完成 [name]（completed）：summary · 任务 1a2b3c4d5e6f
                            ^ 截断时追加「…（已截断）」
```
`runId` 取前 12 位——与 `/tasks` 列表打印的位数一致（`commands.py:373`），用户可以照抄去 `/tasks stop`。

## 4. ④ 拒绝类工具说人话

### 现状
```
dispatch.py:495  output=f"error: {prefix}denied by permission gate{detail}"
dispatch.py:558  output="error: not approved by user"     error="not_approved"
dispatch.py:592  output="error: stopped by user"           error="stopped"
dispatch.py:601  output="error: skipped (turn stopped by user)"  error="skipped"
```
`error` 是**码**（前端 A5 已映射成人话且无人改它），但 `output` 是给模型读、也会进 ledger evidence 的人话——它是英文。实测转录里出现 `· error: not approved by user`。

### 设计
只改这 4 处 `output` 文案为中文，与界面语言一致：
```
"error: 被权限门拒绝…"
"error: 用户未批准这次调用"
"error: 用户已停止"
"error: 已跳过（本回合被用户停止）"
```
**为什么可以改**：测试钉的是 `result.error == "not_approved"` 这个**码**（`test_tool_dispatch.py:165,178` 等），**没有任何测试钉这些英文 output**（grep 确认）。且中文 output 对模型同样可读——本项目的 BASE_INSTRUCTIONS 与工具描述本来就是中文。

**为什么不在前端翻译**：前端拿不到（`ledger.updated` 的 evidence 是成品字符串），做子串匹配就是我批评 OpenCode 的那套反模式。

## 5. 执行顺序（按风险从大到小，每步一次验证）

| 步 | 内容 | 验证 |
|---|---|---|
| 1 | ① 会话根（后端） | pytest + 真机：会话文件出现在 `<ws>/.ai-coding/sessions/` |
| 2 | ③ notice 带 id（前端，纯文案） | reducer 单测 |
| 3 | ④ output 中文（后端，纯文案） | pytest 回归 |
| 4 | ② tail 形状改造（前端，最大） | reducer + 渲染单测 → 真机跑一条会写 stderr 的命令 |

把 ② 放最后：它是唯一需要改数据形状的，前三步都是低风险的独立修复，先落地。

## 6. TDD 用例（RIPR）

| # | 用例 | R | I | P | R | 层 |
|---|---|---|---|---|---|---|
| T1 | `build_world` 把会话根解析到 `<ws>/.ai-coding/sessions` | `build_world` 可调 | 传 tmp workspace | `world.sessions.root` 等于该路径 | 失败时打印实际 root | pytest |
| T2 | 会话真写到新根 | 同上 | 建一个会话 | `<ws>/.ai-coding/sessions/*.json` 存在 | — | pytest |
| T3 | `background.done` 的 notice 含 12 位 runId | `reduce` 纯导出 | 喂 wire | 通知串含 `runId[:12]` | — | bun |
| T4 | `truncated=true` 时通知说明被截断 | 同上 | 喂 wire | 含「截断」 | — | bun |
| T5 | 拒绝类 output 是中文、**码不变** | `Dispatcher` 可造 | 触发未批准 | `output` 含中文；`error == "not_approved"` | 同时断言两边，防止只改一半 | pytest |
| T6 | `tool.chunk` 跨块拼接不腰斩行 | `reduce` 纯导出 | `"err"` + `"or\n"` | 两块合成一行 `[stderr]` | 打印切分结果 | bun |
| T7 | stderr 行染红、stdout 不染 | `captureSpans()` | 渲染 ToolCard | stderr 那个 span 的 `fg` ≠ stdout 的 | 直接比色值 | bun |
| T8 | 溢出丢最老行并置 `linesDropped` | `reduce` | 喂 >4000 | 保留末 4000；标志位 true | — | bun |
| T9 | 真机：跑一条写 stderr 的命令，两色可辨 | — | tmux | 抓 span 不可行，改为目视 + grep 无异常 | — | E2E |

**T5 的双向断言是重点**：码和文案是两套东西，只改一个就是半成品。

## 7. 数据流

```
① world.sessions.root: ./sessions  →  <ws>/.ai-coding/sessions
② tool.chunk{text,stream} → reducer 按 \n 切、跨块续行 → lines[{text,stream}]
                            → 卡片按 stream 选色
③ background.done{runId,truncated} → notice 追加 id 与截断说明
④ dispatch output: "error: not approved by user" → "error: 用户未批准这次调用"
                            （error 码不变，前端 A5 映射照旧）
```

## 8. 架构图

```
<workspace>/.ai-coding/
├── sessions/<id>.json     ← ① 从 ./sessions 搬来，与 traces 并列
├── traces/<id>/…          ← 既有
└── exports/…              ← A3 既有

tool.chunk {text, stream}
        │
        ▼  reducer：按 \n 切 + 跨块续行 + 4000 字裁剪
   lines: [{text, stream}]  linesDropped
        │
        ▼  toolcard
   ┌ stderr → feedbackError      stdout → textMuted
   └ progress → textMuted
```

## 9. 明确不做

| 不做 | 理由 |
|---|---|
| 迁移旧 CWD 会话 | 属破坏性操作，见 §10 ① |
| 删 `.gitignore` 的 `sessions/` | 其它项目的遗留，不越界 |
| 改 `SessionService` 默认值 | 测试全显式传参，改它无收益；生产唯一调用点已显式传 |
| stderr 加图标/加前缀 | OpenCode 也没加；染色足够 |
| 给 `progress` 单独语义 | 没有第二处消费它 |

## 10. 待你拍板

| # | 事项 | 我的建议 |
|---|---|---|
| ① | **旧会话迁移**：CWD 下已有的 115 个 json 怎么办 | **不自动迁移**。自动搬=在用户 CWD 里做文件操作，越界且易错。改为**在 A3 文档与提交信息里记一句迁移路径**，由你决定何时搬 |
| ② | ② 的数据形状改造（`tail: string` → `lines[]`）是本轮最大改动 | 建议**做**：不加字段就无法表达每行归属；`stderrTail` 双缓冲会丢交错顺序 |
| ③ | ④ 改后端 output 文案（影响模型读到的文本） | 建议**做**：本项目 instruction 与工具描述本就中文，模型读中文无碍；且测试钉的是码不是文案 |
| ④ | 四项一次提交还是一个里程碑一个提交 | 建议**一次提交**，它们同属「把已有数据/人话用到屏幕上」这一件事 |