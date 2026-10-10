# Spec: 外部 MCP 支持（external-mcp）

**Source intent:** docs/intent/2026-10-10-external-mcp.md
**Status:** approved → implementing（M1 开工）
**上游依据：** OpenCode v2.0.22 调研 + 本项目调研，逐条对齐见 §11

---

## 1. Overview

外部 MCP 四件事：契约对齐 v2、注入改 catalog+dispatcher、生命周期并发+重连+半自动
面板、传输补 remote(Streamable HTTP)+OAuth。分 M1–M4 交付，M1 契约先行（不改
行为，只改形状），M4 引入新信任边界（单列 ADR）。

**分期偏差说明（相对批准的设计）**：`type/url/headers/oauth` 字段不在 M1 落。
理由：M1 只连 local，落 remote 字段就是新造一个 `auto_discover`（全仓无消费点
的死配置，M1 正要删它）。remote 契约与 M4 传输实现同时落地，一次有效。

## 2. Technical Architecture

### 2.1 Components

| Component | Responsibility | 依赖 |
|---|---|---|
| `MCPServerConfig`/`MCPTimeout`（config/models.py） | v2 契约：type 判别（M4）、三段 timeout、disabled | pydantic |
| `MCPManager`（core/mcp.py） | 并发启动、per-server 重连、servers() 五态行、login/logout（M4） | infra/deadline、mcp SDK |
| `session_factory` 分派 | type=local → stdio_client；type=remote → streamable_http_client（M4） | mcp SDK 2.2.0 |
| `MCPToolAdapter` | **不再是注册表工具**；退化为 dispatcher 的执行内核 + catalog 数据源 | permission gate |
| `mcp_call` 工具（新） | 唯一进 LLM 的 MCP 工具：server/tool/input | gate(叶子 assert) |
| OAuth provider（M4） | httpx.Auth 子类 + TokenStorage(0600 文件) + callback server | mcp.client.auth |
| `mcp.status.changed` 事件 | 状态广播，TUI 面板订阅 | wire 事件 |
| IPC：`mcp.list/mcp.connect/mcp.login/mcp.logout` | 读 + 手触发 | server/handlers.py |

### 2.2 Data Flow

```
config.local.yaml ──► MCPConfig ──► MCPManager
   local: command[] ──► StdioServerParameters(command[0], command[1:])
   remote(M4): url/headers/oauth ──► http_client(auth=OAuthProvider) ──► streamable_http_client
                                        ▲
                                  401 ──┴──► needs_auth ──► mcp.login ──► 浏览器+DCR+PKCE ──► TokenStorage ──► 重连

每个 server 一个 _MCPSessionOwner 任务：
  Semaphore(4) 门下 start ──► 【startup timeout】connect+initialize ──► 【catalog timeout】list_tools
      ├─ 成功 → tools 存 owner，ready
      └─ 失败(普通) → failed + Degraded + backoff 重试循环
      └─ 失败(401) → needs_auth（M4）+ 重试循环（等 login）

catalog：每步从 owners 的 tools 重建 ──► agent_loop volatile tail ──► LLM
LLM ──► mcp_call(server,tool,input) ──► gate.assert(server__tool) ──► owner.session.call_tool
```

关键不变量：**catalog 是每步活数据**（agent_loop.py:253 的 `_tool_schemas` 是
loop 级静态，绝不能把 catalog 冻进去；重连后工具集变化必须自动可见——
挂 :221 已有的 volatile tail 机制）。

## 3. Data Design

### 3.1 Entities

```python
class MCPTimeout(BaseModel):
    startup: int = 60_000      # R7d：npx 冷启动实测 ~27s，v1 的 5s 不够
    catalog: int = 30_000      # list_tools 预算
    execution: int | None = None   # None = 跟随 turn deadline（现状，不收紧）

class MCPServerConfig(BaseModel):        # M1 落地（local-only）
    name: str = ""
    command: list[str] = []              # argv 一体，OpenCode v2 形状
    cwd: str = ""
    environment: dict[str, str] = {}     # 原 env
    disabled: bool = False               # 原 enabled（极性反转）
    timeout: MCPTimeout = MCPTimeout()
    tools: list[str] = []                # 保留：catalog 预算裁剪器
    auto_approve: bool = False           # 保留：per-server 免审批

# M4 追加：type: Literal["local","remote"]；url/headers/oauth（remote-only）

class MCPConfig(BaseModel):
    enabled: bool = False        # 总闸（保留）
    servers: list[MCPServerConfig] = Field(default_factory=list)
    # auto_discover 删除（死字段）
```

### 3.2 Status（servers() 行）

`connected / failed / needs_auth(M4) / filtered / off` 五态。行契约不变：
`{name, command, status, toolCount, wantedCount, detail}`；
M4 追加 `action`（needs_auth 行可 login，failed 行可 connect）。

### 3.3 Storage

- OAuth token（M4）：`~/.local/share/ai-coding/mcp-auth.json`，0600。**不进
  config.yaml、不进 trace、不进日志**（红线）。
- 配置本身：仍是启动期一次性加载（不做运行时增删）。

## 4. API / Interface Design

### 4.1 IPC

```
mcp.list    {} → {servers: [行]}                     # 已存在，M3 加 action 字段
mcp.connect {server} → {ok} | {error}                # M3：触发一次性重连
mcp.login   {server} → {ok, authUrl} | {error}       # M4：OAuth 交互式登录
mcp.logout  {server} → {ok}                          # M4：删 token + 断连
```

### 4.2 Wire 事件

`mcp.status.changed {server, status, detail}`——M3 起，TUI 面板订阅即刷新
（对齐 OpenCode schema/mcp-event.ts:22 的事件驱动，不轮询）。

### 4.3 Tools（M2）

```
mcp_call(server: str, tool: str, input: object) → ToolResult
  错误不外抛：未知 server/tool、未连接（含 "Reconnect it from the panel"）、
  参数不符 —— 全部作为 ToolResult(success=False) 回喂模型。
  权限：execute 叶子按 server__tool 走 gate（目录可见 ≠ 执行授权）。
```

### 4.4 Error Handling

沿用现有降级链：连接失败 `Degraded(component="mcp_server")` + 跳过 + 重试；
调用失败 `mcp_error` ToolResult；401 `needs_auth`。无一条路径让 MCP 异常冒泡
到 turn（data-contracts.md:126 不变量）。

## 5. Technology Stack

| Layer | Choice | Justification |
|---|---|---|
| MCP SDK | `mcp` 2.2.0（已装） | stdio + streamable_http + auth(OAuthClientProvider/TokenStorage/DCR/PKCE 元语)全有；`streamable_http_client` 无 authProvider 参数 → httpx 注入（已验实签名） |
| OAuth callback | stdlib `asyncio` 一次性监听 127.0.0.1 | 后端是 Unix socket JSON-RPC（server/service.py:1）无 HTTP 面，不引 web 框架 |
| 并发 | asyncio.Semaphore(4) + per-server task | 现架构已是纯 asyncio；不需要 anyio 之外的新原语 |
| 重试 | 自有 backoff（指数+jitter） | 无第三方重试库依赖；run_with_deadline 复用现成 |

## 6. Constraints

- 破坏性配置迁移：见 §3.1，spec 批准即执行，spec 里写迁移说明（非发布产品，
  config.local.yaml 不进仓库，成本=自改配置+测试适配）。
- 测试适配≠改弱测试：M1 契约变更导致的 fixture 更新必须逐条保持原断言意图，
  在提交信息里列出每个改动。

## 7. Out of Scope

见 intent。补两条执行口径：不删 `tests/test_mcp_stdio.py` 真连门控；
不引入 `codemode`/`protocol` 配置（不抄 Code Mode，SDK 默认版本协商）。

## 8. Acceptance Criteria（RIPR）

| # | 用例 | 判定 |
|---|---|---|
| A1 | `command: ["python","-m","x"]` 拆成 args 正确传给 StdioServerParameters | 单测 mock 参数 |
| A2 | startup 超时不波及 catalog 计时 | 假时钟/假 session：connect 快但 list_tools 挂 → 到 catalog deadline 才 failed |
| A3 | execution 超时：session.call_tool 挂 → execution ms 后 ToolResult(mcp_error) | 假 session sleep + 断言 wall clock |
| A4 | 4 个装死 server 总墙钟 ≤ 70s（现状 ~240s） | 集成测试（假 factory） |
| A5 | backoff：失败后 1s→2s→4s…cap 60s，恢复即停 | 假时钟 |
| A6 | remote 桩直连，工具进 catalog 且可调通 | mock httpx |
| A7 | 401 → needs_auth → 假 AS 全流程（DCR+PKCE+callback）→ connected | mock http + browser open 打桩，零真实浏览器 |
| A8 | token 文件 0600；请求/事件/日志无 Authorization | 静态断言 + grep 测试 |
| A9 | 幻觉工具名 → ToolResult 报错含合法名 | 单测 |
| A10 | deny `stub__write_file` 后：catalog 仍列、调用被 gate 拒 | 单测 |
| A11 | catalog 40 工具 ≤ 2000 token 等价（8000 字符） | 单测 |
| A12 | 状态真机走通：起 stdio 桩 + remote 桩，TUI 面板显示、断线重连、login | 手动 E2E（老规矩 tmux） |

## 9. Risks & Mitigations

| Risk | L/I | Mitigation |
|---|---|---|
| OAuth 元数据发现 API 面与调研不符（SDK 2.2.0 具体签名） | M/H | M4 开工第一件事：读 `mcp.client.auth.oauth2` 源码定面，再写测试 |
| 破坏性配置迁移影响现存 config.local.yaml | H/L | 同 commit 内改 config.example.yaml + 三步迁移说明 |
| catalog 进 volatile tail 破坏 prompt 缓存（agent_loop.py:221 注释提过缓存） | M/M | 先读缓存键构成；若是前缀缓存则 catalog 放 system 段尾部，不插中间 |
| mcp SDK 2.x 大版本跨 1.19 API 面变化 | M/M | 门控真连测试 `test_mcp_stdio.py` 必跑（AI_CODING_MCP_STDIO=1） |
| 40 工具 catalog 让模型选错工具（幻觉名） | M/M | A9 错误信息带合法名清单；预算内优先保留 top 工具描述 |

## 10. Open Questions

- per-tool ruleset 落点（M2 定）：倾向 per-server `permissions` 子表
- volatile tail 具体字段与缓存键交互（M2 开工先读 agent_loop 缓存逻辑）

## 11. 上游依据（对齐溯源）

| 我们的决策 | OpenCode 依据 |
|---|---|
| command[] 一体 / environment / disabled / 三段 timeout | packages/schema/src/mcp.ts:26,53 |
| stdio + Streamable HTTP 两传输 | mcp/stdio.ts:37、client.ts:270 |
| 五态（+needs_auth）与 mcp.status.changed 事件 | schema/mcp.ts:69、mcp-event.ts:22 |
| 目录可见性 ≠ 执行授权（叶子 assert） | tool/mcp.ts:49 + tool/AGENTS.md:54 |
| catalog 预算内联 2000 token | codemode/catalog.ts:48-50 |
| 会话不可用提示文案分流 | mcp/index.ts:65 unavailable() |
| 不做自动重连 → 我们加 backoff（改进 OpenCode 硬伤） | index.ts:472-477 无 reconnect |
| OAuth 故意不 redirect，落 needs_auth | client.ts:149、oauth.ts:135 |
| token 刷新并发去重 | oauth.ts:29 refreshes Map |
| 运行时 add 不落盘（我们不做） | index.ts:635 overrides map |
