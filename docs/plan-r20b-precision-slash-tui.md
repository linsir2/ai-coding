# R20b 方案 v2：引用精确化 / slash 面板 HTML / TUI 真机评测

> 输入：`docs/plan-r20-evidence.md`（R20 已实现并真机复跑过）。
> v1 经两个子智能体评审，**§1 有 3 处事实错误、§2 高估了 TS 解析可行性、
> §3 的 tmux 驱动有竞态**。v2 逐条修正，见 §5 的评审结论表。
> 状态：**已实现并真机评测**。执行结果、截图与残余问题见
> [r20b-eval-report.md](r20b-eval-report.md)。

---

## §0 决策点

| # | 问题 | 选项 | 状态 |
|---|---|---|---|
| 1 | `covers` 的语义 | (a) 保持交集；(b) **改成"完整包含"** | **已拍板：(b)** |
| 2 | 档 2 怎么改善 | (a) 保持放行；(b) **惰性读被引用路径的行数** | **已拍板：(b)** |
| 3 | 截图主产物 | (a) Pillow PNG；(b) **HTML `<pre>`+span 为主，PNG 出 2-3 张关键帧** | **已拍板：(b)** |
| 4 | TUI 用例分层 | (a) 25 条逐条起 tmux；(b) **Python 单测 + 8 类 TUI 专属** | **已拍板：(b)** |
| 5 | `/export html` 的改造幅度 | (a) 只加 CSS；(b) 重写渲染器；(c) **重写 + 语法高亮** | **已拍板：(c)** |
| 6 | 顺带修 TUI bug | (a) 只测不修；(b) **修 `/permissions` 坏指针 + 4 个前端独有命令的 no-op notice** | **已拍板：(b)** |

---

## §1 引用精确化

### §1.1 现状与问题

`read_ledger.covers()` 的判据是**交集**（`read_ledger.py:249`）：

```python
if same_file(mark.path, path) and mark.start <= end and start <= m.end:
    return True
```

于是"读过 1-60，引用 50-200"算**已覆盖**——尽管 61-200 从未读过。
这正是 T2 那处幻觉的形状（只是 T2 恰好完全在外）。评审指出它让 RC-7 只能抓
"完全在外"的引用。

### §1.2 语义选择

| 语义 | 规则 | 结论 |
|---|---|---|
| 交集（现状） | 与任一已读区间有交集 | 漏判率最高 |
| **完整包含** | 引用区间必须完整落在**某一个**已读区间内 | **推荐** |
| 端点各自落位 | 起点和终点各自落在某条已读区间内（允许中间跨缺口） | 与"完整包含"的差别只在跨缺口引用；**不选**，因为它会放掉"读 1-60 与 300-434，引 50-350"这种最典型的幻觉形状 |

**"完整包含"会误退什么、为什么值得**：

- **不会误伤分页读**：`merge_mark` 的合并条件是 `start <= merged[-1][1] + 1`
  （`read_ledger.py:184`）。`offset=0,limit=500` → `ReadMark(1,500)`；
  `offset=500,limit=500` → `ReadMark(501,1000)`；`501 <= 500+1` → 合并成 `1-1000`。
  引用 `1-1000` 完整包含成立。**推导已核对。**
- **会误退跨缺口引用**：读 1-60 与 300-434，引 50-350 → 退回。
  **这是设计取舍，不是缺陷**：61-299 模型没读过，退它一次的成本是 1 个迭代，
  收益是逼它去读缺口。歧义保护（≥2 个文件跳过）与"每回合最多退 1 次"是缓冲。

### §1.3 实测：三份真实答案上两种语义完全一致

| 答案 | 引用数 | 交集判未覆盖 | 包含判未覆盖 | ledger 来源 |
|---|---|---|---|---|
| 基线 i2c（含 1 处幻觉） | 7 | 1 | 1 | **该 session 文件无 `readLedger` 键**（R20 之前），按其 read 参数 `offset=60,limit=50` 重建出 `(61,110)` |
| r20 第 1 次复跑 | 15 | 0 | 0 | 落盘 `readLedger`（`llm_helpers.py` = 1-190,192-425,427-466,468-480） |
| r20 第 2 次复跑 | 1 | 0 | 0 | 落盘 `readLedger`（`mcp.py` = 611-640） |

**改成包含不会引入新的误退**，同时严格更强。

### §1.4 档 2 的改善（惰性行数）

**现状**：路径被 grep/glob/bash 见过但没读过 → 直接放行，**完全不看行号**。
**洞**：模型 grep 了 `a.py`（100 行），引 `a.py:999` → 放行。

**调研过的四条路**：

| 方案 | 做法 | 结论 |
|---|---|---|
| (a) 不改 | 档 2 保持放行 | 洞留着 |
| (b) **惰性读行数** | 只在档 2 命中时，读**被引用那一条路径**，`end > total` → 退回 | **推荐** |
| (c) 记录 grep 命中的行号 | 只能"升级覆盖"，**不能"挑战"**——grep 只显示匹配行，没显示不等于没读过 | 无实际收益 |
| (d) 内容比对 | 答案里的代码块 vs 磁盘原文（CiteGuard 路线） | 更强，**单独立项** |

**为什么 (b) 是"惰性"而不是"预先记录"**（v1 的 `seen_sizes` 被评审否掉）：

- v1 要在 `_run_tools` 里对 `seen_paths`（≤200 条）逐个 stat/读，**每批都做**。
- 而且 **`os.stat().st_size` 是字节数不是行数**——要行数必须读全文，
  10MB 的文件就是 10MB IO。v1 说"IO 有界"是错的，界是**单文件大小**。
- 惰性版：**只有档 2 真的命中时**（即模型引用了一个只被 grep/glob 见过的文件），
  才读**那一条**路径。真实数据里档 2 命中率极低（三份答案里 0 次），
  所以常态是 **0 次 IO**。

**设计**：

```python
def unsupported_claims(marks, seen, claims, *, size_of: Callable[[str], int] | None = None):
    ...
    # 档 2
    if any(same_file(p, path) for p in seen):
        if size_of is None:
            return []
        total = size_of(path)
        if total and any(end > total for _, _, end in claims):
            return [(p, s, e) for p, s, e in claims if e > total]
        return []
```

- `size_of` 由调用方注入：主闭环传"读文件数行"的闭包；`eval_claims.py` 传 `None`
  （它要复算历史 session，不该做 IO）。
- **判据是 `end > total`，不是 `start > total`**（v1 写错了）：
  `a.py:90-999` 的 `start=90 <= 100` 会漏掉，而 `end=999` 早已越界。
- **不新增 session 字段**：没有 `seen_sizes`、没有序列化改动、没有 `_run_tools` 改动。

**风险与边界**（v1 的"fail-open 是安全方向"说法被评审纠正，这里改掉）：

- 文件读不到 → `total=0` → 放行。**这是放宽，不是安全**；
  但 workspace 外的路径 `normalize` 会折叠成 basename（`read_ledger.py:108-118`），
  stat 必然失败，所以这条路径**永远放行**。这是**已知缺口**，写进文档。
- 文件在会话中被模型改长 → 读到的是新值（惰性读的好处），不会用旧值误退。
  v1 的预先记录反而有这个问题。

### §1.5 逐文件改动

| 文件 | 改动 |
|---|---|
| `ai_coding/domain/read_ledger.py` | `covers()` 改完整包含；`unsupported_claims()` 加 `size_of` 关键字参数（默认 `None`） |
| `ai_coding/core/agent_loop.py` | `_reject_unsupported_claims` 注入 `size_of` 闭包（读文件数行，best-effort） |
| `scripts/eval_claims.py` | 调用处**必须同步**（v1 漏了）：传 `None`，保持复算行为 |
| `tests/test_read_ledger.py` | 新增用例；**8 处既有调用要加 `size_of=None`**（v1 漏了） |
| `tests/test_claim_check.py` | 新增端到端用例 |
| `scripts/mutate_r20.py` | 变异锚点同步 |

### §1.6 TDD 用例

| # | 用例 | 断言 |
|---|---|---|
| P1 | `test_a_citation_spanning_a_gap_is_challenged` | 读 1-60、300-434，引 50-350 → 退回 |
| P2 | `test_a_citation_inside_one_read_range_is_covered` | 读 61-110，引 73-85 → 放行 |
| P3 | `test_adjacent_paged_reads_merge_so_the_whole_file_is_covered` | 读 1-500、501-1000，引 1-1000 → 放行 |
| P4 | `test_a_citation_past_the_end_of_a_seen_file_is_challenged` | grep 见过 `a.py`（100 行），引 `a.py:999` → 退回 |
| P5 | `test_a_citation_range_running_past_eof_is_challenged` | grep 见过 `a.py`（100 行），引 `a.py:90-999` → 退回（**v1 漏了这条**） |
| P6 | `test_a_citation_inside_a_seen_file_is_still_accepted` | grep 见过 `a.py`（100 行），引 `a.py:3` → 放行 |
| P7 | `test_an_unreadable_file_does_not_block_a_citation` | `size_of` 返回 0 → 放行 |
| P8 | `test_a_never_touched_file_is_still_challenged_with_size_of` | 档 1 在档 2 之前返回，加 `size_of` 后仍退回（**v1 漏了这条**） |
| P9 | `test_eval_claims_without_size_of_keeps_tier_two_open` | `size_of=None` 时档 2 放行（兼容老 session） |

**变异**：`covers` 恒 True → **P1 红**（P2 仍绿，v1 写错了）；
`unsupported_claims` 去掉 `end > total` 判据 → **P4/P5 红**。

---

## §2 `/export html` 会话报告的可视化优化

> **范围修正**：用户要的 HTML 是 **`/export html` 导出的会话内容**
> （`ai_coding/core/export_html.py`），不是命令面板。命令面板的调研结论
> 移到 §3.5（slash 命令可用性），那部分发现仍然有效。

### §2.1 现状

`/export html` 走 `core/export_html.py`：

- **单会话**：`render_session_html(view)`（`export_html.py:151-198`）
- **全工作区**：`render_index_html(report, views)`（`:201-220`）
- **数据源**：`TraceStore` 的 `turn-*.jsonl`（每行一个事件）+ `turn-N.full.json`
  （`capture: full` 时的消息快照）

**现在的样子**（`:31-44` 的 `_CSS`）：

```css
body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
       margin: 2rem; color: #222; }
details { border: 1px solid #ddd; border-radius: 4px; padding: .4rem .6rem; }
table { border-collapse: collapse; width: 100%; }
td.data { white-space: pre-wrap; max-width: 60ch; }
.muted { color: #777; }
```

**问题**：

| # | 问题 | 证据 |
|---|---|---|
| 1 | **纯文本表格，零视觉层次**：整个会话是"一张大表 + `details` 折叠"，工具名、参数、输出、错误全挤在 `td.data` 的一个 JSON 串里 | `export_html.py:181-188` |
| 2 | **没有会话级总览**：`render_session_html` 只有 5 个 KPI，没有回合列表、没有状态分布、没有时间线 | `:154-163` |
| 3 | **没有工具的时间线**：`ToolStarted`/`ToolFinished` 是独立 span，看不出"谁在什么时候跑了多久" | `:88-114`（读的是逐行 span） |
| 4 | **模型说的话看不见**：`AssistantTextDelta` 被折叠进 `td.data` 的 JSON 里 | 同上 |
| 5 | **压缩/降级/守卫拒绝没有可视化**：`CompactionEnded` 的层叠、`Degraded` 的原因、`guard_denied` 计数都只是 JSON | 同上 |
| 6 | **`render_index_html` 把完整 HTML 文档拼进 `<body>`**（嵌套文档） | `:217-219` 调用 `:165-168` 的输出 |
| 7 | **无主题**：硬编码亮色，不跟随系统，与 TUI 的 token 体系完全脱节 | `:31-44` |

### §2.2 设计

**产出**：同一个 `/export html`，产出**会话可视化报告**（自包含、无外部资源）。

**页面结构**：

```
<header>
  ai-coding · 会话报告
  会话 ID · 生成时间 · 模型
  [主题切换]  (跟随 prefers-color-scheme，可手动切)
<section class="kpis">
  回合数 · 步数 · prompt tokens · completion tokens · 缓存命中率 · 估算费用 · 工具调用数
  （每项一个卡片，数字大、标签小）
<section class="turns">
  每个回合一张卡片：
    头部: 回合号 + 状态徽章 + 迭代数
    指标行: token 条形图(prompt / completion / cached 三色) · 模型耗时 · 工具耗时
    工具时间线: 每个工具一行 = [图标] 名称 · 参数摘要 · 耗时 · 状态徽章 · 错误
    模型输出: AssistantTextDelta 拼成的正文（代码块按语言着色）
    压缩层: L1/L2/L3/L4 的层叠条（每层一个色块 + 删除条数 + token 变化）
    降级: Degraded 的 component / reason / fallback（黄色警示条）
<section class="plan">      （仅计划模式会话）
  PlanStepReceipt 时间线：每步一个节点（summary + attested + 状态）
<section class="issues">
  guard_denied 汇总 · degraded 汇总 · summary/disk mismatch
<footer>
  数据来源 + 复跑命令
```

**视觉 token**（来自 `frontend/src/theme/tokens.ts`，已逐个核对）：

| 元素 | token | dark | light |
|---|---|---|---|
| 页面底 | `backgroundBase` | `#0a0a0a` | `#ffffff` |
| 卡片底 | `backgroundRaisedHigh` | `#1e1e1e` | `#ececec` |
| 主文 | `textBase` | `#eeeeee` | `#1a1a1a` |
| 次要 | `textMuted` | `#808080` | `#707070` |
| 组头 | `hueAccent` | `#9d7cd8` | `#7a5fb8` |
| 成功 | `feedbackSuccess` | `#7fd88f` | `#3d9e52` |
| 信息 | `feedbackInfo` | `#56b6c2` | `#2a8f9e` |
| 警告 | `feedbackWarning` | `#f5a742` | `#b8791f` |
| 错误 | `feedbackError` | `#e06c75` | `#c53b53` |
| 边框 | `borderBase` | `#484848` | `#b8b8b8` |
| diff 增 | `diffAdded` | `#4fd6be` | `#1f9d85` |
| diff 删 | `diffRemoved` | `#c53b53` | `#c53b53` |

**默认跟随 `prefers-color-scheme`**，提供 `<button aria-pressed>` 手动切换。
（v1 说"默认暗色"的理由不成立：现有导出本来就是亮色。）

**工具图标**：纯 CSS/Unicode（`⏎` 读、`✎` 写、`▶` bash、`🔍` grep、`📁` glob、
`🤖` subAgent…），**不引入外部图标字体**（自包含约束）。

**语法高亮**（已拍板：做）：自己写一个**多语言轻量 tokenizer**，覆盖
会话里实际出现的语言：

| 语言 | 识别依据 | 处理 |
|---|---|---|
| Python | `.py` / `def ` `import ` | 注释 `#`、字符串（三引号/单/双引号/f-string）、数字、关键字、装饰器、内建 |
| TypeScript / JavaScript | `.ts` `.tsx` `.js` `.jsx` | 注释 `//` `/* */`、字符串（含模板串）、数字、关键字 |
| JSON | `.json` | 键（字符串后跟 `:`）、字符串、数字、字面量 |
| Markdown | `.md` / 模型正文 | 标题、代码围栏、行内代码、粗体、列表 |
| Bash / Shell | `bash` 工具输出 | 注释、字符串、变量 `$X`、关键字 |
| YAML / TOML | `.yaml` `.yml` `.toml` | 注释、键、字符串 |
| diff | `diff` 工具输出 | `+`/`-` 行用 `diffAdded`/`diffRemoved` |
| 其他 | — | 纯文本，只转义 |

**实现要点**：
- **先 tokenize 再逐 token `html.escape`**——不能先 escape 再高亮（实体会被切开）。
- 每语言一个正则序列，按优先级匹配；未匹配的字符原样转义输出。
- 输出 `<span class="tok-kw">…</span>`，CSS 里用 token 上色。
- **不做语义分析**（不区分变量名与函数名），只做词法着色。
- 代码块来源：`read`/`grep`/`bash`/`write` 的输出 + 模型正文里的围栏块。

**安全**：所有动态字符串**在 tokenize 阶段逐段 `html.escape`**
（沿用 `export_html.py:4-7` 的约定）。**这是硬约束**——工具输出可以是攻击者可控的。

**可访问性**：语义标签（`header/main/section/table`）、徽章**文字+颜色**双编码、
切换按钮可 Tab 聚焦、`@media (prefers-reduced-motion: reduce)`。

### §2.3 数据可得性核对

| 要展示的东西 | 事件 | 字段 | 有无 |
|---|---|---|---|
| 回合状态 | `TurnFinished` | `status`/`reason`/`iterations`/`handoff` | ✓ |
| 迭代数上限 | `IterationStarted` | `max_iterations` | ✓ |
| token 用量 | `ModelStepFinished` | `usage.prompt_tokens`/`completion_tokens`/`cached_tokens` | ✓ |
| 模型耗时 | `ModelStepFinished` | `latency_ms` | ✓ |
| 工具名/参数/结果/错误/耗时 | `ToolStarted` + `ToolFinished` | `tool_name`/`params`/`output`/`error`/`duration_ms` | ✓ |
| 模型说的话 | `AssistantTextDelta` | `text` | ✓ |
| 压缩层 | `CompactionEnded` | `layers[].name/removed/rewritten/skipped/before_tokens/after_tokens` | ✓ |
| 降级 | `Degraded` | `component`/`reason`/`fallback` | ✓ |
| 守卫拒绝 | `ToolFinished.error == guard_denied` | — | ✓ |
| 计划步收据 | `PlanStepReceipt` | `summary`/`attested`/… | ✓ |
| agent 切换 | `AgentSwitched` | `from`/`to` | ✓ |
| 消息快照 | `turn-N.full.json` | — | 仅 `capture: full` 时 |

**注意**：`capture: full` 才有消息快照，默认 `previews`（≤500 字符）。
所以**模型正文**在未开 full 时只能从 `AssistantTextDelta` 拼，**这是主要来源**，
`full.json` 只是补充。

### §2.4 逐文件改动

| 文件 | 改动 |
|---|---|
| `ai_coding/core/export_html.py` | **重写渲染器**：`_CSS` 换成 token 体系；`render_session_html` 改成结构化报告；`render_index_html` 修嵌套文档 + 加总览 |
| `ai_coding/core/trace_text.py` | 加一个"把 span 折叠成回合视图"的纯函数（工具配对、正文拼接） |
| `ai_coding/core/export_highlight.py`（新） | 多语言轻量 tokenizer + `highlight(code, lang) -> html` |
| `tests/test_export_html.py` | 新增：XSS 转义、分组、徽章、token 条、嵌套文档修复 |
| `tests/test_export_highlight.py`（新） | 每语言一组：注释/字符串/数字/关键字着色正确；**XSS 不被执行**；未知语言降级 |
| `scripts/` | 生成一份样例导出，用于人工核对 |

**规模**：渲染器 ~300-400 行（CSS ~150 + 渲染 ~200）+ 高亮器 ~250 行。
**不引入任何依赖**。

### §2.5 与 `/export palette` 的关系

**不做** `/export palette`（v1 的设计已被范围修正否掉）。
`/export` 的格式白名单保持 `html | otlp`。

---

## §3 TUI 真机评测方案

### §3.1 环境（已实测）

| 项 | 值 |
|---|---|
| tmux | 3.4 |
| bun | 1.2.14 |
| Python | 3.14.8（`.venv`） |
| 字体 | `FiraCode-Regular.ttf`、`wqy-microhei.ttc`（CJK） |
| 截图 | **HTML `<pre>`+span 为主**；Pillow 12.3.0 出 2-3 张关键帧 PNG |
| 已有残留 | tmux `default` 会话里有一个 08:34 启动的旧 TUI，**不动它** |

### §3.2 驱动方式（v1 的 Escape 聚焦是错的）

```bash
tmux new-session -d -s r20 -x 200 -y 50 \
  "cd /workspace/ai-coding && .venv/bin/ai-coding --path <临时配置> \
   --workspace /workspace/eval-workspace/r20-tui --skills /workspace/ai-coding/skills"

# 就绪轮询（v1 没有，会导致按键丢失）
until tmux capture-pane -t r20 -p | grep -qE "Ask anything|shift\+tab"; do sleep 1; done

tmux send-keys -t r20 "/plan" Enter
tmux capture-pane -t r20 -p -e -S -2000 -J > /tmp/shot.ansi
```

**v1 的 `send-keys Escape` 要删掉**：输入框本来就自动聚焦
（`prompt.tsx:278-284` 的 `onMount`）；Home 路由没绑 Escape；
而 Session 路由把 Escape 绑到**两步式中断**（`session.tsx:498` + `util/esc.ts`：
第一次 arm，5 秒内第二次才真中断）。脚本随手发 Escape 会把中断"上膛"。

**其他修正**：
- `capture-pane` 加 `-S -2000`（抓 scrollback，否则滚出的模型回复拿不到）
  与 `-J`（拼接行）；同时读 `sessions/<id>.json` 作为结构化证据。
- tmux 配置显式设 `default-terminal "tmux-256color"` 与
  `terminal-overrides ",*:Tc"`（否则真彩色被降级）。
- **不用仓库的 `config.local.yaml`**：它含字面量 api_key，且没有 `tracing:` 段。
  改用临时配置（照 `scripts/t12_live_check.py:47-54` 的 `_temp_config`），
  设 `tracing.capture: full`、`tracing.redact: false`，api_key 从环境注入。
- 每次 `send-keys Enter` 后**轮询空闲标志**（占位符回到 `Ask anything…`
  或 trace 出现 `TurnFinished`），不用固定 `sleep`。

### §3.3 截图管线

**主产物：HTML**。SGR → `<span style="color:…;background:…">`，外层 `<pre>`。
浏览器天然处理 CJK 宽度与字体回退，颜色不降级，文件可 diff。

**ANSI 状态机必须处理**（v1 只写"解析 SGR"是不够的）：
- 256 色 `38;5;n` / `48;5;n`；真彩色 `38;2;r;g;b` / `48;2;r;g;b`
- 粗体 `1/22`、dim `2`、italic `3/23`、下划线 `4/24`、反色 `7/27`
- 默认色 `39`/`49`、复位 `0`、复合参数（`1;31`、`38;5;n;48;5;m`）的分号解析
- **每行开头 tmux 会重发 SGR**（不延续上一行），解析器按行重置状态

**PNG（可选，2-3 张关键帧）**：按**样式游程**绘制（同行同 fg/bg/bold/font 的
连续格拼成一个字符串一次 `draw.text`），CJK 单独切分；以 `wcwidth` 为唯一列宽来源，
两个字体的字号用 `font.getlength` 校准到"1 格 / 2 格"。

### §3.4 评测用例表

**判据必须可自动判定**（v1 的"T1 首页渲染"不是谓词）。每条给
"断言源 + 断言串/字段"：

| # | 场景 | 操作 | 断言源 | 断言 |
|---|---|---|---|---|
| T1 | 启动 | 启动 TUI | capture | 含 `Ask anything`（`home.tsx:139`）、含 `shift+tab`（177）、含版本串（194）、**不含** `Traceback` |
| T2 | 主闭环 | 输入文本 + Enter | capture + trace | 含工具卡标题；trace 有 `ToolStarted`/`ToolFinished` |
| T3 | slash 补全 | 输入 `/` | capture | 含 `/help`（列表首项，`slash.ts:37`） |
| T4 | 命令面板 | Ctrl+X 松开 P | capture | 含 `Commands`（`command-palette.tsx:30`）；输入 `mod` 后含 `Models` |
| T5 | 帮助 | `/help` | capture | 含 `Help`（`dialog-help.tsx:33`） |
| T6 | plan 进入 | `/plan` | capture + trace + session | 状态栏含 `PLAN`；trace 有 `AgentSwitched`；session `agent == "plan"` |
| T7 | 规划轮 | 输入规划请求 | trace | `IterationStarted.max_iterations == 16`；`ToolFinished` 里**没有** mutating 工具 |
| T8 | 提交计划 | 等 `present_plan` | capture | 含 `◆ Plan review`（`plan-review.tsx:147`） |
| T9 | 批准 | 按 `1` | capture + session | session `task.armed == true`；计划卡出现 |
| T10 | 执行 | `/plan run` | trace | 有 `PlanStepReceipt`；逐步回执 |
| T11 | 退出 | `/plan off` | capture + session | 状态栏回 `build`；`task` 清空 |
| T12 | 附件 | 粘贴图片 | capture + session | 图片落盘、消息带图；busy 时被拒 |
| T13 | @ 提及 | 输入 `@` | capture | 提及列表出现（技能 + 文件两源） |
| T14 | Esc 两步中断 | 回合中按两次 Esc | trace | 第一次 arm（无中断）；5 秒内第二次 → `TurnFinished.status == aborted` |
| T15 | steering / inbox | 回合中输入文本 | capture + trace | 进 `state.queued` / inbox 面板 |
| T16 | 会话 resume | `/resume` | capture + session | 切到目标会话，历史重投影 |
| T17 | 子智能体 / jobs | `/tasks` | capture | run 列表 + job 列表 |
| T18 | 权限卡 / Ask 卡 | 触发一次需审批的工具 | capture | 出现权限卡（与 plan 卡不同槽位） |
| T19 | `/permissions` | 补全选中 | capture | **修好后**应打开权限面板（`dialog.permissions.show`） |
| T20 | `/export html` | 执行 | 文件系统 | 生成 `traces-*.html`；含 KPI 带、回合卡、工具时间线、代码块着色 |

**负向用例**（单列）：busy 拒绝、命令带图被扣留、tracing 关闭时 `/export` 的提示。

### §3.5 slash 命令可用性（分层，决策 5）

#### 调研发现的 5 个真 bug（TUI 侧）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| 1 | **`/permissions` 指向不存在的键位命令** `session.permissions`（实际注册 `dialog.permissions.show`） | `frontend/src/util/slash.ts:121` vs `app.tsx:508`；全仓只有 `slash.ts:121` 一处引用 | 补全选中后 **静默无反应**；`prompt.tsx:213` 先清空输入框，用户敲的字也一起消失 |
| 2 | 前后端清单漂移：**后端独有 7 个**（`clear`/`model`/`resume`/`agents`/`goal`/`plan`/`readonly`，后两个**有意**不广告，被 `frontend/test/slash.test.ts:110-118` 钉住）；**前端独有 4 个**（`models`/`mcp`/`sessions`/`new`） | `slash.ts:35-131` vs `app/slash.py:22-51` | — |
| 3 | 前端独有的 4 个名字**手打会被后端判成普通提示词**发给模型（`parse_slash` 返回 None） | `slash.py:74-77`；`slash.ts:43-86` | 用户以为在切模型，实际在跟模型聊天 |
| 4 | 命令面板与 TUI `/help` **同源**，都不含任何文本 slash 命令（`/export` `/compact` `/undo` `/redo` `/budget` `/trace` `/todo` `/config` `/stats`） | `command-palette.tsx:19`、`dialog-help.tsx:18` 都只收 `palette === true` | 这些命令在 TUI 里**没有任何发现入口** |
| 5 | **同名双实现**：`/status`、`/skills`、`/memory`、`/connect` 在"补全选中"与"手打"两条路上给不同结果 | `slash.ts` vs `commands.py` 各分支 | 单值状态标签表达不了 |

次级：`prompt.tsx:210-222` 丢弃 `onSubmit` 返回值（busy+图片时图片与拒绝理由
被静默丢弃）；`slash.suggest_command` 在 TUI 里不可达。

#### 分层

| 层 | 覆盖 | 方式 |
|---|---|---|
| **Python 单测** | 识别 / 别名映射 / 文本输出前缀 | `tests/test_slash*.py` + 新增一致性用例 |
| **差集测试** | `slash.list` vs `SLASH_COMMANDS` | 新增 |
| **TUI 专属（8 类）** | 坏指针（T19）、面板类（`/help /status /skills /memory /connect`）、未广告但可用（`/plan /readonly /agents /goal`）、文件产物（`/export`）、模型回合（`/review`）、状态改写（`/compact /undo /redo`）、生命周期（`/clear /quit`）、后台（`/tasks`） | tmux |

**不再 25 条逐条起 tmux**：`/budget /trace /todo /config /stats` 是纯文本报告，
后端单测断言输出前缀即可。

#### 顺带修（决策 4）

- `slash.ts:121` 的坏指针 → `dialog.permissions.show`；
- 后端 `_ALIASES` 加 `models`/`mcp`/`sessions`/`new`，各给一条
  "请用 `<leader>m` / Ctrl+X P" 的 notice —— 把"手打变模型回合"变成"明确提示"，
  **不改前端清单**，不违反"不广告"。

### §3.6 观测点

| 信号 | 来源 |
|---|---|
| `AgentSwitched` | trace jsonl |
| `TaskUpdated`（armed / items） | trace / session JSON |
| `PlanStepReceipt` | trace jsonl |
| `TurnFinished.status` + `note` | trace / session JSON |
| `ToolFinished.error` | trace jsonl |
| `ContextAssembled.tail_blocks` | trace jsonl（验证 `<read-ledger>` 块） |
| session JSON 的 `agent`/`readonly`/`task`/`step_deliveries` | `sessions/<id>.json` |

### §3.7 时间预算（v1 没有）

- T1-T11（含一次 plan 规划 16 轮上限 + 批准 + 逐步执行）：**15-30 分钟**
- T12-T20 + 8 类 slash：**20-40 分钟**
- 全量一遍 **40-90 分钟**（不含模型抖动重试）
- 每步 `turn_timeout_seconds` = 600（`config.local.yaml:35`）
- **模型不确定即判 INCONCLUSIVE 并重跑一次**，不让一次失败卡死整轮

---

## §4 不做的事

1. **不新增、不删除前端 `SLASH_COMMANDS` 条目**；**只修正 `command` 指针**
   （`/permissions`）与描述文案。这样与 `frontend/test/slash.test.ts:110-118`
   的"plan/readonly 不广告"不冲突。
2. **不做引用内容比对**（CiteGuard 路线）：单独立项。
3. **不给 TUI 加 headless 模式**：现有 `scripts/t12_live_check.py` 与
   `scripts/plan_lifecycle_live_check.py` 已覆盖无头驱动。
4. **不重写 `export_html.py` 之外的东西**：只重写渲染器，不动 `TraceStore`、
   不动 `trace_text.py` 的现有纯函数（只新增）。
5. **不动 tmux `default` 会话里的旧 TUI**。
6. **"shift+tab 进 plan 对用户隐形"是已知并接受**的缺口，不在本轮改。
7. **不做 `/export palette`**（v1 设计已被范围修正否掉）。

---

## §5 v1 → v2 的评审结论表

| # | v1 的问题 | 修法 |
|---|---|---|
| 1 | §1.2 "并集包含"定义自相矛盾 | 改成"端点各自落位"，并说明为什么不选 |
| 2 | §1.4 `stat` 拿不到行数；"IO 有界"是错的 | 改惰性读，说明界是单文件大小 |
| 3 | §1.4 判据 `start > total` 漏 `end > total` | 改 `end > total` + 补 P5 |
| 4 | §1.4 "fail-open 是安全方向" 方向说反 | 改成"已知缺口"，并说明 workspace 外路径折叠的根源 |
| 5 | §1.5 漏 `eval_claims.py`、8 处测试调用、变异锚点 | 全部补进改动表 |
| 6 | §1.6 变异描述写错（P2 不会红） | 改成"P1 红" |
| 7 | §1.3 没说 i2c 的 ledger 是重建的 | 加列注明 |
| 8 | §2.1 差集写成 5 个，实际 7 个 | 改 7，并标注 plan/readonly 有意 |
| 9 | §2.1 漏第 5 个 bug（TUI `/help` 同源缺失） | 补 |
| 10 | §2.2 正则解析 keymap 不可行 | 删掉（范围修正后整节移除） |
| 11 | §2.2 默认暗色的理由不成立 | 改 `prefers-color-scheme` + 可切换 |
| 12 | §2.2 无语义标签 | 补可访问性要求 |
| 13 | §3.2 Escape 聚焦是错的，且会误触中断 | 删 Escape，改就绪轮询 |
| 14 | §3.2 无 scrollback、真彩色降级 | 加 `-S -2000 -J` 与 tmux 配置 |
| 15 | §3.2 直接用仓库 config 会泄密钥 | 改临时配置 + 环境注入 |
| 16 | §3.3 判据不是谓词 | 每条给"断言源 + 断言串" |
| 17 | §3.3 漏 8 个场景 | 补 T12-T20 + 负向 |
| 18 | §3.5 25 条逐条起 tmux 太贵 | 分层：Python 单测 + 8 类 TUI |
| 19 | §3 无时间预算 | 补 §3.7 |
| 20 | §4.1 与决策 4 自相矛盾 | 改成"不增删、只修指针" |
| 21 | 放过"手打 4 个前端独有命令变模型回合" | 后端加 no-op notice |
| 22 | **范围搞错了**：把"slash 界面 HTML"理解成命令面板 | 改成 `/export html` 会话报告的可视化优化 |
