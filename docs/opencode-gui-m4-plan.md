# M4 方案：会话标签 + 侧栏 + 状态栏 ✅ 已实施

> 分支：`feat/opencode-gui-replica`（接 M3 之后）
> 状态：**已实施并核验**（2026-10-09）
> 基准：OpenCode v1.18.32 会话屏（源码 + 实机 140×42 截图）：`routes/session/sidebar.tsx`、`feature-plugins/sidebar/context.tsx`、底部状态栏、`component/session-tabs.tsx`
> 原则：最小化、第一性、只做相关；**每个元素对接真实后端数据，不无中生有**；库优先；TDD

---

## 验收结果（2026-10-09 实机 tmux 140×42）

- **顶部 tabs**：最近 5 个会话（当前高亮）+ `+ New`；`New session` 命令建新会话并切过去；`Next/Previous session` 切换（侧栏标题/转录/Context 同步变化）✅
- **右侧栏**（宽 42、raised bg）：会话标题 + `Context`（真实 `3,326 tokens` / `7% used`）+ footer `dir:branch`；窄屏（90 列）隐藏 ✅
- **底部状态栏**：`dir:branch` + `3,326 (7%)` + `ctrl+p commands` + `• ai-coding 0.1.0`（宽度自适应截断）✅
- **真实回合后**：tab 标签、侧栏标题、Context 全部刷新（`turn.finished` → 拉 `session.stats` + 刷新会话列表）✅
- 环境：`.venv` editable 安装（`python -m pip` 可用；`ai-coding` 脚本可跑）、前端 `bun install` 就绪 ✅

### 发现与说明
- 初始会话（`initialize` 建、未落盘）不在 `session.list` → tabs 里合成「New session」条目保证当前始终可见
- tabs 溢出：OC 是 1803 行滚动 rail；M4 取最近 5 + 标签截断 12 字符（不做移植）
- `Context tokens` 语义：会话文件只存每轮 prompt/completion/total 合计，无单次请求上下文大小；取**最近一轮的 prompt_tokens**（最接近占用），percent=tokens/maxContext（近似），代码注释与本文档写明

### 环境/回归备注
- 全量 pytest 在本机（load 5/8）下会出现**预存在的偶发卡顿**（如 `test_transcript_streaming.py`/`test_tool_bash.py` 在整套里卡住、单独跑秒过），与 M4 无关；早先完整一次摘要仅报 2 个**预存在** `test_loop_digest` 失败（HEAD 上同样失败）
- M4 定向测试：`test_server_ipc.py` 16 全绿；`bun test` 72 全绿；`ruff`/`mypy`/`tsc` 干净

---

## 0. 验收标准

1. 会话屏顶部横向 tabs：最近会话（当前高亮）+ `+ New`；选中切换（`session.load` + 重放）；`+` 新建（`session.create`）
2. 右侧栏（宽 42、raised bg）：会话标题 + `Context`（`{tokens} tokens` / `{percent}% used`）+ footer `dir:branch`；窄屏（<100 列）隐藏
3. 底部状态栏：`{dir:branch}` + `{tokens} ({percent}%)` + `ctrl+p commands` 提示 + `• ai-coding {version}`
4. 与 OC 会话屏结构一致（截图对比）；M2/M3 行为不回归（门控/对话框/中断/refocus）
5. `pytest`/`ruff`/`mypy`/`bun test`/`tsc` 全绿；无回归

---

## 1. 范围

### 做什么
- 后端：`session.stats` RPC（真实用量）
- 前端：`SessionTabs`、`Sidebar`、`StatusBar` 三组件 + 会话屏布局重构 + `+ New` 命令
- 纯逻辑：`util/stats.ts`（token 格式化 / 百分比）

### 不做什么（延后，各有理由）
| 项 | 理由 |
|----|------|
| 并发多会话（每 tab 一个活会话） | 后端大改（loop/service 单会话假设）；拍板为「切换式」 |
| 侧栏 MCP 段 | `config.local.yaml` `mcp.enabled: false`，无服务器 → 无法真跑通；有 MCP 时再加 |
| `$ {cost} spent` | 无定价数据，不造 |
| tabs 动画 / marquee / unread / overflow / 拖拽重排 / 垂直布局 | OC 的 `session-tabs.tsx` 1803 行；M4 只做横向切换条 |
| 会话删除/重命名 | 需要 footer action 交互，后续 |

### 砍掉
| 项 | 理由 |
|----|------|
| 移植 OC 的 session-tabs 组件 | 1803 行动画系统，与「切换式 tabs」需求无关 |
| `$ spent` / MCP 段 | 无真实数据源 |

---

## 2. 现状与复用契约（已核实）

### OpenCode 结构（复刻目标）
- 侧栏 `sidebar.tsx`：宽 **42**（`SESSION_SIDEBAR_WIDTH`）、`backgroundColor=raised.base`、`paddingTop/Bottom=1 paddingLeft/Right=2`、会话标题在顶（BOLD）、内容 scrollbox、footer
- Context 段（`feature-plugins/sidebar/context.tsx`）：`Context`（BOLD, text.base）+ `{tokens} tokens`（muted）+ `{percent}% used`（muted，有值时）+ `$ {cost} spent`（有 cost 时）
- MCP 段：`MCP`（BOLD）+ `• {name}  {status}`（点色 connected/failed/disabled/needs_auth）
- 侧栏 footer：`dir:branch`（muted，FilePath maxWidth 38）
- 底部状态栏：左 `{dir}` · 中 `{tokens} ({percent}%) · ${cost}` · `ctrl+p commands` · 右 `• OpenCode {version}`
- Tabs：app 级、横向（或垂直）、cycle/close 键位、动画/unread —— **不复刻**

### 我方真实数据
| 数据 | 来源 | 用途 |
|------|------|------|
| 用量 | `SessionData.turn_usages: list[TurnUsage]`（turn/prompt/completion/total） | Context tokens |
| 上下文上限 | `config.ai.max_context_tokens`（默认 48000） | percent 分母 |
| 会话列表 | `session.list` → `{id,title,lastAccessTime,preview,count}`（`preview=first_preview`） | tabs / 标题 |
| 会话切换 | `session.load` → messages → `messagesToState`（M3） | tabs 切换 |
| 新建 | `session.create` | `+ New` |
| 目录/分支/版本 | `hello.workspace` / `hello.vcs.branch` / `hello.version`；`util/location` 的 `abbreviateHome`/`formatLocation`（M1） | 侧栏 footer / 状态栏 |
| 主题 | `backgroundRaised`（侧栏底）、`textBase/textMuted`、`hueAccent` | 颜色 |

---

## 3. 架构与数据流

```
App（持 sessions 列表 / sessionId / hydrate / model / provider）
 └─ Session（route）  props: sessions, onSelectSession, onNewSession, onTurnFinished, location, version
     ├─ SessionTabs   顶部：For sessions（当前高亮）+ "+ New"
     ├─ box row flexGrow=1
     │   ├─ scrollbox  flexGrow=1   （转录，M2/M3 不变）
     │   └─ Sidebar    width=42 flexShrink=0（width>=100 才显示）
     │        title / Context(tokens,%used) / footer dir:branch
     ├─ Prompt
     └─ StatusBar     dir:branch · tokens(%) · ctrl+p commands · • ai-coding {version}

数据流（单向）：
  App.onMount → session.list → sessions 信号 → TabsBar/侧栏标题
  Tabs 选中 → App.onSelectSession(id) → loadSession(client,id)（session.load+重放）→ setSessionId+setHydrate
  Tabs "+ New" → App.onNewSession() → session.create → 切到新空会话 → 刷新 sessions
  回合结束 → Session 收 turn.finished → (a) 拉 session.stats → stats 信号（侧栏/状态栏）；(b) props.onTurnFinished() → App 刷新 sessions
```

**新增 RPC**：`session.stats` → `{tokens, maxContextTokens}`
- `tokens` = `state.session.turn_usages[-1].prompt_tokens`（最近一轮的 prompt tokens = 该轮发送的上下文，我方最接近「上下文占用」的真实值；无 usage 行为 0）
- `maxContextTokens` = `world.config.ai.max_context_tokens`
- 语义说明（诚实）：会话文件只持久化每轮 prompt/completion/total 的合计，没有「单次请求的上下文大小」；因此 tokens 取最近一轮的 prompt_tokens，percent = tokens/maxContext（近似占用）。文档与代码注释都写明

**共享加载**：`state/replay.ts` 增 `loadSession(client, id): Promise<SessionState>`（`session.load` + `messagesToState`），供 M3 会话对话框与 M4 tabs 复用（消除重复）

---

## 4. 后端改动

**EDIT `ai_coding/server/handlers.py`（+~12）**
```python
def session_stats(params):
    session = state.session
    tokens = 0
    if session is not None and getattr(session, "turn_usages", None):
        tokens = int(session.turn_usages[-1].prompt_tokens or 0)
    ai = getattr(world.config, "ai", None)
    return {"tokens": tokens, "maxContextTokens": int(getattr(ai, "max_context_tokens", 0) or 0)}
```
注册 `"session.stats": session_stats`。

**EDIT `tests/test_server_ipc.py`（+2 用例）**
- 无 usage → `{tokens:0, maxContextTokens:48000}`
- 注入 `world` 的 session 带一条 usage → tokens = 该行 prompt_tokens（用 `initialize` 后的 session 追加一行，或直接构造）

---

## 5. 前端改动

**NEW `src/util/stats.ts`（纯，~20 行）**
```ts
export function formatTokens(n: number): string        // 13930 -> "13,930"
export function percentUsed(tokens: number, max: number): number  // round，clamp 0..100，max<=0 -> 0
```

**NEW `src/component/session-tabs.tsx`（~45）**：`<box row flexShrink=0>`；`For` sessions → 每个 `<text>`（当前项 `hueAccent`/BOLD，否则 muted；标题 = `title || preview || id.slice(0,8)` 截断）+ 末尾 `+ New`（hueInteractive）。props `{sessions, currentId, onSelect, onNew}`

**NEW `src/component/sidebar.tsx`（~55）**：`<box width=42 flexShrink=0 bg=backgroundRaised padding>`；标题（BOLD）；`Context`（BOLD）+ `{formatTokens(tokens)} tokens`（muted）+ `{percent}% used`（muted，max>0 时）；footer `{location}`（muted）。props `{title, tokens, maxContextTokens, location}`

**NEW `src/component/status-bar.tsx`（~35）**：`<box row flexShrink=0 paddingLeft/Right=2>`；左 `{location}`（flexGrow，截断）+ `{formatTokens(tokens)} ({percent}%)` + `ctrl+p commands` + `• ai-coding {version}`（muted）。props `{location, tokens, maxContextTokens, version}`

**EDIT `src/state/replay.ts`（+~8）**：`loadSession(client, id)` 复用 `session.load` + `messagesToState`

**EDIT `src/component/dialog-sessions.tsx`（−~6）**：onSelect 改用 `loadSession`

**EDIT `src/routes/session.tsx`（布局重构 + stats）**
- 新 props：`sessions, onSelectSession, onNewSession, onTurnFinished, location, version`
- `stats` signal（`{tokens, maxContextTokens}`）；`refreshStats()` = `session.stats` RPC
- 事件处理：`turn.finished` → `refreshStats()` + `props.onTurnFinished()`；onMount → `refreshStats()`
- 布局：`column` [SessionTabs] [row flexGrow=1 minHeight=0: scrollbox(flexGrow) + Sidebar(width>=100)] [Prompt] [StatusBar]

**EDIT `src/app.tsx`**
- `sessions` signal + `refreshSessions()`（`session.list`）；onMount 调用
- `onSelectSession(id)`：`loadSession(client,id)` → `setSessionId` + `setHydrate`（失败静默，M5 加 toast）
- `onNewSession()`：`session.create` → `setSessionId` + `setHydrate({key, state: initialState()})` + `refreshSessions()`
- `location` = `formatLocation(abbreviateHome(workspace, HOME), branch)`（`util/location`）
- 命令 `session.new`（title "New session"，group "Session"，palette true）
- Session 传新 props

**NEW `frontend/test/stats.test.ts`（~6）**

**零新增依赖。**

---

## 6. TDD 用例

### Python（test_server_ipc.py）
1. `session.stats` 无 usage → `{tokens:0, maxContextTokens:48000}`
2. `session.stats` 有 usage 行 → `tokens == 最后一行 prompt_tokens`

### frontend/test/stats.test.ts（~6）
3. `formatTokens`：0→"0"、13930→"13,930"、1234567→"1,234,567"
4. `percentUsed`：13930/48000→29、0/48000→0、60000/48000→100（clamp）、100/0→0

### E2E（tmux，人工核对 + 截图）
5. 会话屏：tabs 显示最近会话 + 当前高亮 + `+ New`；点 `+ New` → 新空会话；选历史 tab → 重放并切换
6. 侧栏：`Context` tokens/% 为真实值（跑一轮后刷新）；footer `dir:branch`；窄屏（如 90 列）侧栏隐藏
7. 状态栏：`dir:branch` + tokens(%) + `ctrl+p commands` + `• ai-coding {version}`
8. 与 OC 会话屏 140×42 结构对比；M2/M3 不回归（Ctrl+P 面板、Esc 中断、refocus）

---

## 7. 方案自检（假设 → 验证 → 回退）

| # | 假设 | 验证 | 回退 |
|---|------|------|------|
| A1 | 布局 [tabs][row: scroll+sidebar][prompt][status] 不挤压 prompt | E2E 5-7 | 给 prompt/status `flexShrink={0}`（已有），row `minHeight={0}` |
| A2 | 侧栏 42 列 + 转录在 140 列下正常 | E2E 6 | 调宽或阈值 |
| A3 | `turn_usages[-1]` 是当前会话（load 后 state.session 已更新） | 用例 2 + E2E 6 | 无 |
| A4 | tabs 切换复用 `loadSession` 与 M3 一致 | E2E 5 | 无 |
| A5 | `session.create` 后 `+ New` 会话可继续对话 | E2E 5 | 无 |
| A6 | 窄屏隐藏侧栏不破坏布局 | E2E 6 | 调整阈值/换 column 堆叠 |

**最终模样走查**：会话屏顶部一行 tabs（`写一篇… | 只回答… | + New`，当前高亮）；右侧 42 列 raised 面板：标题、`Context` / `N tokens` / `X% used`、底部 `~/projects/ai-coding:feat/opencode-gui-replica`；底部一行 `dir:branch  N (X%)  ctrl+p commands  • ai-coding 0.1.0`；转录与 prompt 不变。

**复杂度盘点**：Python 1 文件小改；TS 新 3 组件 + 1 util + 3 处改（session/app/replay/dialog-sessions）；无新依赖。

---

## 8. 实施顺序

1. 后端 `session.stats`（用例 1-2 红→绿）→ pytest/ruff/mypy
2. `util/stats.ts`（用例 3-4）→ bun test 绿
3. `loadSession` 抽取 + dialog-sessions 改用
4. 三组件 + 会话屏布局 + app 接线 → tsc 绿
5. tmux E2E（5-8）对照 OC；修实际问题；文档状态 + 提交

预计：0.5–1 个专注日。
