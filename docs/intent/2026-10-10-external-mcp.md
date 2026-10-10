# Intent: 外部 MCP 支持（external-mcp）

日期：2026-10-10
状态：已批准（设计对话见本文件附录摘录）

## Problem

当前 MCP 只连本地 stdio server，且一次一个工具一条 function 定义发给模型。
两个问题随外部 server 接入而放大：

1. **外部 server 的工具面不受控**。R7d 实测：13 个工具的 server 让每步 prompt 从
   4,280 → 13,301 字符（3.11×）；`servers[].tools` 白名单砍到 2 个才回落到 5,666
   （docs/features.md:538-541）。白名单是"可用性换 token"的零和交易，工具面越大越疼。
2. **托管型 remote server（HTTP+OAuth）完全不可用**。OpenCode 从 v1 起就有
   `type:remote` + `oauth`（packages/core/src/v1/config/mcp.ts:44-60），我们
   只抄了 local 半边。没有 OAuth 就没有 context7/sentry/GitHub 这类服务。

附带三个已在调研中量化的欠账：权限粒度只到 server（无 per-tool，且远端工具
永远拿不到持久 "always"，grant_key 为 None）；`open_all` 串行连接，N 个装死
server 最坏 N × 60s；连接一次性，断线只能重启 TUI。

## Proposed outcome

- **契约对齐 OpenCode v2**：`command: []` argv 一体、`environment`、`disabled`
  （极性反转）、`timeout{startup,catalog,execution}` 三段、删除死字段
  `auto_discover`（全仓无消费点）。
- **MCP 工具注入改为 catalog + dispatcher**：一个 `mcp_call(server, tool, input)`
  工具 + 每步重建的目录文本（volatile tail，预算 2000 token），所有工具可用，
  每步成本 ≈ 目录而非全量 schema。权限保持叶子逐个 assert
  （server__tool），"目录可见性"与"执行授权"分离（对齐 OpenCode
  tool/mcp.ts:49 的设计）。
- **remote（Streamable HTTP）+ OAuth**：401 → needs_auth → mcp.login → 元数据
  发现 → DCR → PKCE 授权 → 127.0.0.1 一次性 callback → token 落盘 → 重连。
  SDK 用 `mcp` 2.2.0（已验实：`streamable_http_client(url, http_client=...)`
  无 authProvider 参数，auth 走 httpx 注入；`mcp.client.auth` 有
  OAuthClientProvider + TokenStorage 协议）。
- **生命周期**：启动并发（Semaphore(4)）、per-server backoff 重连、
  `mcp.status.changed` 事件 + `mcp.connect` IPC，面板从只读升为半自动。

## Affected users and systems

- 写 `config.local.yaml` 的用户（**破坏性变更**：enabled→disabled、command+args→
  command[]、env→environment、单 timeout→三段）。
- `MCPManager`（core/mcp.py）、`MCPToolAdapter`（不再是注册表里的工具）、
  `AgentLoop`（工具 schema 构成 + 新增 per-step volatile 段）、
  permission gate、IPC handlers、TUI 面板、`world.py` 组装。

## Constraints

- **静默降级不变量不变**：MCP 任何故障（连不上/调用失败/token 过期）都不得向
  会话主链路抛错或污染状态（docs/data-contracts.md:126）。OAuth 交互失败同样
  降级为 needs_auth 状态，不是会话错误。
- token 与授权头属于秘密：不进 config.yaml、不进 trace 事件、不进日志。
- 不引入 JS 沙箱（Code Mode 不抄：任意代码执行的信任边界重画，负 ROI）。
- 不做运行时增删 server、不做 prompts/resources、不做 SSE 传输
  （MCP 规范已弃 SSE，OpenCode v2 亦只要 Streamable HTTP）。

## Out of scope

- 真 Code Mode（execute + JS 运行时）
- `mcp add`（jsonc 就地改配置，OpenCode 有；我们仍是启动期配置）
- per-tool 的 UI（规则写配置，不在面板里逐工具点）
- legacy protocol 版本协商（protocol: legacy/auto/pin，SDK 默认即可）

## Open questions

- catalog 挂 volatile tail 的哪个字段（M2 实现时按 agent_loop.py:221 现有结构定）
- per-tool ruleset 配置落点：config 根级 `permissions` 还是 per-server 子表
  （M2 定，倾向 per-server `permissions: {deny: [...], allow: [...]}`，理由：
  MCP 规则的价值是随 server 分发，根级表把本地工具和远端工具混在一起）
- OAuth 元数据发现用 SDK 哪一层（`mcp.client.auth.oauth2` 具体面，M4 开做时验）
