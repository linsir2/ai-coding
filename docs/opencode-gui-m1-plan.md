# M1 方案：Home 屏完整化（元素级对齐）✅ 已完成

> 分支：`feat/opencode-gui-replica`（M0.5 之后）
> 基准：运行中的 OpenCode **v1.18.32**（实机 140×42，列号实测）+ 仓库源码 v2.0 对照
> 原则：**每个 UI 元素必须对应一个真实后端能力**；没有的能力宁可不显示，不造假

---

## 验收结果（2026-10-09，列号实测对比）

| 元素 | OpenCode 1.18.32 | 本项目 | 结论 |
|------|------------------|--------|------|
| 底栏行号/列号 | 行 40，`~/` col 2，右缘 col 138 | 行 40，`~/` col 2，右缘 col 138 | ✅ 完全一致 |
| Prompt cap | 行 24，`╹` col 33，宽 108 | 行 25，`╹` col 33，宽 108 | ✅ 列/宽一致；行差 1（见下） |
| "Ask anything" | col 36 | col 36 | ✅ |
| Logo | 行 14-17（4 行） | 行 16-18（3 行，figlet） | ✅ 字体差（见下） |
| 80×24 | — | footer 显示、路径完整、版本右对齐 | ✅ |
| 40×10 | — | logo/footer 隐藏、文本换行 | ✅ |

**1 行偏移说明**（无假元素）：OC 的 logo 画布 4 行（自带 `▄` 顶饰），figlet `ANSI Compact` 3 行；且 OC 的 hints 行（M3）占 1 行。M3 加回 hints 行后，本项目的 block 将落在 20-24，与基准完全对齐（top spacer 12 + height3 + logo 3 + h1 + padTop + block）。

---

## 1. 实机基准（v1.18.32 @ 140×42，列号实测）

```
行 15-17  Logo（块字符，双色；居中）
行 19     空（prompt wrapper paddingTop=1）
行 20-24  Prompt 块（col 33-108，宽 75）
          20: ┃
          21: ┃  Ask anything… "Fix a TODO in the codebase"
          22: ┃
          23: ┃  Build · DeepSeek V4.1 Flash OpenCode Go · max
          24: ╹▀▀▀…（75 格）
行 25     提示行：右对齐到 col 108（prompt 右缘）"tab agents  ctrl+p commands"
行 40     底栏：col 2 起 `~/projects/opencode:v2`；右缘 col 138 止 `1.18.32`
          （paddingLeft/Right=2；行高 <16 时无上下 padding）
```

布局骨架（源码 `routes/home.tsx` + `feature-plugins/home/footer.tsx`）：
```
flexGrow 弹性区
height 3 固定区
Logo（height<12 整体隐藏）
height 1
prompt wrapper（width 100%, maxWidth 75, paddingTop 1）
flexGrow 弹性区
home footer（width 100%；宽度<44 或高度<12 整体隐藏）
```

---

## 2. 元素对照表（核心：每个功能对应好）

| # | OpenCode 元素（基准） | OpenCode 数据来源 | 本项目对应实现 | 本项目数据来源 | M1 动作 |
|---|----------------------|------------------|---------------|---------------|---------|
| 1 | Logo 块字符、双色、居中 | 静态 + 宽高自适应 | figlet `ANSI Compact` 双色（已做） | 静态 | 🔧 补：height<12 隐藏；上下留位对齐（flexGrow + height 3） |
| 2 | Prompt 居中块 maxWidth 75 | 静态 | 已有（width 100%, maxWidth 75） | — | ✅ 保持 |
| 3 | Prompt 左框线 `┃` | agent 色（无 agent 时 border.base） | `┃` + `theme.agentColor` | 固定值 | ✅ 已有 |
| 4 | 底部 cap `╹▀▀▀…`（跨满 75） | 动态内容宽 | 已有（customBorderChars） | — | 🔧 验证：cap 宽度=内容宽（截图核对） |
| 5 | Placeholder `Ask anything… "<随机示例>"` | 静态 3 例轮换 | 已有 3 例（与基准同文案） | 静态 | ✅ 已有 |
| 6 | 元数据行 `Agent · model provider · variant` | agent/模型/variant | `provider · model`（我们无 agent/variant） | hello.model + provider 派生 | 🔧 **去掉假的 "Build"**；provider 从 `base_url` host 派生（如 `api.deepseek.com`→`deepseek`），拿不到就不显示 |
| 7 | 提示行 `tab agents  ctrl+p commands`（右对齐 prompt 右缘） | keymap shortcuts | 仅 `ctrl+p commands`（无 agent 系统）；且 palette 是 M3 | `@opentui/keymap` shortcuts | 🔧 **M1 先隐藏**（对应原则：功能没通不显示）；M3 palette 接通后一行开启（对照表留记录） |
| 8 | 底栏左 `~/dir:branch` | location + `git rev-parse` | `~` 缩写 + `:branch` + 智能截断 | hello.workspace + **新增** `vcs.branch` | 🔧 新增后端 `vcs.py`（git subprocess，失败返回 ""）+ 前端 location 组件（string-width 截断） |
| 9 | 底栏右版本 `1.18.32` | app.version | `0.1.0`（去 `v` 前缀，对齐格式） | hello.version | 🔧 格式对齐 |
| 10 | 底栏显隐阈值（宽<44 或 高<12）| 终端尺寸 | 加同样阈值 | `useTerminalDimensions` | 🔧 新增 |
| 11 | 更新提示 `/update …` | 更新服务 | **不做**：本项目无更新机制 | — | ❌ 记录不做的原因 |
| 12 | Global form overlay（底部表单层）| 表单系统 | 延后 M5（审批面板用） | — | ⏭ 延后 |
| 13 | 主题 dark/light 自动跟随终端 | 终端 OSC 11 查询 | `renderer.waitForThemeMode(1000)` | 库能力（@opentui/core） | 🔧 新增 ThemeProvider（dark/light 双表已有） |
| 14 | 路径智能截断（保 basename）| `truncateFilePath` | 用 `string-width` 实现（CJK 安全） | 库 | 🔧 新增 |
| 15 | 底栏位置可点击移动目录 | `/cd` 等 | **显示但不点击**：本项目 TUI 无换目录功能 | — | ❌ 记录（点击动作延后，无假交互） |

---

## 3. 改动清单（逐文件）

### 后端（Python，2 文件 + 1 测试）

| 文件 | 改动 | 细节 |
|------|------|------|
| `ai_coding/server/vcs.py`（新） | `git_branch(workspace) -> str` | `subprocess.run(["git","rev-parse","--abbrev-ref","HEAD"], cwd, timeout=3)`；非仓库/超时/无 git → `""`；不 raise |
| `ai_coding/server/handlers.py` | `initialize` 返回值扩展 | `+ vcs: {branch}`；`+ provider`（`model_config.base_url` 的 host 首段，取不到为 `""`） |
| `tests/test_server_vcs.py`（新） | git 仓库 / 非 git 目录 / 无 git 三种 | tmp_path + `git init`（子进程）或 monkeypatch |

`initialize` 返回形状（v1 + 增量，向后兼容）：
```json
{
  "version": "0.1.0", "workspace": "/path", "model": "qwen3.8-...",
  "provider": "deepseek",            // 新增
  "vcs": { "branch": "main" },       // 新增；非仓库为 ""
  "sessionId": "...", "models": [...]
}
```

### 前端（TS，6 文件）

| 文件 | 改动 | 细节 |
|------|------|------|
| `src/theme/index.tsx`（新） | ThemeProvider + `useTheme()` | 启动时 `renderer.waitForThemeMode(1000)`；dark/light 切 `tokens.ts` 两表；默认 dark |
| `src/component/location.tsx`（新） | `LocationLabel` 组件 + 纯函数 | `abbreviateHome(dir, home)`（仅前缀、边界 `/`）；`formatLocation(dir, branch)`；`truncatePath(label, width, stringWidth)` 保 basename |
| `src/routes/home.tsx` | 布局对齐基准 | flexGrow 区 + height 3 + Logo（height<12 隐藏）+ h1 + prompt(75) + flexGrow + footer；footer 显隐阈值（宽<44 或 高<12） |
| `src/component/prompt.tsx` | 元数据行 | 去 `Build`；`provider · model`（provider 空则仅 model）；cap 宽度核对 |
| `src/app.tsx` | 传 props | provider/vcs 传给 Home |
| `src/ipc.ts` + `index.tsx` | hello 类型扩展 | `Hello.provider`、`Hello.vcs.branch` |

### TS 纯函数测试（bun test，新 `frontend/test/location.test.ts`）
- `abbreviateHome("/home/u/proj", "/home/u")` → `~/proj`；其他路径不动
- `formatLocation(dir, "main")` → `~/proj:main`；branch 空 → `~/proj`
- `truncatePath`：长路径保尾部 basename；CJK 字符串按显示宽截断

---

## 4. 不做清单（记录原因，防"假复刻"）

| 元素 | 不做的原因 |
|------|-----------|
| `tab agents`（agent 循环） | 本项目无 agent 概念（model/fallback 是另一层）；M3 palette 后只显示 `ctrl+p commands` |
| `Build` 标签 | 无 agent 系统，标签会是假的 |
| `variant :max`（thinking effort） | 后端无 variant 概念 |
| `/update` 更新提示 | 无更新服务 |
| 底栏点击移动目录 | TUI 无换目录功能（workspace 启动即定） |
| MCP 计数（v2 底栏有，v1.18 无） | 我们的 MCP 状态在 hello 已有基础，但 v1.18 基准不显示；M3 `/mcps` 对话框时再加 |

---

## 5. 验收（截图对比）

1. `tmux` 同尺寸（140×42）并排截图：opencode 1.18.32 vs 本项目 Home
2. 逐元素核对第 2 节表格：每个"🔧"项在截图中可见或（❌/⏭ 项）有记录
3. 边界尺寸：80×24（footer 正常）、40×10（footer/logo 隐藏）
4. `pytest`（新 vcs 测试）+ `bun test`（location 纯函数）+ 全量回归 + ruff/mypy/tsc 全绿

---

## 6. 实施顺序（TDD）

1. 后端：`vcs.py` 测试先行 → 实现 → `handlers.initialize` 扩展 → 测试绿
2. 前端纯函数：`location` 测试先行 → 实现 → bun test 绿
3. 主题：ThemeProvider（无测试，截图验证 light/dark）
4. 布局：home.tsx/prompt.tsx 调整 → tmux 截图对比 → 微调列号
5. 收尾：全量检查 + 更新对照表状态 + 提交
