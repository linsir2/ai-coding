# 编排层对标进度

> 这份文件是里程碑的唯一真相源。之前的对比报告只存在于对话中，`docs/orchestration-explore/` 是个空目录——已重建。

## 已完成

| 里程碑 | 内容 | commit | 测试 |
|---|---|---|---|
| — | 审批 UI：edit 统一 diff、`Always allow`（会话内 per-tool，bash 排除）、按钮宽度修复 | `7c009a4` | — |
| — | 记忆畸形输出修复：`_from_lines` 补守卫，删冗余 parser | `cb7ff9d` | — |
| — | trace scoping：子 agent 事件不再决定父方回合收尾、ToolChunk 按 scope 丢弃 | `34fce7a` `6302624` `b9da977` `72e38c7` | — |
| — | memory 维护可观测性：`MemoryHarvested.evicted`、`MemoryConsolidated`、删 `memory_catalog` 死参数 | `3da4840` 内 | — |
| **M1** | provider 重试可观测：`FailureKind` 五分类、`retry_delay` 退避、`on_retry` 钩子、`ProviderRetried` 事件 | `3da4840` | 已全绿 |
| **M2** | 模型降级链：`FallbackEngine` 组合多引擎、`FallbackWorthwhile`、`fallback_models` 配置、启动时解析链 + 环检测、`ProviderFallback` 事件 | `3da4840` | 已全绿 |
| — | `/model` 死代码修复：`AgentLoop(models=...)`，模型表从 `AIConfig` 移到显式参数 | `aa72115` | 已全绿 |

**当前门禁**：`2014 passed, 2 skipped` / ruff clean / mypy 115 files clean。

**已知偶发**（非回归，均为无同步点的时序测试）：`test_pty_textual.py::test_approval_and_ask_user_reach_the_tools`、`test_ui_visual.py::test_collapsible_has_no_top_border`。

## 差距来源

`docs/orchestration-explore/2026-10-07-gap-report.md`（1357 行，2026-10-07 重建）。参考对象：OpenCode（TS）、GenericAgent、Hermes（同语言）、gstack/openclaw（0 源文件，仅 markdown 规范）。

## 待办

**正式里程碑清单见 `milestones.md`**（含依赖关系、验收口径、明确不做项及理由）。
本文件只保留进度摘要。

### P0 —— 已完成

- [x] **子 agent 的 guard 链是空的。** 根因：`core/subagent.py:452-461` 未传 `guards`，`core/dispatch.py:163` 把 `None` 变成 `GuardChain([])`（恒 ALLOW）。修法：`_make_guards` 从 `cli.py` 搬到 `core/guard.py::build_guards`（三处调用点共用：REPL、TUI、子 agent），`SubAgentRunner.__init__` 建一次并传给每个子循环。真实路径已验证 `cat /etc/shadow` 在执行前被拦、拒绝理由回喂给模型。

## G5-6 复查（2026-10-08，有证据）

用全仓真实 trace 统计：**172 个 model step 里 provider 因上下文超长拒绝 0 次**；
`Degraded` 全是别的原因（8 次回合超时、3 次 delta 对账）。估算偏差仍在
（`real/est` 中位 **1.349**，与改动前的 1.356 一致）。

→ **自设预算（48k）确实吸收了估算偏差**，反应式压缩在真实使用中从未触发。
修估算会压缩更早、降低 prompt cache 命中率，而问题没有发生。**G5-6 继续暂缓。**

注意："0 次"是"没遇到这种情况"，不是"恢复路径坏了"——G5-1 的真实路径验证过它能触发。

## TUI 真机运行发现（2026-10-08）

### 已修
1. **斜杠命令提交后面板不关**：键入首个 `/` 打开命令面板，后续字符落进主输入框，Enter 由主输入框
   消费 → 命令跑了、面板还盖着 transcript。修法：提交时用面板自己的 `dismiss`（`pop_screen`
   会和 app 关停竞态）。
2. **状态栏能把 command worker 打死**：`StatusBar._refresh` 每次按 id 查询子节点，而它从 worker
   里被调用；屏幕切换时状态栏短暂 detached → `NoMatches` → worker 死。可达路径：面板开着时
   点击输入框再回车。修法：`compose` 里持有两个 `Plain`，刷新不再查询。

### 已改（设计）
3. 状态栏从**顶部**移到**输入框上方**（对齐 opencode 的 footer menu：agent/model/status 都在输入区旁）。
   测试按 region 断言顺序，而不是 compose 调用顺序。
4. 折叠标题带信息（参考 opencode 的 thinking 折叠）：
   `▶ 思考过程 · The user wants to read...`、`▶ 工具 2 个 · 查找文件 · 读取文件`。

### 已修（根因：日志写进了 TUI 的帧）
5. **输入框中间行右侧残留 2 个字符**（观测到 `a"` / `e"`）。
   - 测试里**不复现**（`run_test` 是 headless 驱动，compositor 里那 2 格是空格）
   - 用 `tmux pipe-pane` 抓 pty 原始字节流，在里面搜到：
     ```
     row=39 col=109  后续可打印='2026-10-08 09:57:20,015 IN'
     ```
   - **根因**：`setup_logging()` 把 `StreamHandler(sys.stderr)` 装在 root logger 上，
     而 TUI 的 stderr **就是 Textual 正在渲染的终端**。一条 152 字符的 httpx 日志被写在
     帧中间的任意位置，Textual 之后只重绘它认为脏的区域 → 留下 2 格碎片（只有 resize 能清）。
   - **为什么 httpx 的 INFO 没被滤掉**：原有过滤只压了 `logging.getLogger("httpx")`，
     而这条日志的 logger 名字是 **`httpx2`**（`%(name)s` 字段直接证据）——是另一个顶层 logger，
     不受影响。
   - **修法**：`send_logs_to_file(workspace)` 在 TUI 接管终端前把 root 日志改到
     `<workspace>/.ai-coding/ai-coding.log`（RotatingFileHandler，2MB×3，可写失败降级为丢弃），
     并把 `httpx2` 加入压制名单。CLI/REPL 路径保持 stderr（面向行，日志只是交错不破坏画面）。
   - **验证**：真机重跑，多回合，`超 108 列的行: 无`。

### 真机观察（非缺陷）
- 长对话（4+ 回合、多工具、审批 y/n、排队命令）累积正常：折叠汇总、transcript 滚动、
  `● Working`/`● Ready`、`[queue] 回合结束后执行` 都对。
- **bash 每命令都要审批**（G4 刻意保留 bash 不可授权）。一个探索型回合就弹了 2 次；
  模型有时用 bash 做 glob/read 能做的事。这是刻意的安全/效率权衡，不是 bug。
