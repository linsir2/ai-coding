# 批次 4 修复方案（R22）

> 输入：`docs/r22-feature-matrix.md`（A-1…A-10）、`docs/r22-context-audit.md`（H10–H12）。
> 规则：每项先红（测试/帧复现），再改，再绿；改完做一次代码评审。
> 本轮**只做**下列 5 项，其余（A-4 错误码契约、A-10 时长、H10 引用注入）留待独立方案。

## FIX-1（P1）建议条劫持手打命令 —— A-2

**现状**：`util/slash.ts::filterSlashCommands` 用 `keys: ["name","description"]` + `threshold: 0` 做模糊匹配，
于是 `/readonly`、`/plan`、`/agents`、`/clear`、`/resume` 这些**后端认识、前端目录里没有**的命令，
会被"某条目录项的 name/description 的子序列"命中；而 `prompt.tsx:357-363` 在建议条可见时把 `return`
绑给 `prompt.autocomplete.select`，回车执行的是**被选中项**（`/readonly`→`/export`，实测 3 次）。

**根因（两层）**：
1. 匹配键包含 `description`（`permissions` 的描述里有 "auto|confirm|clear"，所以 `clear` 命中它；
   `tasks` 的描述里有 "sub-agent runs"，所以 `agents` 命中它）；
2. 命中即选，没有"我打的就是一条完整命令"的短路。

**改法（最小、可测）**：
- `filterSlashCommands` 只对 **name** 做前缀/子串匹配（`name.includes(query)`，忽略大小写），不再搜 description；
  description 仍显示在行内，调色板（ctrl+x p）继续搜它。
- 新增纯函数 `exactSlashCommand(text, known)`：`text.trim()` 与 `"/"+名字` 完全相等时返回该名字（`known` = 后端公布的
  `props.commands` ∪ 目录项）。`prompt.tsx` 的 `submit()` 与 `refresh()` 用它：命中时不弹建议条，回车直接提交文本。
- 两条都做，是为了"短查询还能补全（`/re` → review/redo），完整命令一定原样送出"。

**测试（frontend/test/slash.test.ts）**：
- `filterSlashCommands("readonly")` / `("plan")` / `("agents")` / `("clear")` / `("resume")` → `[]`；
- `filterSlashCommands("hel")[0].name === "help"`、`("mod") → models`、`("zzzzz") → []` 保持；
- `exactSlashCommand("/readonly", names)` 为真、`("/readonly x", names)` 为假；
- 回归：`/review`、`/models` 仍走原路径（前者提交文本，后者 `ownCommand` 打开对话框）。

**风险**：描述搜索没了 → 用户打 "diff" 找不到 `/review`（原来靠描述 "Review the current git diff"）。缓解：
调色板仍可搜描述；建议条只对 name。若评审反对，退一步：description 只作为**同分时的次序**，不作为命中。

## FIX-2（P2）`/trace` 把"没有回合"说成"没开 trace" —— A-1

**现状**：`app/commands.py:1304-1312` 只看 `recorder.current_path`，为 None 就返回 `tracing is off`；
而 `/budget`（`commands.py:1315-1330`）分四态，同一时刻说 `本会话还没跑过回合`。实测两帧互斥。

**改法**：`_trace_report` 先分三态——`loop.trace is None` → off；`recorder.current_path is None` →
`[trace] 本会话还没跑过回合（跑一轮后可用）`；否则照旧。

**测试**（`tests/test_app_commands.py` 若无则新建）：三个假 loop：无 trace / 有 trace 无 path / 有 path（tmp 文件写一行）。

## FIX-3（P2）`/undo` 的 toast 标题写成 `Compacted` —— A-3

**现状**：`server/handlers.py:641-646` 对"同会话历史变短"一律发 `compacted{before,after}`；
`routes/session.tsx:596-613` 一律 `title: "Compacted"`。实测 `/undo` 后提示 `Compacted 7 → 0 messages`。

**改法**：payload 增 `"action": action`（`compact` / `undo` / `redo` / …，现有参数里已有）；
客户端按 action 映射标题：`compact → Compacted`、`undo → Undone`、`redo → Redone`、其它 → `History rewritten`。

**测试**：前端纯函数 `compactionTitle(action)`（新增在 `util/`）+ 单测三分支；后端断言 payload 带 action。

## FIX-4（P2）空压缩也把上下文计量清零 —— A-8

**现状**：`core/compaction.py:182-183` 在 `CompactionEnded` 前无条件写 `session.last_compacted_turn`；
`domain/session.py:582-586` 据此把所有 usage 判为"已被压缩"，计量读 0。实测 `/compact` 报 7→7（三层都 681→681），
但侧栏立刻变成 `0 tokens / 0%`，读起来像"上下文被清空"。

**改法**：只有在**估计值真的变小**时才抬边界——把 `layers` 先算出来，`layers[-1].after_tokens < layers[0].before_tokens`
才写 `last_compacted_turn`；否则不动（计量继续显示上一轮真实占用）。

**测试**（`tests/test_compaction*.py`）：一次 `rewritten=0, before==after` 的压缩后，`session.last_compacted_turn` 不变、
`live_prompt_tokens()` 仍返回上一轮值；真正变小的一次则抬边界。用假 context 桩，不调模型。

## FIX-5（P3）压缩行给个图例 —— A-8 的文案半边

**现状**：`L3 681→681 tok / L1 681→681 tok / L2 681→681 tok` 里的 L1/L2/L3 没有任何解释（前端 `message.tsx:204-210`）。

**改法**：在该行末尾追加一次性图例 `（L3 落盘 · L1 剪断 · L2 占位 · L4 摘要）`，不新增行，不改布局。
**测试**：`layerLine` 的渲染快照（`composer-meta-render` 或新增 `compaction-render`）。

## 不做（本轮）

- A-4（`bwrap.unavailable` → `denied`）：牵动 `ToolResult.error` 契约与文档，需独立方案。
- A-9（调色板滚动/搜索命中 `Agents`）：布局改动，留批次 5。
- A-10（工具时长含审批等待）：与 D7 的 turnId 记账有关，单独方案。
- H10（`referenced_files` 超预算）：改动面最大，且在审计里已定性，留独立方案 + 消融实验。

---

## 完成记录（本轮已落地）

| 项 | 改动 | 测试 | 真机复验 |
| --- | --- | --- | --- |
| FIX-1 | `util/slash.ts::filterSlashCommands` 改为**只按 name 的大小写不敏感子串匹配**（前缀优先、短名优先），不再搜 description、不再用子序列模糊 | `frontend/test/slash.test.ts` +2（`readonly/plan/agents/clear/resume/goal → []`、`diff → []`）；全量 525 通过 | `/readonly` → `[readonly] 用法：/readonly on|off（当前：关）`；`/agents` → `[agents] 4 available · current: build` + 表；`/plan` → `已进入计划模式（只读）…`；`/resume` → 会话列表（帧 90–93）|
| FIX-2 | `_trace_report(loop, session)` 三态 + `_trace_is_another_session`（父目录名 ≠ 当前 sid 视为"别的会话"）| `tests/test_repl.py` +4（off / 无回合 / 旧会话文件 / 正常渲染）| `/trace` → `[trace] 本会话还没跑过回合（跑完一轮后可看）`（帧 94）|
| FIX-3 | `util/history.ts::historyRewriteTitle(rewound)`；`session.tsx` 以**事件里有无 undo 载荷**判定标题，且只有历史真的变短才回填草稿（`/redo` 不再重灌）| `frontend/test/history.test.ts` 2 例 | 未改真机（需一次 `/undo`）——见下方"遗留" |
| FIX-4 | `core/compaction.py::_freed_tokens`：token 变小**或**消息数变小才抬 `last_compacted_turn`（覆盖 `hard_trim` 不在 layers 里的情况）| `tests/test_compaction.py` +3（无变化保持计量 / 真压缩抬边界 / 仅 hard_trim 收缩也算）| 未改真机（需一次空 `/compact`）——见下方"遗留" |
| FIX-5 | `component/message.tsx::layerLine` 导出并给每层加一词（`L1 剪断 / L2 占位 / L3 落盘 / L4 摘要`）| `frontend/test/layer-line.test.ts` 3 例 | 同上 |

## 代码评审处置（第二轮）

评审结论：**0 个 blocker、6 个 minor**。已并入 3 条，其余记录在案：

| 评审意见 | 处置 |
| --- | --- |
| `freed` 只比 layers 会漏掉 `hard_trim` 收缩（它跑在 layers 之后、且不是图层）| **已改**：`token 变小 || 消息数变小`，并补了专门的测试 |
| `/redo` 也带 undo 载荷，会把被撤销的提示词重新灌回输入框 | **已改**：只在 `compacted`（历史真的变短）时回填 |
| 标题按文本真值判断，纯图片回合会被叫成 "Compacted" | **已改**：改为按"有无 undo 载荷"判断（`historyRewriteTitle(rewound: boolean)`）|
| 调色板走另一套搜索（`command-palette.tsx` + `dialog-select.ts`），按描述搜的能力仍在；但 slash 里 10 条纯文本条目的描述不再可搜 | 接受：描述仍显示在行内；纯文本条目的发现性靠名字（`budget/trace/todo/…` 都是名词）|
| 空查询返回 `SLASH_COMMANDS` 常量引用，外部若就地 sort 会改到目录 | 记录：唯一调用方 `prompt.tsx:244` 只 `.map`；测试 `toBe` 依赖同一引用 |
| `_trace_is_another_session` 的 docstring 声称校验"目录形状"，实际只比名字 | 记录：行为保守（拿不准就按"本会话"渲染），文案已在 docstring 里写明 |

## 遗留（未在真机复验的两项）

- FIX-3（`/undo` toast 标题）与 FIX-4（空 `/compact` 后计量不再归零）需要各跑一次带历史的会话才能看到效果；
  两项都有单测覆盖，真机复验放到批次 6 的回归里一起做（S1 已留下可复用的会话 `957ab4e2`）。
