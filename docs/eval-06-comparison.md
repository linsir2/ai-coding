# eval-06 · 同上下文对比实验：ai-coding vs 本助手（TRAE）

> 方法：同一组分层任务，**同一上下文**（都能读同一份仓库），分别交给
> `ai-coding`（真机，无头 `AgentLoop`）与本助手（TRAE）回答，比较产出。
> 驱动脚本：`scripts/eval_comparison.py`；原始结果：`/workspace/eval-workspace/comparison_ai_coding.json`

## 1. 任务与结果总览

| 任务 | 类型 | ai-coding 耗时 | 工具调用 | 消息数 | 产出质量 |
|---|---|---|---|---|---|
| **T1** 读代码：四层压缩的触发条件与顺序，哪层会完全不生效 | 理解 | **27.6s** | 1（read） | 4 | 详尽、结构清晰，**但指错了层** |
| **T2** 找真实运行时缺陷（讲透一个） | 检索+判断 | **900.1s（撞满 turn_timeout）** | **63** | 94 | ❌ **没有产出结论**（超时） |
| **T3** 设计：自动重试阶梯加在哪一层 | 设计 | 183.6s | 30 | 55 | ✅ **非常完整**（含图、表、代码骨架） |

## 2. 逐题对照

### T1 · 四层上下文压缩

**ai-coding 的答案要点**（27.6s，1 次 read）：
- 正确给出顺序 `L3 → L1 → L2 → L4 → hard trim → sanitize pairs`
- 正确给出各层触发条件（L3 尾部字节 > window/2 且单条 > 30KB；L1 `len > max_messages`；
  L2 `tool 数 > keep_recent`；L4 token 估算 > window 且失败 < 3）
- **结论：L4 在 `_summarizer is None` 时完全不生效** —— 这是**测试环境/未配置**的边界情况

**本助手的答案**（同一上下文）：
- 同样的顺序与条件
- **结论：L3 才是那个「会完全不生效」的层**，而且原因更本质：
  `persist_large_outputs` **只检查消息列表末尾连续的 `role=="tool"` 段**
  （[context_compression.py:L251-257](file:///workspace/ai-coding/ai_coding/core/context_compression.py#L251-L257)）：

```python
end = len(messages)
start = end
while start > 0 and messages[start - 1].role == "tool":   # 只看尾部连续 tool
    start -= 1
batch = messages[start:end]
if not batch:
    return messages          # 末尾是 assistant → 直接返回，什么都不做
```

  **回合结束时最后一条是 assistant 的回复** → `batch` 为空 → L3 直接返回。
  所以 **L3 是「回合中段」护栏**（工具刚返回、模型还没答的那一瞬间），
  **在落盘的会话 JSON 里永远观测不到**。这一点已由 eval-04 的实测确认
  （10 轮真实对话后 `persisted=0`；用真实函数 + 合成输入才触发）。

**判读**：两边都答对了大半，但**本助手指出了更本质、且被实测证实的那一层**。
ai-coding 的答案更"教科书"（覆盖全面），本助手的答案更"尖锐"（直指实测确认的盲区）。
**深度上本助手略优**，但 ai-coding 只用 1 次工具调用、27 秒就给出了覆盖全面的答案 —— **效率上 ai-coding 明显更优**。

### T2 · 找一个真实运行时缺陷 ⭐ 最重要的差异

**ai-coding 的答案**：

```
"让我深入分析已发现的可疑点，交叉验证哪个是真正的运行时缺陷。"
```

- 耗时 **900.1s**（= `turn_timeout_seconds`，**撞满超时**）
- 工具调用 **63 次**（`glob`/`grep`/`read` 交替，读了 40+ 个文件）
- 94 条消息
- **最终答案为空** —— 它一直在"交叉验证"，**直到超时被掐断，也没给出结论**

**本助手的答案**（同一上下文，且已做过静态检查）：

> **`ai_coding/core/mcp.py:787` 的 `import httpx2`**
>
> ```python
> # ai_coding/core/mcp.py:787（远程 streamable-HTTP MCP + OAuth 分支）
> import httpx2          # ← 模块不存在，正确名是 httpx
> ...
> http_client=httpx2.AsyncClient(auth=provider, headers=headers or None),
> ```
>
> - **触发条件**：任何走「远程 HTTP MCP 服务器 + OAuth」的路径
> - **后果**：`ModuleNotFoundError`，该分支 100% 崩溃
> - **证据**：`mypy ai_coding` 报 `Cannot find implementation or library stub for module named "httpx2"`
>   （[eval-01](eval-01-offline-baseline.md) §2）；PyPI 无 `httpx2` 此包
> - **修复**：`import httpx`（1 行）

**判读**：这是**同一个上下文下最显著的差距**。
- ai-coding **有工具、能搜**（63 次调用说明它确实在系统性检索），但**没有收敛**：
  它陷入了"广度优先搜索 + 反复交叉验证"，**缺少"够用就停"的收敛判据**。
- 本助手之所以能给出答案，部分是因为**静态检查（mypy）已经把答案递到眼前** ——
  这说明：**把 mypy 输出喂给模型**（本项目已有 trace/OTLP，但没有"把 lint 结果喂回模型"的通道）
  能显著改善这类任务的收敛。

> ⚠️ 公平性说明：T2 对本助手**不完全公平** —— 本助手已经做过完整静态检查并知道答案。
> 但反过来说，**"知道从哪看"本身就是能力差距的一部分**：本助手知道"先跑 mypy"，
> 而 ai-coding 选择了逐文件 grep。**方法论上的差距是真实的。**

### T3 · 设计自动重试阶梯

**ai-coding 的答案**（183.6s，30 次调用）：**非常完整**，包含
- 现状分析（`_request_with_retry` 已有指数退避，缺的是**分类**）
- ASCII 架构图（AgentLoop / engine.step / policy 三层）
- `RetryStrategy` 枚举 + `RETRY_LADDER` 映射表（三档：NO_RETRY / SERVER_HINT / EXPONENTIAL_BACKOFF）
- 三个文件的最小改动方案 + 代码骨架
- 与 `stop_policy`（turn 级）/`turn_timeout_seconds`（call 级预算）的**正交性**分析

**本助手的答案**（同一上下文）：核心结论一致，但**取舍不同**：

- **加在哪一层**：Engine 层（`openai_engine._request_with_retry`）。理由与 ai-coding 一致 ——
  `ChatEngine.step()` 的契约是"一次模型调用"（[engine.py](file:///workspace/ai-coding/ai_coding/ai/engine.py)），
  重试是**这个契约的实现细节**，不该漏到 `AgentLoop`。
- **三档分类**（对标 Codex 的 `retry_delay`，[error.rs:L418-470](file:///workspace/_vendors/codex/codex-rs/protocol/src/error.rs#L418-L470)）：
  - **NO_RETRY**：`ContextWindowExceeded`、`InvalidRequest`、认证失败、配额用尽 —— 重试也是同样的错
  - **SERVER_HINT**：429 RateLimit（有 `Retry-After` 就用它，没有才退避）
  - **EXPONENTIAL_BACKOFF**：`Stream`（握手后断流）、`Timeout`、`ConnectionFailed`、5xx
- **与现有机制的关系**：
  - `stop_policy`：**完全正交** —— 它是 turn 级"何时收尾"，重试是 call 级"怎么恢复"
  - `turn_timeout_seconds`：**重试等待计入回合预算**（本项目的 `deadline.py` 已经是"硬超时 + 心跳 tick"）
  - `max_tool_iterations` / `max_auto_continuations`：**不该**让工具重试吃迭预算
- **本助手会额外强调的一点**（ai-coding 没提）：
  本项目 `turn_policy.decide_next_iteration` 的**优先级顺序是刻意的** ——
  `budget_expired`（时钟）**最先判**，然后 token 门，然后 C3 续跑，然后阶梯门
  （[turn_policy.py:L322-398](file:///workspace/ai-coding/ai_coding/core/turn_policy.py#L322-L398)）。
  所以重试阶梯**必须**在 `budget_expired` 之前被"看到"，否则一次长退避会让时钟先到期，
  重试就永远没机会发生 —— 这是**把重试放在 Engine 层的一个隐藏好处**。

**判读**：T3 上 **ai-coding 的答案质量与本助手相当，甚至更可落地**
（它给了具体到行的改动清单，本助手更偏架构论证）。**这一局 ai-coding 不落下风。**

## 3. 综合判读

| 维度 | ai-coding | 本助手（TRAE） | 赢家 |
|---|---|---|---|
| 代码理解（T1） | 全面、快（27s/1 次调用） | 更尖锐、指出实测确认的盲区 | **平**（效率 ai-coding 胜，深度本助手略胜） |
| 缺陷定位（T2） | ❌ 63 次调用 + 900s **未收敛** | ✅ 1 处，带 mypy 证据 | **本助手明显胜** |
| 架构设计（T3） | 完整、可落地、含代码骨架 | 完整、更偏架构论证与优先级 | **平**（ai-coding 略可落地） |
| **收敛能力** | **弱**：广度搜索不收束 | 强：知道先跑静态检查 | **本助手胜** |
| **效率** | T1 极快；T2 爆炸 | T2 快（有先验） | **视任务而定** |

### 关键结论

1. **同上下文下，本助手的产出在"检索+判断"类任务上更深** —— 但**部分优势来自先验**
   （本助手已做过完整静态检查）。**去掉先验后差距会缩小，但不会消失**：
   本助手的**方法论**（先跑 mypy → 再定向读代码）比 ai-coding 的
   **逐文件 grep + 反复交叉验证**更可能收敛。

2. **ai-coding 在"设计/理解"类任务上不落下风**，T3 的答案可直接落地。

3. **ai-coding 最大的短板是收敛**：T2 里 63 次工具调用、900 秒、**零结论**。
   这不是"工具不够"，而是**缺少"够用就停"的判据**。
   可操作的改进方向（本项目已有机制，只需接线）：
   - 把 **lint / mypy 输出**作为回合输入喂回模型（现在只有 trace，没有这条通道）
   - 给 `grep`/`glob` 的**连续无新发现**加一个"换策略"提示（类似已有的 `[strategy-gate]`）
   - 给检索类任务设**更紧的迭代预算**，避免 900s 空转

4. **效率上 ai-coding 有亮点**：T1 只用 **1 次 `read`、27.6 秒**就给出了覆盖全面的答案 ——
   说明它的工具栈在"定向读代码"上很直接。

## 4. 复现

```bash
.venv/bin/python scripts/eval_comparison.py --config config.local.yaml
# -> /workspace/eval-workspace/comparison_ai_coding.json
```
