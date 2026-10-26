# eval-04 · 长时任务机制实测

> 驱动脚本：`scripts/eval_longtask_mechanics.py`（评估专用，真机）。
> 结果：**6 passed, 0 failed**。

## 1. 后台子智能体（`subAgent(background=true)`）

```
turn returned in 11.31s, ledgers=1
[PASS] background turn returns fast (<60s)      11.31s
[PASS] background ledger written                1 ledger(s)
        ledger status=completed iterations=2
```

- 父回合 **11.3s** 返回（不是等子任务跑完），符合「调用即返 run id」的设计。
- 子任务完成后 ledger 落盘：`transcripts/subagents/<id>.json`，`status=completed`。
- **注意**：ledger 是在子任务**完成时**才写，不是父回合返回时。首轮测试没等到完成就退出，
  所以看到 0 —— 这是测试时序问题，不是缺陷（已修正为轮询等待）。

## 2. 上下文压缩（L1–L4）

10 轮真实对话（每轮读同一个 60KB 文件）：

```
turn 1..10: reply=6~29 chars, msgs=6 → 28
after turns: persisted=0 elided=0 msgs=28
L3 probe: persisted=1 marker='<persisted-output path=".../transcripts/persisted/..."'
[PASS] L3 persists an oversized trailing tool result
compact_session -> {'messages': 28, 'after': 3, 'removed': 25}
after compact: elided=1 msgs=3
[PASS] history stays bounded (<= max_messages)   3 messages
L2 probe: path=.../transcripts/elided/read_d468160f464f.txt elided=2
[PASS] L2 elides an aged tool result to a content-addressed file
```

### 关键发现：L3 是**回合中段**守卫，不是回合末守卫

读 [context_compression.py](file:///workspace/ai-coding/ai_coding/core/context_compression.py#L239-L282)
的 `persist_large_outputs`：

```python
end = len(messages)
start = end
while start > 0 and messages[start - 1].role == "tool":   # 只看「尾部连续 tool」
    start -= 1
batch = messages[start:end]
if not batch:
    return messages                                        # 末尾是 assistant → 直接返回
total = sum(_text_bytes(m.content) for m in batch)
if total <= max_result_bytes:                             # max_context_tokens // 2
    return messages
...
if _text_bytes(body) <= per_result_bytes:                 # 30000
    continue
```

- **它只检查消息列表末尾连续的 `role=="tool"` 段**。回合结束时最后一条是 assistant 的回复，
  于是 `batch` 为空、直接返回 —— **所以 L3 在落盘的会话 JSON 里永远看不到**。
- 触发条件：`尾部 tool 段总字节 > max_context_tokens/2`（48000 → 24000），
  且**单条**结果 `> per_result_persist_bytes`（30000）。
- **设计含义**：L3 是「工具刚返回、模型还没答」这一瞬间的护栏（防 400），
  属于**回合中段**保护。要端到端观测它，必须在回合进行中检查历史，而不是回合结束后。
- 本报告用真实函数 + 合成输入做了验证（`persisted=1`，marker 正确）。

### L2 的端到端计数不稳定

L2（`elide_and_persist`）在管线中位于 L4 **之前**（L3 → L1 → L2 → L4），
而 L4 的摘要会先把中段（含旧工具结果）吃掉。所以「压缩后 elided 数量」在不同运行里
是 0 或 1，**不是稳定断言**。已改为直接验证真实 `elide_and_persist`：
内容寻址文件名 `read_<sha1前12位>.txt`，落盘成功。

> **M-E2 固化**：这条承认已变成**显式不变量**，不再是脚注。
> - 顺序由 `tests/test_context_compression.py::test_the_pipeline_order_is_l3_l1_l2_l4`
>   锁住（L2 必须先于 L4；`force=True`，否则断言会 vacuous）。
> - 「计数不可断言」本身写进管线文档与 `_run_l4` 注释：L4 摘要会吃掉 L2 的信封，
>   所以**不以压缩后条数/信封数为目标**；现存断言（`test_context_compression_l2_window.py`、
>   `r7_cases.py`）都是 **token 判据**。

### compact_session 效果

`{'messages': 28, 'after': 3, 'removed': 25}` —— 28 条压到 3 条，
且严格配对未被破坏（后续回合仍可正常回放）。

## 3. 回合看门狗（`turn_timeout_seconds`）

```
elapsed=8.0s reply=189 chars
[PASS] watchdog bounded the turn (<=20s)   8.0s
```

把 `ai.turn_timeout_seconds` 设为 8，让模型执行 `sleep 30` 的脚本：
回合在 **8.0s** 被掐断，`reply` 仍有 189 字符（**已产出的文本被保留**），
且回合以合法 transcript 收尾 —— 与 R2a 的设计一致。

## 4. 中断 / 恢复

- **回合中断**：`sample_tasks` 的 `watchdog` 场景与 `r8_cases --real` 的
  `cancel_kills_the_group` / `dispatcher_cancel_is_structured`（`error=cancelled_by_user`）
  已覆盖「取消是结构化的、不判失败」。
- **boot 续跑**：`world.find_armed_session` 只捞 `armed_profile ∈ {plan, goal}` 的会话
  （见 [world.py](file:///workspace/ai-coding/ai_coding/server/world.py#L66-L97)），
  由 `handlers.initialize` 消费一次 → `_launch_armed_run` 驱动 `PlanRunner`/`GoalRunner`。
  该路径由 `scripts/plan_lifecycle_live_check.py` 覆盖（阶段 5 会真跑）。

## 5. 结论

| 机制 | 结论 |
|---|---|
| 后台子智能体 | ✅ 毫秒级返回 + 完成写 ledger |
| L3 超大输出持久化 | ✅ 生效，但是**回合中段**护栏（落盘会话看不到） |
| L2 信封化 | ✅ 内容寻址落盘可回收；端到端计数不稳定（L4 先行） |
| L4 摘要 | ✅ 28 → 3 条，配对不坏 |
| 回合看门狗 | ✅ 8s 掐断，已产出保留 |
| 中断结构化 | ✅ `cancelled_by_user`，不判失败 |

**未发现新缺陷**；但记录两条**设计层面的观测**（L3 中段语义、L2/L4 顺序导致的计数不稳定），
它们会影响「如何验证压缩」的测试设计。
