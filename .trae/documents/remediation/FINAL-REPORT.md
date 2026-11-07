# 多批次缺陷修复 最终报告（2026-11-07）

## 1. 范围与结果

| 项 | 数 |
|---|---|
| 问题条目 | 42 |
| 可执行修复点 | 51 |
| 已修 | 39 条（51 个修复点中 48 个） |
| 已决策不修 | 2（CTX-06、ORC-01） |
| DEFERRED | 1（SUB-05） |
| 批次 | 6（W1：B1/B2/B5；W2：B3/B4/B6） |
| 改动文件 | 生产 + 测试共 80 个 |

每批严格走 **调研方案 → 方案评审 → 修复 → 实现验证 → 代码评审** 五阶段；方案评审 6/6 给出 CHANGES，代码评审 6/6 首轮 REQUEST-CHANGES，整改后全部 APPROVE。

## 2. 逐批结果

| 批次 | 覆盖 | 关键修复 | 评审 |
|---|---|---|---|
| B1 MCP | 11 | 沙箱开关（默认关、enforce 拒绝）、close 等待、超时会话重建、output 截断、分页、错误码、退避、端口回退 | APPROVE |
| B2 上下文+压缩 | 14 | 引用块预算准入、不再整读、重注入进入视图且不放大、视图 memo、protect 随窗口、冻结计量 | APPROVE（1 轮整改） |
| B3 Skill | 6 | deny 三面覆盖、容器级软链拒绝、注入预算、单层 `<skills>`、本地安装、Dendritic/phase | APPROVE（1 轮整改，含 P0） |
| B4 编排 | 5 | nudge 级数上限真实生效、threshold=0 真关、最后一轮不注入、阶梯后缀、告警口径 | APPROVE（1 轮整改） |
| B5 子代理 | 5 | shutdown/TTL/并发 close、同 key 不双跑、`<agents>` 清单、产物文案 | APPROVE（1 轮整改） |
| B6 工具/会话/可观测 | 10 | append 原子、一次性审批、网络键 DENY、trace 无幽灵目录、tracing 同源、jobs 保留、加锁、`rewind` | APPROVE（1 轮整改） |

## 3. 验证证据

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest -q` | exit 0（每波与最终各一次） |
| `.venv/bin/ruff check ai_coding` | All checks passed |
| `.venv/bin/mypy ai_coding` | 186 文件 0 错误 |

定向测试：各批在 04-verification.md 记录了具体文件与用例数（合计新增/修改用例 100+）。

## 4. 已决策不修（附理由）

- **CTX-06**（`budget.py` 死指标）：删除会破坏 `test_turn_reporter` 的既有断言；改为在 docstring 标注"诊断用途、无生产消费者"，保留字段。
- **ORC-01**（计划实施回合无上限）：用户决策"不设上限"，只在 `turn_policy.py` / `agent_loop.py` 注释与钉扎测试中写明边界（仍受窗口、阶梯、模型自停、goal 轮次约束）。

## 5. DEFERRED（附理由与后续路径）

- **SUB-05**（子代理 gate 固定主 workspace）：改成 worktree 会让 `GrantStore` 按 workspace 存盘的 "always" 授权对子代理整体失效，属于用安全问题换可用性。已回退并保留原行为；后续若要修，需给 gate 增加"额外解析根"而**不**改 workspace 键，并迁移 grants。

## 6. 残留风险与已知限制

| 项 | 说明 |
|---|---|
| MCP 沙箱 | 不同平台 argv 差异；bwrap 下 `~/.npm` 可写性与 deny 网络未实机验证；`mcp.sandbox` 默认关闭 |
| MCP 工具发现 | 只做分页（20 页上限），未订阅 `list_changed`；运行中增删工具需重连才可见 |
| 压缩视图 | memo key 覆盖长度/尾 id/anchor/session/window/protect，但"同长度原地改写"仍不命中（append-only 下成立，undo/rewrite 为已知边界） |
| 重注入 | 每请求重读 ≤5 个最近文件；文件集合变化会改变前缀（内容本身稳定） |
| Skill 安装 | symlink 扫描与 `copytree` 之间仍有 TOCTOU 残余（已文档化） |
| 子代理停机 | 20s close 上限下，大仓库可能遗留孤儿 worktree（已提交内容不丢）；worktree 内未提交的项目规则不可见 |
| 命令 | `/rewind` 仅 REPL 别名可用，TUI 命令列表不发布 |
| trace | 会话建立前的事件不落盘 |
| observer | 内层抛 `BaseException` 仍会穿透 |
| 编排 | `max_strategy_nudges=1` 时 L2 不出现、`strict_repeats` 不武装（配置注释已写明） |

## 7. 过程产物

```
.trae/documents/multi-batch-remediation-plan-2026-11-07.md   计划
.trae/documents/remediation/INVENTORY.md                     42 项总账
.trae/documents/remediation/B1-mcp/01..05                    六批各五份
.trae/documents/remediation/B2-context-compression/01..05
.trae/documents/remediation/B3-skill/01..05
.trae/documents/remediation/B4-orchestration/01..05
.trae/documents/remediation/B5-subagent/01..05
.trae/documents/remediation/B6-tools-session/01..05
.trae/work/w1/all.diff, .trae/work/w2/all.diff               回退快照
```

## 8. 提交

按用户决策：全部批次完成、统一评审通过后**一次性提交**，不 push。commit message 按批次分节。
