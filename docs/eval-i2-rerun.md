# I2 真机复跑（eval-08 阶段 E 的 T2 + 阶段 F 前 4 轮）

> 目的：验证 `docs/plan-i2-context-integrity.md` 的修复在真机（dashscope `qwen3.7-flash`，
> 窗口 48k）上是否成立。判据见该方案 §8。
> 环境：`/workspace/ai-coding`，`.venv/bin/python`，配置 = `config.local.yaml` 去掉
> `keep_recent_tool_results` / `snip_keep_tail` 两行（让 D10 的新默认值生效）。
> 日期：2026-10-28。

## 1. T2（同一个题目、同一个模型）

题目（原样）：

> 在 ai_coding/ 下找出一个会导致运行时异常（不是风格问题、不是类型注解问题）的真实缺陷，
> 给出文件路径、行号、触发条件与修复方式。只报一个，但要讲透。

| 判据 | 修前 | 目标 | **本次** | 结论 |
|---|---|---|---|---|
| 摘要调用次数 | 17 | ≤ 6 | **4** | ✅ |
| 摘要 token 占比 | 68.8% | ≤ 20% | **5.2%** | ✅ |
| 最后一次摘要含"截断" | 是 | 否 | **否**（写的是真实进展） | ✅ |
| `tool_calls` | 42 | ≤ 25 | **43** | ❌ |
| 唯一调用率 `unique/total` | 0.93 | ≥ 0.8 | **0.95**（43 次里只有 1 组重复） | ✅ |
| 同一 `(tool, params)` 成功执行次数 | 10 | 1 | **3**（同文件同 offset 的 `read`） | ❌ |
| 必须引用的文件数 | — | ≥ 3 | 多个，且 `path:line` 真实存在 | ✅ |

原始数据：43 次工具调用 / 301.5s / 79 条消息 / checkpoint 覆盖 92 条（本轮结束时）。

**结论：压缩本身的问题解决了，任务层面的"调用次数"没有。**
- 摘要从 17 次降到 4 次，占比从 68.8% 降到 5.2%——"每轮重摘同一段开头"消失了。
- 最后一次摘要写的是"ruff 全绿、`[0]` 索引都有长度校验、转向 mypy 与状态机分析"，
  不再出现"原始对话文本在第 870 行意外截断"。
- 重复调用从 10 组降到 1 组；模型不再反复读 `agent_loop.py`（上一轮 4 次）。
- 但调用总数 43，与修前 42 持平。原因是任务本身是"在 3 万行里找一个 bug"，
  证据一直在增长，模型自然要多读；**这不是上下文冻结，是任务没有收敛判据**。

## 2. 复跑暴露的三个新缺陷（已修）

### D-A · `split_tail` 在 L2 之后取切点，身份全失配（P0）

首压路径原来是 `L2 改写整表 → split_tail(改写后的表) → delta/cut 按身份求差`。
L2 会给每个被 elide 的消息**新建对象**，于是：

- `_delta_by_identity` 一条也排除不掉 → 摘要被喂了整段历史（重复付费）；
- `_cut_index` 匹配不到 → 返回 `len(l4_source)` → **checkpoint 声称覆盖了原样保留的 tail**；
- 下一次请求 `delta = head[used:] = []`、`tail = l4_source[used:] = []` → **请求里只剩一条 note**。

实测：`CompactionEnded.estimated_after` 反复落在 **323 / 916 / 1192 / 1506 / 1737 tokens**，
而窗口是 48k。模型因此把 `agent_loop.py`、`dispatch.py` 各读了 4 次。

修：切点改在 **snip 之后、L2 之前**取（此时还是原对象），L2 只作用于 tail。
无 checkpoint 可装时（摘要失败/无摘要器）才回退到整表 elide。

### D-B · L2 在"要原样保留的 tail"上做 elide（P0）

即使切点对了，L2 仍会把 tail 里较早的工具结果换成 1200 字符的信封。
tail 是模型唯一逐字看到的东西，elide 它等于把刚读完的内容抽走。
修：checkpoint 路径整体跳过 L2；首压路径只对 tail 之外的失败兜底才 elide。

### D-C · `split_tail` 的 user 边界回溯会把 tail 压成 1 条（P1）

旧规则无条件回溯到"最近一条非 synthetic 的 user 消息"。但 **agent 的一个回合只有一条
user 消息（在 index 0）**，于是一路 pop 到只剩 1 条：

```
budget = min(15000, window//4) = 12000 tokens
实际 tail = 1 条消息（323–2,600 tokens）
```

修：只有当回溯后仍保留 ≥ 50% 预算时才回溯（`_TAIL_BOUNDARY_FLOOR`）；
tail 的成本改为计入 `tool_calls[].arguments` 与图片（写文件密集的回合原来"免费"）。

修复后同一份 trace：压缩后的上下文从 318–2,600 变成 **318 → 5,462 → 8,139 → 12,301 →
14,866 → 16,925**（增量累积，到顶再摘要），不再塌回 1–3k。

## 3. 阶段 F（全栈任务，前 4 轮）

| 轮 | 用时 | 驱动判据 |
|---|---|---|
| 1 | 604.7s | `be_pytest=1 fe_build=None` |
| 2 | 253.7s | `be_pytest=1 fe_build=1` |
| 3 | 146.7s | `be_pytest=1 fe_build=1` |
| 4 | 258.4s | `be_pytest=1 fe_build=0` |

**驱动脚本的判据是错的（已修）**：`self_check` 先跑 `pip install -r requirements.txt`，
失败就直接把 `rc` 当成 `be_pytest`。本环境 Python 3.14 上 `pydantic-core` 没有轮子、
源码构建失败（`maturin failed`），于是 **`be_pytest` 永远是 1**，无论测试是否真的通过。
模型每轮都被喂一段 pip 构建报错（而且是从中间截断的，开头是 `ecent call last)`），
却始终看不到自己真正的测试失败。

实测：4 轮结束时 `pytest -q` 已经是 **8 passed**，但驱动仍报 `be_pytest=1`。
换成修好的 `self_check` 复跑同一目录：`be_install=1, be_pytest=0, fe_build=0`。

修（`scripts/eval_fullstack_task.py`）：
1. 依赖安装失败不再阻断 pytest，反馈里单列一行 `[依赖安装失败]`；
2. `_tail_at_line` 按行边界截断，不再出现 `ecent call last)`；
3. `_pytest_digest` 对"没有 FAILED 行"的输入也走行边界截断。

**仍未达标**：4 轮里模型跑 pytest 29 次、`find frontend/src` 12 次、`find -type f` 9 次。
第 1 轮 98 次工具调用（从零建项目），后三轮 37/42/21。这是"修完就重跑验证"的正常形态，
但 `find` 的重复说明每轮开头都在重新摸目录——工作记忆里有 `<working-memory>` 的行摘要，
没有"目录结构"这种结构化事实。

## 4. 结论与遗留

**已证明解决**：压缩不再冻结在会话开头；摘要成本从 68.8% 降到 5.2%；最后一次摘要不再说
"截断"；重复调用从 10 组降到 1 组；压缩后的上下文稳定在 12–17k 而不是 1–3k。

**未解决，且不在压缩层**：
1. **T2 的 43 次调用**。判据 ≤25 在这类开放式任务上可能本身选错了（eval-08 复盘 §4.2 已
   讨论过）。真正的杠杆是"证据增长"与"收敛判据"，不是压缩。
2. **每轮重新摸目录**（`find` 重复）。工作记忆是自然语言行摘要，没有结构化的
   "已知文件清单"。这是一个新立项（`TurnDigest` 加 `files_seen` 并渲染成结构化块）。
3. **模型幻觉**。本次 T2 的答案引用了 `domain/plan_delivery.py:81`（该行确实是
   `"turns": [int(self.turns[0]), int(self.turns[1])]`），但把构造函数说成
   `turns: tuple[int, str] | None = None`、默认 `()` —— 文件里是 dataclass，
   字段是 `turns: tuple[int, int] = (0, 0)`。它只读了第 60–110 行，没读字段声明。
   **这是模型能力问题，不是上下文问题**：它读到的内容没有被截断，是它自己编了前提。
   §8 的"path:line 存在且含所引代码"这条判据拦不住这种"引对了行、编错了上下文"。

**诚实声明**：T2 没有 ground truth，上述数字能证明"上下文不再冻结、重复执行消失"，
不能证明"模型一定找到 bug"——本次就找到了一个半真半假的。
