# r22 方案：`[tools]` 告警失效 + 图片占位符原子删除

> **状态**：待拍板。两项独立，可分开做。逐项 TDD（RIPR），每项单独提交。
> **基线**：`294f435`。**基线绿**：pytest 2611 passed / 6 skipped，mypy 132 干净，前端 290 passed，tsc 干净。

---

## 0. 结论摘要

| # | 问题 | 根因 | 定性 |
|---|---|---|---|
| A | `[tools] 13304 字符（阈值 12000）` 每次开新会话都出现 | 阈值 12000 是**本地工具只有 4,280 字符的年代**定的；r17 之后本地工具涨到 **12,760**，**自己就超了**，与 MCP 无关 | 阈值失效，非图片引起 |
| B | 图片占位符删除是逐字符的，会留 `[Imag` 残渣 | 我用真实文本当占位符，backspace 逐字删 | 需要原子删除 |

**两项的共同点：都不是我最初以为的原因。** A 与图片无关（是"第一回合"），B 不能靠 opentui 的 extmarks（见 §2 实测）。

---

## 1. A：`[tools]` 告警为什么"一输入图片就出现"

### 1.1 取证（生产路径实测）

`_warn_tools_chars_once()`（`core/agent_loop.py:789`）在**每个回合开始时**调用（`:754`），`self._tools_warned` 是**每 loop 一个**标志（`:316`）→ **每次启动应用，第一回合必报一次**。

你的第一次动作就是粘图，所以看起来像图片引起的。**把图片去掉，它照样出现。**

实测工具面构成（`build_world` 真实装配）：

```
工具面总计: 13304 字符   (阈值 12000)
  local        12760  █████████████████████████
  mcp            527  █
```

最贵的几个：`memory_search 2059` · `memory_write 1552` · `todo_write 1486` · `subAgent 1327`。

**阈值 12000 < 本地工具 12760** → 条件恒真。

### 1.2 现有设计与契约

| 项 | 内容 |
|---|---|
| 事件 | `ToolSurfaceWarning(tools_chars, threshold, origins, hint)`（`core/events.py:306`） |
| 触发 | `_warn_tools_chars_once`，`tools_chars > tools_chars_warn` 且未报过 |
| 配置 | `tools_chars_warn: int = 12_000`（`config/models.py:90`），0 关闭 |
| 设计出处 | R7e-3（`docs/r7e-analysis.md`）。原话：**"measured: 4,280 chars without MCP, 13,301 with a 13-tool server"** |
| 提示语 | `"工具 schema 每一步都会重发；最贵的是 'X'。用 mcp.servers[].tools 只注册需要的工具，或关掉用不到的 server。"` |

**关键**：设计意图是**抓 MCP 膨胀**（提示语只谈 MCP），阈值是按"MCP 之前 4,280"定的。r17 加了 grep/webfetch/websearch/skill 等工具后，本地基线翻了三倍，**告警变成了恒真**。

### 1.3 三个候选修法（都要你拍板）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A1 抬高阈值** | `12_000` → 例如 `20_000` | 一行改动，语义不变 | 仍是魔数；本地工具再涨会再失效 |
| **A2 只对 MCP 计费** | 新增 `mcp_chars_warn`，只对 MCP 部分告警 | **永远可行动**（本地工具用户改不了）；与提示语一致 | 要改事件字段（`tools_chars` 的含义） |
| **A3 移出转录区** | 不再作为 transcript notice，改走日志/状态栏 | 转录区干净 | 成本可见性下降，违背 R7e-3 初衷 |

**我的推荐是 A2**：本地工具是**用户改不了的固定成本**，对它告警没有行动价值；MCP 才是用户能配置的。A1 只是把问题推迟。

### 1.4 A2 的执行范围（若选 A2）

| 文件 | 改动 |
|---|---|
| `config/models.py` | `tools_chars_warn: int = 12_000` → `mcp_chars_warn: int = 2_000`（名字与语义一起改，不留旧名） |
| `core/agent_loop.py` | `_warn_tools_chars_once` 判据改成 MCP 部分；`_tools_by_origin` 已有，直接取 `mcp` 项 |
| `core/events.py` | `ToolSurfaceWarning` 增加 `mcp_chars`（或把 `tools_chars` 改为 MCP 值）——**需你定**：是保留总量供诊断，还是只留 MCP |
| `server/bridge.py` | 事件载荷随之调整 |
| `frontend/src/state/session.ts:234` | 文案改成只讲 MCP |
| `docs/data-contracts.md`、`docs/how-to-run.md` | 配置表同步 |

**不引入**新事件类型、新阈值常量（复用 `_tools_by_origin`）。

### 1.5 A 的 RIPR

- **Red** `test_only_mcp_bloat_warns`：本地 12,760 + MCP 0 → **不告警**；本地 12,760 + MCP 5,000 → 告警
- **Red** `test_the_local_tool_surface_alone_never_warns`：直接用 `build_world` 的真实装配断言不发事件
- **Pass** 既有 R7e-3 用例（若有）按新语义更新

### 1.6 A 的效果

| 场景 | 改前 | 改后 |
|---|---|---|
| 默认装配（本地 12.7k，MCP 0.5k） | 每次启动都报 | **安静** |
| 挂了一个 13 工具 MCP server | 报（混在噪音里） | 报，且指出是哪个 server |

---

## 2. B：图片占位符的原子删除

### 2.1 你要的"像 OC 一样"，我调研到的**两个否定结论**

**① opentui 有现成的原子区间机制（extmarks），但对中文是坏的。**

`TextareaRenderable.extmarks` 暴露了 `ExtmarksController`，它**自己包装**了光标移动与删除：光标落在 virtual extmark 内会原子跳过，backspace 落在区间末端会**整段删除**（`deleteExtmarkById` + `deleteRange`）。

我按生产 API 写了原型实测：

```
纯 ASCII   : "hi [Image 1] ok"  → "hi  ok"        ✅
含中文     : "看看 [Image 1] 对吗" → "看 1] 对吗"    ❌ 删错了范围
token 在头 : "[Image 1] 后面"    → " 后面"          ✅
```

**ASCII 对、中文错。** 这个库自己也标着：

> WARNING: This is simulating extmarks in the edit buffer and will move to a real native implementation in the future. **Use with caution.**

**结论：不用 extmarks。** 你的场景全是中文，用它等于引入一个数据损坏 bug。

**② 手动拦截是可行且中文安全的。**

同一个原型，纯函数 + `setText` + `cursorOffset` 往返：

```
✅ "看看 [Image 1] 对吗" → "看看  对吗" (光标 3)
✅ "hi [Image 1] ok"    → "hi  ok"    (光标 3)
✅ "[Image 1] 开头"      → " 开头"      (光标 0)
```

### 2.2 复用到的既有契约（都已实测可达）

| 契约 | 位置 | 用途 |
|---|---|---|
| `input.plainText` / `input.cursorOffset` | `EditBufferRenderable` | 读文本与光标（**JS 字符串下标，中文安全**） |
| `input.setText(text)` / `input.cursorOffset = n` | 同上 | 写回 |
| `input.deleteCharBackward()` / `deleteChar()` | 同上 | **回退到默认删除**（非 token 的情况） |
| `input.editorView.hasSelection()` | 同上（**public readonly**） | 有选区时让路给默认行为 |
| `useBindings({enabled, commands, bindings})` | `@opentui/keymap/solid` | 拦截键位（r21 已用过同一模式） |
| `extractImages(raw, map)` | `util/attachment.ts` | 提交时解析（**已实现，本次不动**） |

### 2.3 契约：一个纯函数

```ts
/** 光标前若是完整 token，整段摘掉；否则原样返回。 */
export function dropTokenBeforeCursor(
  text: string,
  offset: number,
  tokens: ReadonlySet<string>,
): { text: string; offset: number; dropped: boolean }
```

**它存在的作用**（说不出来就砍掉）：把"光标前是不是一个完整占位符"这个判断从 UI 里拿出来，使它可被测试覆盖——CJK 下标、光标在词中、多个 token 相邻这些边界，全都不用起终端就能钉住。

### 2.4 分步实现

**第 1 步** `util/attachment.ts`：加 `dropTokenBeforeCursor`（纯函数，先写测试）。

**第 2 步** `component/prompt.tsx`：加一个拦截层。

```tsx
useBindings(() => ({
  // 只在文本里真的有 token 时才接管，普通输入不受影响
  enabled: hasToken(),
  target: () => input,
  commands: [{
    name: "prompt.backspace",
    run: () => {
      // 有选区 → 交给默认（删选区），不要吃掉用户的选区
      if (input?.editorView?.hasSelection?.()) { input.deleteCharBackward(); return }
      const dropped = dropTokenBeforeCursor(
        input?.plainText ?? "", input?.cursorOffset ?? 0, new Set(Object.keys(imagesByToken())),
      )
      if (dropped.dropped) {
        input?.setText(dropped.text)
        input!.cursorOffset = dropped.offset
        return
      }
      input?.deleteCharBackward()   // 默认行为，逐字删
    },
  }],
  bindings: [{ key: "backspace", cmd: "prompt.backspace" }],
}))
```

**第 3 步**：同样的拦截给**前向删除**（`delete` 键）——光标在 token **开头**时向后删也要整段摘掉。用同一个纯函数加一个 `dropTokenAfterCursor`（或给现有函数加一个方向参数——**倾向后者，避免两个近乎相同的函数**）。

**第 4 步**：`enabled: hasToken()` 的响应式来源。文本不是 signal，所以要么用 textarea 的 `onInput` 把文本记进 signal，要么**让这一层常开、全部在命令里判断**（推荐后者：回退路径就是 `deleteCharBackward()`，等价于默认）。

### 2.5 数据流影响

```
粘贴 → insertText("[Image N]") → imagesByToken 记下 token→dataUrl
                                        │
Backspace ──► 拦截层 ──► 光标前是完整 token？
                 │                          │
               是 │                       否 │
                 ▼                          ▼
        setText(摘掉 token)         deleteCharBackward()（默认）
        cursorOffset 前移
                 │
                 ▼
        文本里没有该 token → extractImages 自然不再产出它（图片被删）

提交 → extractImages(plainText, imagesByToken) → { 干净文本, 图片[] }
```

**不变式**：图片的生命周期**只**由文本里的 token 决定。拦截层只负责让"一次按键 = 一个 token"，不改变"文本即真相"这条既有契约。

### 2.6 B 的 RIPR

- **Red** `dropTokenBeforeCursor` 纯函数：
  - 中文前缀 `"看看 [Image 1] 对吗"`、光标在 token 尾 → 摘掉、光标 3
  - 光标在 token **中间** → 不摘（交默认逐字删）
  - 光标前不是 token（普通文字）→ `dropped=false`
  - 两个 token 相邻、光标在第二个尾 → 只摘第二个
  - token 在文本最前、光标 0 → 不摘
- **Red（集成）** 用 opentui 测试渲染器：`setText` 中文 + token → 模拟 backspace → 断言文本与光标（**中文是重点，extmarks 就是死在这**）
- **Pass** 既有 `extractImages` 5 条、paste 相关用例

### 2.7 B 的效果

| 场景 | 改前 | 改后 |
|---|---|---|
| `看看 [Image 1] 对吗`，光标在 `]` 后，按一次 backspace | `看看 [Image 1 对吗`（残渣 + 图片丢） | `看看  对吗`，图片丢 |
| 光标在词中按 backspace | 逐字删 | **逐字删（不变）** |
| 有选区按 backspace | 删选区 | **删选区（不变）** |
| 中文文本 | 正常 | **正常（extmarks 会坏，手动不会）** |

### 2.8 终态设想

粘贴一张图 → 输入框出现 `[Image 1]` → 在它后面打字 `看看这个 [Image 1] 对吗` → 按一次 backspace，`[Image 1]` **整体消失**、光标停在 `看看这个 ` 之后 → 提交后模型收到纯文本 `看看这个 对吗` 且无图。

---

## 3. 跨项架构图

```
                用户
                  │  Ctrl+V / 终端粘贴
                  ▼
   ┌────────────────────────────────────────┐
   │ prompt.tsx                             │
   │  粘贴路径（r21，不动）                   │
   │    ├─ onPaste 带 image mime → 直接用字节 │
   │    ├─ onPaste 空字节 → 读宿主剪贴板      │
   │    └─ ctrl+v 键 → 读宿主剪贴板           │
   │  插入：[Image N] 进文本 + 记 imagesByToken│
   │                                        │
   │  ← 新增：backspace/delete 拦截层         │  §2
   │     完整 token → 整段摘；否则默认删除      │
   └────────────────┬───────────────────────┘
                    │ 提交
                    ▼
   ┌────────────────────────────────────────┐
   │ extractImages(text, imagesByToken)     │  ← 已实现，不动
   │   文本即真相：token 在→图片在            │
   └────────────────┬───────────────────────┘
                    ▼
              session.prompt → 后端

   ┌────────────────────────────────────────┐
   │ agent_loop._warn_tools_chars_once      │  §1
   │   改前：tools_chars > 12000（恒真）      │
   │   改后：只看 MCP 部分                    │
   └────────────────────────────────────────┘
```

---

## 4. 拍板结果与落地

| 决策 | 选择 | 提交 |
|---|---|---|
| A 修法 | **只对 MCP 计费**（`mcp_chars_warn`，默认 2000） | `efac5de` |
| A 事件字段 | **两个都留**：`tools_chars`（总量）+ `mcp_chars`（判据） | `efac5de` |
| B 键位范围 | **backspace + delete 两个方向**（不做光标跳过） | 本提交 |

**实测验证（tmux + 真实 Windows 剪贴板）**

```
① 粘贴        → [Image 1]
② 打字        → [Image 1]看看这个
③ 退一格      → [Image 1]看看这        （逐字删，正常）
④ 再退到 token → [Image 1]
⑤ 再退一格    → （空）                 ← 整个 token 一次删掉，无残渣
⑥ 光标移到开头 + delete → AB           ← 前向删除同样整段摘
⑦ 光标移进 token 内 + backspace → [Imag 3]AB  ← 逐字删（按方案，不做光标跳过）
⑧ 提交 → 模型收到图片（试图用 PIL 查颜色，弹权限框）
```

**已知取舍（⑦）**：光标**停在 token 中间**时逐字删，会留下 `[Imag` 这类残渣。正常使用（在 token 后按退格）不会遇到——这是"不做光标跳过"的直接后果，你已选择不覆盖它。

**回退等价性已实测**：`deleteCharBackward()`/`deleteChar()` 与默认按键在中文、ASCII、混合文本上**逐字节相同**，所以拦截键位对普通编辑零影响。

## 5. 终态自检清单

- [ ] 默认装配启动应用，转录区**不再**出现 `[tools]`
- [ ] 挂一个大 MCP server，**仍然**出现且指出是哪个 server
- [ ] 粘贴图片 → `[Image 1]` 在文本里
- [ ] 中文文本中，光标在 `]` 后按一次 backspace → token 整体消失，**中文完好**
- [ ] 光标在词中按 backspace → 逐字删（不变）
- [ ] 有选区按 backspace → 删选区（不变）
- [ ] 提交后模型收到干净文本 + 图片
- [ ] pytest / mypy / ruff / bun / tsc 全绿
