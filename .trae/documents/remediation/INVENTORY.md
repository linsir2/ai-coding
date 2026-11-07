# 遗留问题总账（单一事实源）

基线：`fd5a929`。**42 项问题**（含 9 个子任务，共 51 个可执行修复点）。
状态：`已修` 39、`已决策不修` 2、`DEFERRED` 1。

## 波次结果（2026-11-07）

| 波次 | 批次 | 结果 |
|---|---|---|
| W1 | B1 MCP | 10 项，评审 APPROVE |
| W1 | B2 上下文 + 压缩 | 13 项 + CTX-06 决策，评审 APPROVE（1 轮整改） |
| W1 | B5 子代理 | 4 项，SUB-05 回退为 DEFERRED，评审 APPROVE（1 轮整改） |
| W2 | B3 Skill | 6 项，评审 APPROVE（1 轮整改，含 1 个 P0） |
| W2 | B4 编排 / 计划 | 5 项 + MCP-10a，评审 APPROVE（1 轮整改） |
| W2 | B6 工具 / 会话 / 可观测 | 10 项，评审 APPROVE（1 轮整改） |

波次门禁：全量 pytest exit 0、ruff 全绿、mypy 186 文件 0 错误。

## A. MCP（11 项）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| MCP-01 | stdio 沙箱开关 `mcp.sandbox`（默认关闭；enforce 失败拒绝启动） | P1 | 已修 |
| MCP-02 | close 超时等待上限 | P1 | 已修 |
| MCP-03 | 超时后会话重建 | P1 | 已修 |
| MCP-04 | 远端 output 截断（`clip_head_tail`） | P1 | 已修 |
| MCP-05 | 工具分页 + 不完整声明 | P2 | 已修 |
| MCP-06 | remote 手动重连 + 文案 | P2 | 已修 |
| MCP-07 | 错误码细分（timeout / transport / tool_error） | P2 | 已修 |
| MCP-08 | 重连退避不再秒重置 | P2 | 已修 |
| MCP-09 | OAuth 端口回退 | P2 | 已修 |
| MCP-10 | `mcp_chars_warn` 判定（a→B4）+ 子 agent 无 MCP 说明（b→B5） | P2 | 已修 |
| MCP-11 | 过时测试字段 | P2 | 已修 |

## B. 上下文工程（7 项，含 CTX-07 的 3 个子任务）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| CTX-01 | 引用块预算准入（超预算即停） | P1 | 已修 |
| CTX-02 | 裸路径不再整读（stat 估算） | P1 | 已修 |
| CTX-03 | 显式 @ stat + 前缀读取 | P1 | 已修 |
| CTX-04 | 同一 skill 推荐与 @mention 去重 | P2 | 已修 |
| CTX-05 | 单条记忆写入/渲染上限 | P2 | 已修 |
| CTX-06 | `budget.py` 死指标 | P2 | 已决策不修（仅注释） |
| CTX-07 | 事件口径：a 关闭步估算、b canonical 图像 token、c history_chars 含参数 | P2 | 已修（3/3） |

## C. 压缩遗留（5 项）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| CMP-01 | 重注入进入视图，且不放大、不重复 | P1 | 已修 |
| CMP-02 | 视图 memo（含 session/window/protect/anchor）+ 廉价判定 | P1 | 已修 |
| CMP-03 | protect 随窗口（`max(8k, min(40k, w//3))`） | P1 | 已修 |
| CMP-04 | 信封按真实长度判断 | P2 | 已修 |
| CMP-05 | 冻结计量检测（3 次 + 5% 增长，跨 turn 复位） | P1 | 已修 |

## D. Skill（6 项）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| SKL-01 | route / intervene / check_recovery 三面查 deny | P1 | 已修 |
| SKL-02 | 容器级与嵌套软链拒绝 | P1 | 已修（含 P0 整改） |
| SKL-03 | 注入预算（描述 200 / steps 500 / pattern_dna / @mention） | P1 | 已修 |
| SKL-04 | `<skills>` 单层 + 裸串兼容 | P2 | 已修 |
| SKL-05 | `./path` 本地安装 | P2 | 已修 |
| SKL-06 | Dendritic 健壮性 + phase 加载校验 | P2 | 已修 |

## E. 编排 / 计划（5 项）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| ORC-01 | 计划实施回合无上限 | 决策 | 已决策不修（注释 + 钉扎测试） |
| ORC-02 | `max_strategy_nudges` 生效（默认 3、cap≤L3、0 关闭） | P2 | 已修 |
| ORC-03 | `search_streak_threshold=0` 关闭（保留 stale L3） | P2 | 已修 |
| ORC-04 | 最后一轮不注入（预测下一步；状态按 reason 映射） | P2 | 已修 |
| ORC-05 | FINISH 原因带阶梯后缀（strategy / search） | P2 | 已修 |

## F. 子代理 / 后台（5 项）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| SUB-01 | 产物路径文案（无 branch 时字节不变） | P2 | 已修 |
| SUB-02 | shutdown / idle TTL / stop task 引用 / 并发 close | P2 | 已修 |
| SUB-03 | 同 idempotency key 并发双跑 | P2 | 已修 |
| SUB-04 | `<agents>` 清单注入 | P2 | 已修 |
| SUB-05 | 子代理 gate 固定主 workspace | P2 | **DEFERRED**（回退；已知限制：worktree 内绝对路径项目规则不可见，修复需改 gate API 并迁移 grants） |

## G. 工具 / 会话 / 可观测（3 项，10 个子任务）

| ID | 问题 | 级别 | 状态 |
|---|---|---|---|
| X-01 | 写路径与审批：a shell_read 网络键硬 DENY、b append 原子（大文件 fsync 回退）、c overwrite 中间片段（前缀例外）、d allow_once 一次性 | P2 | 已修（4/4） |
| X-02 | 可观测：a 不再产生 unknown 目录、b tracing.dir 同源（含 `-p`）、c Braid failures 可见 | P2 | 已修（3/3） |
| X-03 | 会话 / 命令：a jobs 保留上限、b mark_* 加锁、c `rewind` 别名（REPL） | P2 | 已修（3/3） |
