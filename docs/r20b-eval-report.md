# R20b 评测报告：引用精确化 / `/export html` / TUI 真机

> 方案见 [plan-r20b-precision-slash-tui.md](plan-r20b-precision-slash-tui.md)。
> 本文件是**执行结果与证据**：改了什么、真机跑出什么、还剩下什么。
> 日期 2026-10-28。所有截图在 `/workspace/exports/shots/`，会话报告在 `/workspace/exports/index.html`。

---

## §0 结论摘要

| 项 | 结论 |
|---|---|
| 决定 1：`covers` 覆盖语义 | 已改为**完整包含**。三份真实答案复算：未覆盖条数与"交集"**完全一致**（1/1、0/0、0/0），即改强不引入新误退。 |
| 决定 2：档 2 不看行号 | 已改为**惰性读行数**：只有档 2 真的命中时才读**被引用那一条**文件，`end > total` 才退回。常态 0 次 IO。 |
| `/export html` | 重写为结构化报告（KPI 行 / 回合卡片 / 工具时间线 / 语法高亮 / 暗亮切换）。真机导出成功，暗亮两套均人工核对。 |
| TUI 真机 | plan 模式与主闭环**全流程跑通**（计划 → 批准 → 写文件 → 两条验收 → Todos 2/2）。 |
| slash 命令 | 面板 22 条**全部可用**；后端 25 条全部可达。修掉 2 类真实故障（死指针、名字打到模型）。 |
| 顺带修掉 | 6 个 TUI/HTML 缺陷（见 §3.4、§2.2）。 |
| 未修（已记录） | 5 条，见 §3.5，均为观感或上游库问题，不影响功能。 |

验证：`pytest` 全绿、`ruff`/`mypy` 干净、R20 变异脚本 **25/25 全杀**、前端 `bun test` 505 通过 + `tsc --noEmit` 通过。

---

## §1 两个决定的落地

### §1.1 决定 1：什么叫"完整引用精确"

**调研结论：在"只靠读证据表"的前提下，完整包含就是能做到的最精确语义。**

三种候选的实际判据：

| 语义 | 规则 | 判定 |
|---|---|---|
| 交集（改前） | 与任一已读区间**有重叠** | 读 1-60、引 50-200 算覆盖 → 61-200 从未读过也放行 |
| **完整包含（已采用）** | 引用区间必须完整落在**某一个**已读区间内 | 读 1-60、引 50-200 → 退回 |
| 端点各自落位 | 起止各自落在某条区间内，允许中间跨缺口 | 会放掉"读 1-60 与 300-434、引 50-350"这种最典型的幻觉形状 |

**为什么"完整包含"不会误伤分页读**：`merge_mark` 的合并条件是 `start <= 上一段末尾 + 1`。
`offset=0,limit=500` → `(1,500)`；`offset=500,limit=500` → `(501,1000)`；`501 <= 501` → 合并成 `(1,1000)`，
引用 `1-1000` 完整包含成立。推导已在单测里钉住（`tests/test_read_ledger.py`）。

**残余误差（诚实说明）**：证据表记的是"读过哪几行"，不记"那几行当时是什么内容"。
文件在两次读之间被改写、或路径被 `mv` 之后，行号仍然对得上，但语义已漂移。
要真正闭合这一层，只有**内容比对**（答案里的代码块 vs 磁盘原文，即 CiteGuard 路线）——
这是独立立项，不属于 R20b；本报告把它列为下一步而不是"已解决"。

改动位置：
- [read_ledger.py](file:///workspace/ai-coding/ai_coding/domain/read_ledger.py) `covers()` 改为 `mark.start <= start and end <= mark.end`。
- 单测 `tests/test_read_ledger.py` 覆盖：分页合并后整段引用、跨缺口引用被退、单行引用、越界引用。

### §1.2 决定 2：档 2 的改善

**问题**：路径只被 `grep`/`glob`/`bash` 见过、没被 `read` 过时，档 2 直接放行，**完全不看行号**——
`grep` 了 100 行的 `a.py`，引 `a.py:999` 也放行。

**为什么不做"预先记录行数"**：`os.stat().st_size` 是**字节数**不是行数；要行数必须读全文，
10MB 文件就是 10MB IO，而且要在每批工具调用后对 `seen_paths`（≤200 条）逐个做。

**已采用：惰性读**——只在档 2 **真的命中**时，读**被引用那一条**路径：

```python
if any(same_file(seen_path, path) for seen_path in seen):
    if size_of is None:
        return []
    total = size_of(path)
    if not total:
        return []
    return [(cited, start, end) for cited, start, end in claims if end > total]
```

- 注入点在 [agent_loop.py](file:///workspace/ai-coding/ai_coding/core/agent_loop.py) 的 `_line_count`（惰性读，失败按 0 处理＝不挑战）。
- 真实数据里档 2 命中率极低（三份答案 0 次），所以常态 **0 次 IO**。

### §1.3 证据

| 判据 | 结果 |
|---|---|
| `tests/test_read_ledger.py` / `test_claim_check.py` | 全绿 |
| `scripts/mutate_r20.py` | **25/25 killed**（含 P1「covers 退回交集」、P4「档 2 不看文件尾」、P4b「档 2 用被引用拼写而不是被看见的拼写」、P5「档 2 查 start 而不是 end」） |
| 真实答案复算（`scripts/eval_claims.py`） | 基线 i2c 1 条未覆盖 / r20 两次 0 条，与交集语义逐条一致 |

---

## §2 `/export html` 会话报告

### §2.1 改了什么

| 之前（R9c） | 现在（R20b） |
|---|---|
| 一张扁平 span 表 | KPI 行 + 每回合一张卡片 + 工具时间线 + 正文 |
| 无颜色体系 | 直接镜像 TUI 的 `theme/tokens.ts`（暗/亮两套） |
| 纯 `<pre>` | 多语言轻量语法高亮（py/ts/js/json/md/bash/yaml/toml/diff） |
| 无主题切换 | `prefers-color-scheme` + 复选框强制切换，**零 JavaScript** |
| index 页把整份文档塞进 `<body>` | index 用 fragment 渲染，是**一个合法文档** |

安全边界没有放松：工具输出是攻击者可控的（`echo '<script>…'`），所以
**先分词、再逐 token `html.escape`、最后包 span**——顺序反了会把实体切开。
`tests/test_export_highlight.py` 41 条用例里有一条专门扫"任何语言下 span 之外不出现 `<`"。

### §2.2 真机导出的三个真 bug（已修）

1. **KPI 卡片竖着堆**：`.kpis` 的 flex 容器被漏写，六张卡变成六个整宽块。
   现在由一个 `_kpis()` 统一产出（三个页面共用），单测钉住"必须有一个 `.kpis` 父级"。
2. **强制亮色后整页不可读**：覆盖写在 `.page` 上，而 `body` 的 `color` 是在 `body` 层解析的，
   于是继承下来的文字仍是浅色 → 白底白字。改成覆盖 `body` 本身。
3. **index 页嵌套文档**（R9c 遗留）：`<!doctype>` 出现在 `<body>` 里。

### §2.3 视觉验证

用 headless Chromium 实际渲染（不是看代码猜）：

| 截图 | 内容 |
|---|---|
| `20-export-dark.png` | 暗色，KPI 行、回合卡片、绿色状态边、read 工具卡片与输出 |
| `21-export-light.png` | 系统亮色 |
| `22-force-light.png` | 系统暗色 + 点「亮色」→ 整页切亮且文字可读（验证零 JS 切换真的生效） |

---

## §3 TUI 真机评测

### §3.1 方法与可复现

- 环境：tmux 3.4 / bun 1.2.14 / 190×46 / `qwen3.7-flash`（dashscope）。
- 驱动：`tmux send-keys` + `tmux capture-pane -p -e`（保留 SGR）。
- 截图：自写 ANSI→PNG 渲染（FiraCode + 文泉驿，块字符按矩形绘制，避免行间缝隙）。
  之所以不用浏览器截图：TUI 是终端界面，`capture-pane` 是它真实的像素来源。

```bash
tmux new-session -d -s r20b -x 190 -y 46 -c /workspace/eval-workspace
tmux send-keys -t r20b 'cd /workspace/ai-coding && .venv/bin/ai-coding -p config.local.yaml -w /workspace/eval-workspace' C-m
tmux capture-pane -p -e -t r20b > shot.ansi
```

### §3.2 用例与结果

| # | 场景 | 结果 | 证据 |
|---|---|---|---|
| T1 | 冷启动 / Home | ✅ | `01-home.png` |
| T2 | `/` 唤起命令面板（22 条） | ✅ | `02-slash-palette.png` |
| T3 | 主闭环：读文件 + 引用行号 | ✅ 一次通过，无引用退回 | `08-live-turn.png` |
| T4 | `/export html` 写出报告 | ✅ `[export] wrote …html` | `09-export-written.png` |
| T5 | plan 模式（shift+tab） | ✅ 姿态变 `Plan · 只读` | `10-plan-mode.png` |
| T6 | 计划提交 → 审阅卡（1 执行 / 2 继续规划） | ✅ | `10-plan-mode.png` |
| T7 | 批准 → 自动执行（写文件） | ✅ 需确认一次 write | `11-plan-approved-run.png` |
| T8 | 分步验收（bash 两条） | ✅ 每次确认后执行 | `12/13` |
| T9 | 收尾：Todos 2/2 | ✅ | `14-plan-complete.png` |
| T10 | 对话框族（help/status/models/permissions/skills/memory/mcp/sessions/connect） | ✅ 全部打开且可 esc 关闭 | `19/04/05/06/18` |
| T11 | 文本命令（budget/trace/todo/config/tasks/compact/undo/redo/stats/export） | ✅ 均有回执 | 见 §4 |
| T12 | `/new` | ✅ 新会话 id | — |
| T13 | `/review` | ✅ 起回合并调 `git status` | — |
| T14 | `/quit` | ✅ 退出 | — |

### §3.3 发现的问题

**P1（严重，已修）`/permissions` 是死指针。**
面板条目指向 `session.permissions`，而 keymap 里从来没有这个名字。
点它 → 清空输入框、什么都不发生。属于"看起来能用、其实静默失效"最坏的一类。
已改为 `dialog.permissions.show`（app 层注册的那个对话框）。
并新增一条**源码扫描测试**：`SLASH_COMMANDS` 里每个 `command` 必须在 `src/` 里真的有 `name: "…"` 注册。

**P2（严重，已修）4 个 TUI 独有命令被当成普通提问发给模型。**
`/models`、`/sessions`、`/new`、`/mcp` 只存在于前端目录，后端 `_ALIASES` 不认识；
手打这四条 → 走 `session.prompt` → 模型收到一行斜杠命令并开始"回答"。
已加 `ownCommand()`：**输入恰好等于某个带 keymap 命令的条目名**时，本地直接执行该命令，
与从面板里选它完全一致。带参数的（`/permissions auto`）仍交给后端文本分发。

**P3（中，已修）同名双实现。**
`/help`、`/status`、`/skills`、`/memory`、`/connect` 面板走对话框、手打走后端文本 → 两种行为。
P2 的同一处改动让手打也走对话框，两边归一。

**P4（严重，已修）打开任何对话框都会把背景里的中文打成空洞。**
遮罩用的是 OpenCode 的 `RGBA.fromInts(0,0,0,150)`；在本项目的 opentui 上做 alpha 混合时，
**双宽字符被抹掉、单宽字符留下**。实测：中文转录 `[✓] 1. 在 r20b-live/ 下创建 hello.py…`
在对话框打开时变成 `[✓] 1.    r20b-live/        hello.py`，关闭后恢复。
alpha 200 同样复现，255 不复现（因为背景被完全盖住）→ 确认是混合路径。
已改为**不透明 `theme.backgroundBase` 遮罩 + 对话框自身边框**，并给 `DialogSelect` 补了内容高度，
否则空列表会撑成整屏（`15 → 16 → 17 → 18` 四张图是这个过程）。

**P5（轻，已修）help 面板快捷键列贴着滚动条。**
给滚动区加 `paddingRight=1`，快捷键列与滚动条之间留出一列。

**P6（轻，已修）空列表没有任何提示。**
`DialogSelect` 现在在 0 行时显示 `no matches`。

### §3.4 已修清单（代码位置）

| 文件 | 改动 |
|---|---|
| [frontend/src/util/slash.ts](file:///workspace/ai-coding/ai_coding/frontend/src/util/slash.ts) | `/permissions` 指向 `dialog.permissions.show`；新增 `ownCommand()` |
| [frontend/src/component/prompt.tsx](file:///workspace/ai-coding/ai_coding/frontend/src/component/prompt.tsx) | 提交前先判 `ownCommand`，命中则本地执行 keymap 命令 |
| [frontend/src/dialog/context.tsx](file:///workspace/ai-coding/ai_coding/frontend/src/dialog/context.tsx) | 遮罩改不透明 + 对话框边框 |
| [frontend/src/component/dialog-select.tsx](file:///workspace/ai-coding/ai_coding/frontend/src/component/dialog-select.tsx) | 列表按内容给高度；空态 `no matches` |
| [frontend/src/component/dialog-help.tsx](file:///workspace/ai-coding/ai_coding/frontend/src/component/dialog-help.tsx) | 滚动区 `paddingRight` |
| [core/export_html.py](file:///workspace/ai-coding/ai_coding/core/export_html.py) | `_kpis()` 统一 KPI 行；状态色边；主题覆盖改到 `body` |
| `frontend/test/slash.test.ts` | 新增 7 条（死指针扫描、`ownCommand` 五态） |

### §3.5 未修 / 待确认

1. **plan 卡把 Markdown 原样显示**：审阅卡里 `# Create …`、`| # | 步骤 |` 是字面量，
   而转录区的 Markdown 表格是渲染过的。同一份内容两种呈现，读起来重复且廉价。
   → 需要给审阅卡接一个 Markdown 渲染器（或让后端只下发已解析的步骤表）。
2. **执行期姿态后缀短暂不一致**：`/plan run` 后后端已"只读已关闭"，输入框仍显示 `Build · 只读`，
   回合结束才自愈。属前端状态刷新时机问题。
3. **`update_goal` 误用**：计划执行末尾模型调用 `update_goal`，后端回 `no_goal`，
   TUI 如实标红并给 `[medium]` 诊断。不是 TUI bug，是工具选择/提示词问题。
4. **计划计数口径**：`[plan] 完成 1 步 · 用了 1 回合 / 32528 tokens（含 1 步分步外完成…）已完成：1, 2`
   与同屏 `Todos 2/2` 并列时读起来矛盾。需要统一"分步验收"与"目标步骤"的计数。
5. **中文换行边界偶发 `�`**：长中文行在右边缘出现一个替换字符（截图 `08-live-turn.png` 行内可见）。
   只在 tmux 抓取里观察到一次，未确认是 opentui 折行还是 tmux 网格占位，需单独复现。

---

## §4 slash 命令可用性矩阵

**面板（22 条，全部实测可用）**

| 命令 | 行为 | 状态 |
|---|---|---|
| help / status / models / connect / mcp / sessions / skills / memory / permissions | 打开对应对话框 | ✅ |
| new | 新会话 | ✅ |
| quit | 退出 | ✅ |
| review | 起 review 回合 | ✅ |
| budget / trace / todo / config / tasks / stats | 文本回执 | ✅ |
| compact | `[compact] messages n → m` | ✅ |
| undo / redo | 有回执（空会话给"没有可撤销的回合"） | ✅ |
| export | `[export] wrote …` | ✅ |

**后端独有 7 条**（`clear / model / resume / plan / agents / goal / readonly`）：
不在面板里是**设计**（`plan`/`readonly` 由 19 §Q1=A 明确不广告，模式行负责入口）。
手打仍可用——它们走 `session.command`，后端 `_ALIASES` 认识。**不修**。

**曾断掉、现已修好的 4 条**：`models / sessions / new / mcp`（P2）。

---

## §5 怎么重跑这套评测

```bash
# 1) 后端
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check ai_coding tests scripts
.venv/bin/python -m mypy ai_coding
.venv/bin/python scripts/mutate_r20.py          # 期望 "all 24 mutations killed"
.venv/bin/python scripts/eval_claims.py         # 真实答案覆盖率复算

# 2) 前端
cd ai_coding/frontend && bun test && bun run typecheck

# 3) TUI 真机（tmux 驱动）
bash /tmp/shots/retake.sh                        # 需要先起 r20b 会话，见 §3.1

# 4) 导出报告 + 网页截图
#    TUI 内输入 /export html，然后：
/tmp/pw/bin/python /tmp/shots/webshot.py <导出的 html> out.png dark 1280
```

---

## §6 评审（自查）

| 风险 | 判断 |
|---|---|
| "完整包含"会不会太严，把正常回答也退回？ | 真实答案 3 份 0 新增误退；且每回合最多退 1 次 + 歧义保护（≥2 个文件跳过）。可接受。 |
| 惰性读会不会被大文件拖慢？ | 只在档 2 命中时读一条；真实数据 0 次命中。最坏是单文件一次读。 |
| 遮罩改不透明，用户会不会觉得"看不见上下文了"？ | 会。这是为绕开 opentui 的双宽字符 alpha bug 做的取舍；上游修好后可以恢复半透明。已在代码注释里写明原因。 |
| `ownCommand` 会不会把本该给模型的文本吞掉？ | 只匹配**恰好等于** `/<面板条目名>` 的行；带空格参数、路径、普通句子都不匹配（单测 5 态钉住）。 |
| 前端改对话框外观，会不会影响渲染测试？ | `bun test` 505 条全过；`tsc` 干净。 |
| 报告页会不会执行工具输出里的脚本？ | 不会：零 `<script>`、零外链，所有动态串先 escape 再包 span。 |

### §6.1 独立评审的发现与处置

一次独立子智能体评审（只读、逐文件核对）报出 8 条，全部处置：

| # | 发现 | 级别 | 处置 |
|---|---|---|---|
| 1 | `covers()` 对**倒置区间**（`a.py:100-1`）判为"已覆盖"——比旧交集规则更弱 | minor | `extract_claims` 里把 `start > end` 交换过来（两处：inline 与普通形态），并加单测 |
| 2 | 档 2 把**被引用的拼写**交给 `size_of`，答案缩写路径（`llm_helpers.py` vs `ai_coding/ai/llm_helpers.py`）时查不到 → 静默 fail-open | minor | 改为解析**被看见的那条拼写**再测行数；加单测断言 `size_of` 收到的就是 seen 路径 |
| 3 | `_line_count` 无上限读全文 | minor | 加 `_LINE_COUNT_MAX_BYTES = 4MB`，超限返回 0（未知即不挑战） |
| 4 | `size_of` 异常未兜底 | nit | `unsupported_claims` 内 `try/except (OSError, ValueError)` → 0 |
| 5 | `_kpis()` 的 `value` 未 escape（当前调用点都是数字，属潜在洞） | nit | 补 `escape(value)` |
| 6 | `_render_issues` 双重转义（`&amp;lt;`） | nit | 只对拼接后的整条转义一次 |
| 7 | `RGBA` 成为死导入 | nit | 删除 |
| 8 | 两个主题复选框可同时勾选，靠源码顺序决胜 | nit | 改成显式互斥：`body:has(#force-light:checked):not(:has(#force-dark:checked))` |

评审确认无 blocker/major，且核对了报告里的数字（41 条高亮用例、22 条面板条目、505 条前端测试）与截图文件是否真的存在。
