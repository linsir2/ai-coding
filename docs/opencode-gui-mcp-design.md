# M1-MCP 详细设计：把 MCP 连接状态从启动日志搬到屏幕上

> 状态：**待拍板**（未写实现代码）
> 前置：已 merge 到 main（`76f0a4d`）
> 对标：OpenCode `packages/tui/src/component/dialog-mcp.tsx` + palette `mcp.list`

## 0. 范围与三个判断

**做**：后端加 `mcp.list`，前端加只读面板 + palette 一条命令。
**不做**（理由在 §6）：逐 server 重连/启用按钮、`needs_auth` 态、slash `/mcps`、sidebar 常驻。

三个已定的设计判断：

| # | 判断 | 理由 |
|---|---|---|
| ① | **三态** `connected` / `failed` / `filtered` | OpenCode 四态里 `needs_auth` 我们没有 OAuth 型 server（全 stdio 子进程），造它就是死代码；`disabled` 我们只读全局开关（见下） |
| ② | `filtered` **单独一态**，配 warn 色 | R7d 白名单省了 80% token，显示 "connected" 会误导；但过滤不是失败，所以不是 error |
| ③ | **配置声明过但没连上的 server 必须在列表里** | 现在是 `open_all` 里 `continue` 掉、**连 owner 都不建**，命令写错的 server 会从面板消失——那比显示失败更糟 |

## 1. 现状核准（事实，带行号）

| 位置 | 事实 |
|---|---|
| `core/mcp.py:265-307` | `open_all()`：`_config.enabled` 为假直接返回；逐个 server 四道 skip（`not server.enabled`/无名/无命令/重名）都在 `:274-282`；随后建 owner、`start()`、`wait_ready(timeout)`；超时则 `report_degraded` 后 `continue` |
| `core/mcp.py:53-64` | `_MCPSessionOwner`：`server`(config)、`ready`(Event)、`tools`(list)、`session`、`error` |
| `core/mcp.py:69-89` | `_serve()`：连接成功 → `tools` 填充 + `ready.set()`；异常 → `error = exc` + `ready.set()`——**所以 ready 之后必须再看 error** |
| `core/mcp.py:91-105` | `wait_ready(timeout)`：`False` 当且仅当超时或 `error is not None` |
| `core/mcp.py:109-115` | `aclose()` 把 `_owners` 清空——**所以 `servers()` 只能在运行期存活**，关闭后返回空。这是可接受的（关闭时 UI 已退出） |
| `server/world.py:94-96,119` | manager 构造、工具注册、实例挂 `world.mcp` |
| `server/handlers.py:436-450` | `make_handlers` 返回 14 个方法的字典 |
| `config/models.py:277-295` | `MCPServerConfig`：`name/command/enabled/args/env/cwd/tools/auto_approve` |
| `test_mcp.py:22-84` | 现成夹具：`_Session`、`_tool`、`_factory({name: session})`、`_cfg(*servers)`、`_server(name, command, **kw)`、hanging factory——**全部复用，不新造** |
| `test_server_ipc.py:_fake_world` | 现成 world 夹具（`SimpleNamespace`，无 `mcp` 字段——T5 要补） |
| OpenCode `dialog-mcp.tsx:20-36` | 四态文案 `Connected ✓` / `Failed !` / `Sign in required →` / `Disabled ○`，`pending` 显示 `Connecting …` |

### 纠正上一版的一处错误

我在 M1-MCP 设计初稿 §5 写「`MCPServerConfig.enabled` 定义了却没接线」——**这是我说错的**。`mcp.py:274` 第一个条件就是 `if not server.enabled`：它接得好好的。因此**不存在这个缺口**，§6 的"不做"里相应删掉这一条。

## 2. 新契约

### 2.1 后端 `MCPManager.servers()`

```python
def servers(self) -> list[dict[str, Any]]:
    """Every configured server and what became of it (m1-mcp §2).

    ``_config.servers`` is what the user asked for, ``_owners`` is what
    happened; both are needed, because a server that never reached an owner
    (skip at open_all:274) has an outcome of "not connected" that must still
    be visible rather than vanishing from the list.
    """
```

返回行（dict，照 `skills_list` 的裸 dict 风格）：

```python
{
  "name": str,          # 服务器名；无名 server 用空串，detail 说明
  "command": str,       # "npx -y @foo/bar"，拼接 command + args
  "status": str,        # "connected" | "failed" | "filtered" | "off"
  "toolCount": int,     # 实际注册进 registry 的工具数
  "wantedCount": int,   # 白名单要求的数量（0 = 不过滤）
  "detail": str,        # 失败原因 / 被过滤掉的工具名 / 空
}
```

**为什么是四种 status 而不是三种**：`_config.enabled` 为假时 `open_all` 直接返回、`_owners` 为空，此时若返回空列表，用户看到"没有 MCP"而实际上配了 3 个 server——**"没开"和"没配"必须能区分**。所以全局关闭 → 每行 `off`。

| status | 判据 | 文案 |
|---|---|---|
| `off` | `not self._config.enabled` | `未启用` |
| `connected` | owner 存在、`ready.is_set()`、`error is None`、`tools` 非空 | `Connected ✓` |
| `filtered` | 同上，但 `wanted` 非空且 `len(tools) < len(wanted)` | `部分工具已过滤` |
| `failed` | owner 存在但（未 ready 或 `error`），**或** owner 不存在（被 skip） | `Failed !` |

`detail` 的内容按 status：
- `off`：空
- `connected`：空（或用 `auto_approve` 提示，见 §6-3）
- `filtered`：被丢掉的工具名（已排序，最多 8 个 + `…共 N 个`）
- `failed`：`str(owner.error)` 或 `未连接（命令不存在/启动失败/超时）`

### 2.2 后端 handler `mcp.list`

```python
def mcp_list(params: dict[str, Any]) -> dict[str, Any]:
    """Rows for the MCP dialog: configured servers and what became of them."""
    manager = getattr(world, "mcp", None)
    if manager is None:
        return {"servers": []}
    return {"servers": manager.servers()}
```

照 `skills_list`（`handlers.py:419`）的形状：`getattr` 兜底 + None 给空列表。

### 2.3 前端 `util/mcp.ts`（新文件）

```ts
export type McpStatus = "off" | "connected" | "filtered" | "failed"
export interface McpServer {
  name: string
  command: string
  status: McpStatus
  toolCount: number
  wantedCount: number
  detail: string
}

/** Row label: OpenCode's four words, ours for four states. */
export function mcpStatusLabel(status: McpStatus): string
/** Which colour the row reads in. */
export function mcpStatusTone(status: McpStatus): "muted" | "success" | "warn" | "error"
/** `3 / 13 tools` when a whitelist is in play, `3 tools` otherwise. */
export function mcpToolNote(server: McpServer): string
```

**为什么新文件**：`util/toolmeta.ts` 是工具卡的映射域（工具名→动词、错误码→文案）；MCP 是另一域。`util/permissions-dialog.ts`、`util/slash.ts` 都是「一域一个纯函数文件」的既有惯例，跟它走。

### 2.4 前端 `component/dialog-mcp.tsx`（新文件）

props：`{ client: IpcClient }`。

- `onMount`：`client.request("mcp.list")` → `setServers(data.servers ?? [])`
- 选项：`title=name`、`description=mcpToolNote(server)`、`footer=server.detail`、`category` 按 status 分组（`已连接` / `有问题`）
- `current`：无（没有"当前 server"概念）
- ~~整个面板**只读，无逐行动作**~~ **M3 起被推翻并记录在此**：该结论的前提「启动时一次性连接，没有重连机制」被外部 MCP 的 M3 消除——现在每个 server 有永久 backoff 重连的 supervisor，`mcp.connect` 唤醒一次立即重试。半自动面板的前提成立了，failed 行的 enter 就是那个动作（且仅 failed 行：connected 无可重试、disabled 不可连、connecting 正在试）。原判据（没有背后机制的按钮是假实现）依然有效，变的是机制是否存在。
- ~~**不做 slash `/mcps`**~~ **M5 起被推翻并记录在此**：面板回答「连了谁的 server、什么状态」，正是 slash 目录的用途。现在 `/mcp` 打开同一面板，且面板多一个 `a` 动作打开引导式连接向导。理由：操作者要从 slash 发现 MCP，且「连接」被要求是引导式（原先「不做运行时增删」的前提是没有任何 UI 要加 server；现在有）。**删/改仍不做**：删除没有出现过的需求，且误删代价高。：该结论的前提「启动时一次性连接，没有重连机制」被外部 MCP 的 M3 消除——现在每个 server 有永久 backoff 重连的 supervisor，`mcp.connect` 唤醒一次立即重试。半自动面板的前提成立了，failed 行的 enter 就是那个动作（且仅 failed 行：connected 无可重试、disabled 不可连、connecting 正在试）。原判据（没有背后机制的按钮是假实现）依然有效，变的是机制是否存在。
- footer：`esc close`

为什么用 `DialogSelect`：它已提供标题+esc、fuzzy 搜索、分组、footer 行。MCP 面板是一个纯只读列表，与 `dialog-skills.tsx` 同构但更简单。

### 2.5 `app.tsx`：palette 一条

```tsx
{
  name: "mcp.list",
  title: "MCP servers",
  group: "Agent",              // 对位 OpenCode app.tsx:895 的 category
  desc: "Show configured MCP servers and what came of them",
  palette: true,
  run: () => dialog.open(() => <DialogMcp client={props.client} />, "large"),
}
```

## 3. 逐文件实施（每步一次验证）

| 步 | 内容 | 先写 | 验证 |
|---|---|---|---|
| 1 | `MCPManager.servers()` | 4 条 pytest（T1-T4） | `pytest tests/test_mcp.py` |
| 2 | handler `mcp.list` | 1 条 IPC（T5） | `pytest tests/test_server_ipc.py` |
| 3 | `util/mcp.ts` | 5 条 bun（T6） | `bun test test/mcp.test.ts` |
| 4 | `dialog-mcp.tsx` | 2 条渲染（T7） | `bun test test/dialog-mcp-render.test.tsx` |
| 5 | `app.tsx` 命令 | tsc | 无单测（palette 命令无既有单测，E2E 覆盖） |
| 6 | E2E | — | tmux：起桩 server + `mcp.enabled: true` |

顺序理由：域方法 → 传输 → 纯函数 → 视图 → 接线 → 真机。与 A1–A8 每轮的顺序一致（先里后外，每步都能单独红绿）。

## 4. TDD 用例（RIPR）

| # | 用例 | R 可达 | I 交互 | P 预测 | R 显明 | 层 |
|---|---|---|---|---|---|---|
| T1 | 连上的 server 报告 connected + 工具数 | `MCPManager` + `_factory` 直造 | `open_all()` → `servers()` | `status=="connected"`、`toolCount==len(tools)` | 整行 dict 可比 | pytest |
| T2 | 连不上的（hanging）→ failed 且有 owner | hanging factory + `connection_timeout=0.05` | 同上 | `status=="failed"`，`detail` 含超时或错误 | — | pytest |
| T3 | **配了但被 skip 的仍在列表** | `_cfg(_server("dead"))` 而 factory 不给 session（owner 建了但 `_serve` 抛错） | 同上 | 行存在、`status=="failed"` | 列出 name | pytest |
| T3b | **无名/无命令的 server 也占一行** | `_cfg(_server(name=""), _server(command=""))`，不开 `open_all` | 直接 `servers()` | 两行都在，detail 说明原因 | — | pytest |
| T4 | 白名单过滤 → filtered + 丢名进 detail | `_server("fs", tools=["a"])`，session 给 3 个工具 | 同上 | `status=="filtered"`、`toolCount==1`、`wantedCount==1`、`detail` 含被丢的 2 个名 | — | pytest |
| T4b | 白名单全中 → connected（不是 filtered） | `tools=["a","b"]`，给 2 个 | 同上 | `connected` | 防止误报过滤 | pytest |
| T5 | `mcp.list` 走 IPC 返回同形状 | `test_server_ipc.py` 的 `ipc` fixture + `_fake_world` 加 `mcp` | `client.request("mcp.list")` | `servers` 列表，字段齐 | JSON 直接看 | pytest |
| T5b | `world.mcp` 缺失（老夹具）→ 空列表不炸 | 现有 `_fake_world` 不改（它没有 mcp 字段） | 直接发请求 | `{"servers": []}` | — | pytest |
| T6 | 四态文案/配色/工具计数 | 纯函数 | 直调 | 逐条字面量 | — | bun |
| T7 | 面板渲染：标题、`N / M tools`、失败原因进 footer | `testRender` | 渲染 `DialogMcp` | `captureCharFrame()` | 帧可比 | bun |
| T8 | 真机：桩 server + 开关打开 → 面板一行 connected | — | tmux | 屏上行 | — | E2E |

**三个生死线**：T3（写错的 server 不能消失）、T4b（白名单全中不算 filtered）、T5b（没有 mcp 的世界不能炸）。

## 5. 数据流

```
config.mcp
  ├─ enabled: false ──────────────► servers() 每行 status="off"（不返回空列表）
  └─ servers[] ──► open_all() ──► _MCPSessionOwner{ready, tools, error, session}
                                        │
                    被 skip 的（无名/无命令/重名）└─► 无 owner ─► 从 config 补行，status="failed"
                                        │
                  servers()（新只读方法）◄┘
                                        │
                  handlers.mcp_list ──► IPC ──► app.tsx palette
                                        │
                       util/mcp.ts ◄── dialog-mcp.tsx（DialogSelect 只读）
```

**wire 只加一个方法**；域对象加一个只读方法；`open_all`/`aclose`/连接逻辑**零改动**。

## 6. 明确不做

| 不做 | 理由 |
|---|---|
| 逐 server 的**重连/断开/启用**按钮 | 我们的 MCP 是启动时一次性连接，没有重连机制。OpenCode 有，**但那要连着改连接生命周期**，不是加个按钮。要么单开一轮做重连，要么在 detail 里写"重启后生效" |
| `needs_auth` 态 | 无 OAuth 型 server；死代码 |
| slash `/mcps` | ~~OpenCode `dialog-mcp.tsx` 有 `slash:{name:"mcps"}`；我们的 slash 目录已砍掉全部同义入口~~ **M5 起推翻**：加 `/mcp`（见上），因为操作者要求 slash 能发现并引导连接 |
| sidebar 常驻 MCP 状态 | OpenCode 有 `feature-plugins/sidebar/mcp.tsx`；我们的 sidebar 固定四项，且 MCP 是启动态、不需要常看 |
| 显示 `auto_approve` | 配置里有（`models.py:294`），但这会把"要不要审批"和"连没连上"混在一起。要显示就单开一项，本轮不混 |

## 7. 架构图

```
                          ┌──────────────────────────────────────┐
config.yaml               │  MCPManager（core/mcp.py）            │
  mcp:                    │                                      │
    enabled: false  ─────►│  servers()  ← 唯一新增，只读          │
    servers:              │    ├─ config.servers（要求）           │
      - name: fs          │    └─ _owners[]（结果）                │
        command: mcp-fs   │         ready / tools / error         │
        tools: [a, b]  ───┴──────────────┬───────────────────────┘
                                         │ dict 行
                          handlers.mcp_list（server/handlers.py）
                                         │ IPC
                          dialog-mcp.tsx（component/）
                            ← util/mcp.ts 文案/配色
                            ← DialogSelect 现成列表
                          ← palette「MCP servers」
```

## 8. 实施后自查

| 现象 | 结果 |
|---|---|
| 配 2 个 server 都连上 | 面板 2 行 `Connected ✓` + `2 tools` |
| 一个命令写错 | 1 行 `Failed !` + 原因，**不消失** |
| 白名单只要 1/3 | `部分工具已过滤` + `1 / 3 tools` + 被丢的 2 个名 |
| `mcp.enabled: false` | 每行 `未启用`，**不是空列表** |
| 后端 | 只加 1 个只读方法 + 1 个 handler，连接逻辑零改动 |
| import/导出 | `servers()` 被 handler 与测试消费（真实双消费者，非"只被测试消费"） |

## 9. 待你拍板

| # | 事项 | 我的建议 |
|---|---|---|
| ① | 四态（含 `off`）而非三态 | **四态**：`enabled:false` 时返回空列表会让"没开"和"没配"无法区分 |
| ② | 无名/无命令的 server 也占一行 | **占**：配置写错却看不到，用户会以为没配 |
| ③ | `filtered` 用 warn 色 | **warn**：省 token 是主动选择，不是故障 |
| ④ | 先只做查看，重连单开一轮 | **先只读**：我们的连接是一次性的，加按钮没有后端可调 |