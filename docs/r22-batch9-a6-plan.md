# 批次 9-4 方案（R22·A-6 空态质量不一致）

> 只出方案。依据：`docs/r22-feature-matrix.md` §5 A-6、真机帧 `05-mcp` / `06-sessions` / `07-skills`。

## 1. 调研

三个面板都复用了同一个列表组件 `DialogSelect`，它的空态是写死的：

```tsx
fallback={<text fg={theme.textMuted}>{"  no matches"}</text>}
```

于是：

| 面板 | 空态（帧） | 问题 |
| --- | --- | --- |
| `/mcp`（05） | 正文 `no matches`；footer `没有配置 MCP 服务器 · MCP 已关闭（…）· 先打开它才能接入` | 正文英文；解释在 footer，正文没有 |
| `/sessions`（06） | 正文 `no matches`；footer 只有按键提示 | 英文，且不说明"会话怎么来" |
| `/skills`（07） | 正文 `no matches`；footer `enter add to prompt` | 英文，且不说明"技能放哪里" |

其它同类面板（`/memory`、`/jobs`、`/inbox`）已各自写中文空态；`/models` 有默认行。真正的缺口是
**通用组件没有"无数据"与"筛选无命中"的区分**，也没有给面板一个覆盖入口。

技能根路径（`ai_coding/paths.py`）：工作区 `skills/`（`project_skills`）、数据目录 `skills/`
（`user_skills`）、兼容的 `.claude/skills`/`.agents/skills`。会话由第一条消息创建
（`agent_loop` 首轮建 `TaskState`，会话文件同时落盘）。

## 2. 方案

**FIX-A6-1：`DialogSelect` 区分两种空态，并允许面板覆盖。**

`util/dialog-select.ts` 加纯函数（决策可单测）：

```ts
/** 无数据 vs 有数据但搜索无命中，各给一句人话；面板可覆盖。 */
export function emptyLabel(
  hasOptions: boolean, emptyText?: string, noMatchText?: string,
): string {
  return hasOptions ? (noMatchText ?? "没有匹配项") : (emptyText ?? "没有可选项")
}
```

`component/dialog-select.tsx`：
- props 增加 `emptyText?: string`、`noMatchText?: string`；
- fallback 改为 `{"  " + emptyLabel(props.options.length > 0, props.emptyText, props.noMatchText)}`。

**FIX-A6-2：三个面板各说一句"为什么空 + 下一步"。**

| 面板 | 传入 `emptyText` |
| --- | --- |
| `/skills` | `还没有技能（把带 SKILL.md 的目录放进工作区 skills/）` |
| `/sessions` | `还没有会话（发一条消息就会创建）` |
| `/mcp` | `没有配置 MCP 服务器`（footer 里重复的那句删掉，保留"已关闭/怎么开"） |

其余面板（agents、inbox、jobs、models、command palette）不传，自动得到中文默认文案，
不新增分支。

## 3. 影响面

- 布局/滚动/选择逻辑不变；只有 fallback 一行文本与两个可选 prop；
- 无 IPC、无后端、无数据契约；
- `dialog-mcp-render.test.tsx` 的空态断言（正文含"没有配置 MCP 服务器"）继续成立，
  因为该句从 footer 移到正文。

## 4. RIPR

### Red
- `test/dialog-select.test.ts`：`emptyLabel(false)` === `没有可选项`；`emptyLabel(true)` === `没有匹配项`；
  自定义覆盖优先。
- 新增 `test/dialog-empty-state-render.test.tsx`：
  - `DialogSelect` 无 options → 帧含 `没有可选项`、不含 `no matches`；
  - 传 `emptyText="自定义空态"` → 帧含该句；
  - `DialogSkills`（`skills.list` 返回 `[]`）→ 帧含 `还没有技能` 与 `skills/`；
  - `DialogSessions`（`session.list` 返回 `[]`）→ 帧含 `还没有会话`。

### Implement
`util/dialog-select.ts` → `component/dialog-select.tsx` → 三个面板 → 测试。

### Prove
`bun test test/dialog-select.test.ts test/dialog-empty-state-render.test.tsx
test/dialog-mcp-render.test.tsx`；全量 `bun test` + `bun run typecheck`。

### Refactor
一个纯函数、两个可选 prop；不新增组件、不复制三份 fallback。

## 5. 真机与验收

在 `r22-s1`（空工作区、无技能、无 MCP）依次打开 `/skills`、`/sessions`、`/mcp`：
正文分别为"还没有技能（… skills/）"、"还没有会话（发一条消息就会创建）"、
"没有配置 MCP 服务器"，均不再出现 `no matches`；帧存 `/tmp/r22-shots/161–163-*`。

## 6. 完成记录

- 实现：`emptyLabel` 纯函数 + `DialogSelect` 两个可选 prop；skills/sessions/mcp 传各自文案；
  mcp 的 footer 删掉重复句、保留开关/下一步。
- 评审修正：① 加载中/请求失败不得声称"还没有…"——skills、sessions 增加 `loaded` 闸门，
  未成功返回前退回通用文案；② mcp 的空态测试由 `toContain` 收紧为"该句所在行不含 esc close"，
  否则旧 footer 也能通过。
- 测试：新增纯函数 3 例 + 渲染 4 例；`dialog-mcp-render` 收紧；前端全量 **563 通过 / 0 失败**，
  `tsc` 干净。
- 真机（空工作区 `/tmp/r22-a6-ws`，帧 161–163）：
  - `/skills` → `还没有技能（把带 SKILL.md 的目录放进工作区 skills/）`
  - `/sessions` → `还没有会话（发一条消息就会创建）`
  - `/mcp` → 正文 `没有配置 MCP 服务器`，footer `MCP 已关闭（…）· 先打开它才能接入 · esc close`
- 明确不改：`/stats`、REPL `/skills` 的英文行（条目外）；memory/jobs/inbox 的正文通用文案
  + footer 中文解释（已有消费，保持现状）；搜索框占位符 `Search`。
