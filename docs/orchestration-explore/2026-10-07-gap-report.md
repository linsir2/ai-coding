# 编排层对标差距报告

> 日期：2026-10-07
> 范围：**编排层**（orchestration）—— 消息如何进入、错误如何分层、工具面如何决定、权限如何建模、上下文如何压缩、子智能体如何派生聚合、可观测性、预算、并发。
> **不评 UI，不评检索算法，不评 prompt 文笔。**
> 方法：全部结论来自实际读源码，每条给 `文件:行号`。读过的主要文件见 §12。
> 对标对象：OpenCode（TypeScript / Effect）、GenericAgent（Python）、Hermes（Python）、gstack/openclaw（纯 markdown，无源码）。
> 本项目当前基线：`/home/linsir365/projects/ai-coding`，已含 M1（provider 重试可观测）与 M2（模型降级链）。

---

## 0. 结论速览

本项目在**单轮内的编排纪律**上已经强于三个参考系统中的两个：错误分层是纯函数 + 可测试枚举（`ai/openai_engine.py:84`），压缩顺序是硬不变量（`core/context_compression.py:306`），guard fail-closed（`core/guard.py:119`），trace 的诚实性纪律是全项目最强的一块（`core/trace.py:1-14`）。

差距集中在**三个地方**，而且都不是「某个算法更聪明」，是**结构性的**：

| # | 差距 | 本项目现状 | 参考系统 |
|---|---|---|---|
| A | **入口没有持久化层** | 消息直接进 `process_input`，进程活多久活多久 | OpenCode 有 inbox 表 + doorbell 协调器 + 幂等 admission（`session/inbox.ts`、`session/run-coordinator.ts`） |
| B | **工具面在循环外冻结，权限只挡执行不挡可见** | `tool_schemas()` 在 `AgentLoop.__init__` 算一次（`core/agent_loop.py:215`），永久发给模型 | OpenCode 按 permission ruleset 过滤 snapshot（`core/tool.ts:225`），并把 ruleset 纳入「工具发现」本身（`session/context.ts:134`） |
| C | **权限模型是 tool-name 级静态表，没有 resource 维度** | `PermissionGate.check(tool_name, params)`（`security/permission_gate.py:118`）；always-allow 只是 UI 内存 set（`ui/textual_app.py:95`） | OpenCode 是 `action`/`resource`/`effect` 三元组 + glob + 落库（`core/permission.ts:87`、`core/permission/saved.ts:63`、`core/util/wildcard.ts:3`） |

另有两个「本项目有但更弱」：压缩（无 overflow 驱动的收缩重试、无 provider 原生 compaction）、可观测性（无成本核算、无跨 session 成本归因）。

Hermes 是四个参考里唯一**同语言**的，它的价值不在架构优雅，而在于它把「错误分类 → 恢复动作」做成了一张 18 项的决策表（`hermes/agent/error_classifier.py:26-56`），并且实现了「凭据池轮换」这一层本项目完全没有的中间恢复动作（`run_agent.py:8191`）。但它的载体是 9877 行的 `run_agent.py`——**不可搬**，理由见 §10.1。

gstack/openclaw 里 0 个源文件，全是 4 个 markdown 规范片段 + 4 个 SKILL.md。它没有代码可对标，**唯一可对标的是一条编排纪律**：plan 阶段禁止实现、report 阶段必须写盘（`gstack-plan-CLAUDE.md:4`、`gstack-full-CLAUDE.md:8`）。本项目的 `plan_runner.py` 已经用代码做到了同一件事并且更强（每步一个 bounded turn + 证明命令验收），所以这条不需要搬。

---

## 1. 入口韧性：持久化 inbox、幂等去重、合并唤醒

### 1.1 参考系统怎么做

**OpenCode（有完整三层）**

**第一层：持久化 inbox。** 消息不是直接进循环，而是先进一张 pending-only 的表：

```ts
// packages/core/src/session/inbox.ts:76-106
const fromRow = (row) => {
  const base = { id: ..., sessionID: ..., time: { created: ... } }
  if (row.type === "compaction") return Compaction.make({...})
  if (row.type === "move")     return Move.make({...})
  if (row.type === "user")     return User.make({...})
  if (row.type === "synthetic")return Synthetic.make({...})
  throw new LifecycleConflict({ id: base.id })
}
```

四种待处理输入（user / synthetic / compaction / move）共用一张表，每种带一个 `delivery` 字段（`queue` | `steer`，`inbox.ts:48`）。**行只在被消费时删除**（`projectDelivered` 是 `DELETE ... RETURNING`，`inbox.ts:324-336`），所以这张表天然是「未处理工作队列」，不是一个日志。

**第二层：幂等 admission。** 入队是「先查后发」的，并且用 `onConflictDoNothing` 在投影层做最终防线：

```ts
// session/inbox.ts:155-167
const reconcile = Effect.fn("SessionInbox.reconcile")(function* (request) {
  const existing =
    (yield* find(db, request.id)) ??
    (yield* promotedFromMessage(db, request.sessionID, request.id, request.delivery))
  if (existing === undefined) return undefined
  if (existing.type === "compaction" || !matches(existing, request))
    return yield* new LifecycleConflict({ id: request.id })
  return existing
})
```

同一个 `messageID` 提交两次，第二次拿回既有的那条（`admit` 第一行就是 `reconcile`，`inbox.ts:174`）。冲突时不是覆盖而是 `LifecycleConflict` 失败，且有专门的 `PromptConflictError`（`session/session.ts:172`）。

**第三层：doorbell（合并唤醒）。** 这是最值得看的一段，`run-coordinator.ts:32-58` 的注释把状态机画出来了：

```
 *              wake | run
 *      idle ──────────────▶ execution (one fiber)
 *                             drain ⟲ doorbell rung mid-drain
 *                             │ exit (settled hook runs)
 *      doorbell quiet ◀───────┴───────▶ doorbell rung
 *      idle, waiters get exit          successor execution,
 *                                      waiters get this exit
```

`pendingWake` 就是一个布尔门铃：`drain` 期间来的新工作不会立刻起第二个 fiber，只把门铃按一下；`drain` 结束后检查门铃，按过就再 drain 一轮（`run-coordinator.ts:74-85`）。门铃还**合并作用域**——`"input"` 吸收 `"steer"`（`run-coordinator.ts:140`）。这个设计解决的是一个具体竞态：drain 的最后一次资格检查和 idle 转换之间不可能是原子的一步。

**Hermes（有持久化会话，但无 inbox）**：gateway 侧有 `SessionStore`，transcript 落盘且可 rewrite（`hermes/gateway/session.py:943`、`:971`），但没有 pending-only 的入队表。多个平台（telegram / discord / cron）投递到同一个 session key，靠 `build_session_key`（`gateway/session.py:445`）而不是靠 inbox 表做多路复用。

**GenericAgent（无）**：`agent_runner_loop` 的入参就是一条 `user_input` 字符串，`messages` 每次调用重建（`agent_loop.py:44-47`）。历史在 `*Session` 对象里，由 `agentmain` 持有（`agent_loop.py:104` 注释明说 "just new message, history is kept in *Session"）。

### 1.2 本项目现在怎么做

`process_input` 的第一个动作就是把用户输入写成一条消息，然后直接进循环：

```python
# ai_coding/core/agent_loop.py:473-509
async def _begin_turn(self, session, user_input, observer) -> TurnFrame:
    self._bind_observer(observer)
    self._bind_trace(session)
    session.add_message(user_message(user_input, now_utc()))
    session.turn_count = int(getattr(session, "turn_count", 0) or 0) + 1
    ...
```

全部调用点只有 6 处（`ui/repl.py:79`、`ui/repl.py:179`、`ui/textual_app.py:351`、`core/plan_runner.py:101`、`core/plan_runner.py:484`、`core/plan_runner.py:496`），每个都是「拿到字符串 → await process_input」。

**已有的韧性（这部分做得比「完全没有」好）**：

- **中途落盘**：`TurnCloser.checkpoint` 在每次工具批次后调用，长会话降频到每 3 轮一次（`core/turn_close.py:154-158`）。所以进程崩了已经跑过的部分不丢。
- **落盘失败只降级不中断**：`core/turn_close.py:168-181`，`OSError` → `report_degraded("persist", ...)`。
- **轮内 steering 队列**：有界 FIFO，drain 有上限，剩下的不丢（`core/steering.py:38-59`），且 `/` 开头的行被拒绝交给 REPL（`core/steering.py:44`）。

**缺什么**：

1. **没有 pending-only 队列**。steering 队列是进程内的 `deque`（`core/steering.py:34`），不是持久化的；重启即丢。
2. **没有幂等 admission**。`session.add_message` 无条件追加（`domain/session.py` 的 `_message_to_dict` 是纯序列化）；同一条输入提交两次 = 会话里两份。
3. **没有 doorbell**。`AgentLoop.process_input` 是单 fiber 独占的——REPL 是 `await` 阻塞的（`ui/repl.py:79`），TUI 靠 `input.disabled = True` 防重入（`ui/textual_app.py:84` 注释提到）。也就是说「turn 正在跑时又来一条输入」这件事，**依赖前端自觉关输入框**，而不是编排层保证。
4. **steering 的投递语义和 OpenCode 不一样**：本项目在**每次迭代开始前** drain（`core/agent_loop.py:370` → `_take_steering`），steering 行会消耗一个 `InputSteered` 事件但不消耗 iteration（`core/agent_loop.py:511-534`）。这其实**已经是一个简化的 doorbell**——只是没有门铃状态，也没有「同一轮 drain 内不重复 drain」的保护之外的东西。

### 1.3 差距与判断

**差距**（按严重度）：

| 差距 | 影响 | 现状 |
|---|---|---|
| G1-1 turn 进行中无输入闸门，闸门在前端 | 中。前端任何一个 bug = 两个 turn 并发写同一个 session，`ensure_tool_pairing` 会救回一半但 tool_call 配对会错乱 | 完全没有 |
| G1-2 无消息幂等，重复提交 = 重复计费 + 重复动作 | 中-高。UI 双击、网络重试、脚本重放都会踩 | 完全没有 |
| G1-3 steering 队列不持久化 | 低。steering 本来就是「这轮内的临时插话」 | 已有但弱 |
| G1-4 无 inbox 表，所以无法「agent 自己在跑时排一个任务」 | 低。本项目目前是同步 CLI 形态，没有异步任务入口的需求 | 完全没有 |

**是否值得做**：

- **G1-1 值得，成本最低（1 文件，~30 行）**。把「本 loop 是否正在跑」从 `ui/textual_app.py` 的 `input.disabled` 提升到编排层：`AgentLoop` 持一个 `asyncio.Lock` 或 `_running` 标志，`process_input` 进入时若已在跑，则按 steering 语义排队而不是并发执行。理由：**前端不该持有编排不变量**，而这个不变量现在只在前端。这是唯一一个「零设计成本、纯搬」的项目。
- **G1-2 值得做，但只做最薄的一层**。不需要 inbox 表。只需要给 `SessionData` 一个单调递增的 `message_seq`，`process_input` 接一个可选 `input_id`，重复时返回缓存的上一轮 outcome。这和 OpenCode 的 `reconcile`（`inbox.ts:155`）是同一个思路，成本 ~1 文件。
- **G1-3 不值得。** steering 的语义本来就是「这一轮内的临时插话」，进程重启后丢掉是正确的，不是缺陷。
- **G1-4 现在不值得。** inbox 表（`inbox.ts` 550 行 + `run-coordinator.ts` 196 行 + 配套的 `admitCompaction` / `move` / `Synthetic` 四种类型）是**为多入口异步系统设计的**——OpenCode 有 server / TUI / desktop / cron / gateway 多个投递方。本项目是单进程同步 CLI。搬 750 行 TS 到一个只有一个投递方的 Python CLI，是纯粹的复杂度增加。**明确不建议搬。** 如果将来加了 `/plan` 之外的异步任务、或接了 IDE / web 前端，再谈。

---

## 2. 错误恢复分层：重试 / 换模型 / 换策略 / 中止

### 2.1 参考系统怎么做

**OpenCode —— 四种 step outcome，穷尽且正交**

```ts
// packages/core/src/session/runner/step.ts:33-44
export type Outcome = Data.TaggedEnum<{
  Completed: { readonly needsContinuation: boolean }
  Retry: { readonly error; readonly decision: SessionRunnerRetry.Decision }
  Continue: { readonly error; readonly decision: SessionRunnerRetry.Decision }
  RecoverFull: {}
  Compacted: {}
}>
```

`Retry` vs `Continue` 的区别就是本项目注释里引用过的那条不变量：**已经产生输出后失败，只能 Continue 不能 Retry**：

```ts
// session/runner/step.ts:188-192
    && (recorded.outputStarted && isInterruptedStream(llmFailure)),
...
if (llmFailure && llmError && retry?.retry && !recorded.outputStarted) {
```

`outputStarted` 是一个 flag，`publish-llm-event.ts` 在第一个 text/tool-call delta 时置位。`Continue` 的动作是等完退避后**换一个 `assistantMessageID` 重来**，并往会话里插一条 synthetic 消息（`session/runner/llm.ts:278-286`），原文是 `CONTINUE_AFTER_INCOMPLETE_STREAM`。

退避策略本身也是分层的一部分：

```ts
// session/runner/retry.ts:45-56
const schedule = Schedule.max([
  Schedule.min([Schedule.exponential("2 seconds"), Schedule.spaced("10 seconds")]),
  Schedule.recurs(10),
]).pipe(Schedule.jittered, ...)
```

关键细节：**timeout 单独计数**，上限 3 次（`retry.ts:60`、`retry.ts:84`），因为「一次超时的 attempt 本身已经等了几分钟」。以及 **`retry-after` 被钳到 15 分钟**（`retry.ts:32`），注释写明是防「敌意或 bug 的 retry-after 让 session 卡几小时」。

**Hermes —— 18 项错误分类 + 5 个恢复 hint，最细的一套**

```python
# .hermes/hermes-agent/agent/error_classifier.py:26-56
class FailoverReason(enum.Enum):
    auth / auth_permanent
    billing / rate_limit
    overloaded / server_error
    timeout
    context_overflow / payload_too_large
    model_not_found
    format_error
    thinking_signature / long_context_tier
    unknown
```

分类结果带四个布尔 hint，恢复动作由 hint 决定而不是由 reason 字符串再解析一次：

```python
# error_classifier.py:69-73
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
```

分类是**优先级排序的流水线**（不是 if 链），且注释明说 "Replaces scattered inline string-matching with a centralized classifier"。实测覆盖了三个非显然场景：

- **billing vs rate_limit 分开**：`_BILLING_PATTERNS` 里是 `"insufficient credits"`、`"credit balance"`、`"account is deactivated"`（`error_classifier.py:97-106`），而 `_RATE_LIMIT_PATTERNS` 是 `"rate limit"`、`"tokens per minute"`（`error_classifier.py:108-115`）。同样是 429/配额耗尽，一个等一会就好，一个要立刻换凭据。
- **thinking_signature**：Anthropic 把 thinking block 按整轮内容签名，任何上游改动（压缩、截断、合并）都会让签名失效 → 400。恢复动作是**剥掉所有 `reasoning_details` 再重试一次**（`run_agent.py:8247-8270`）。这是一条 provider 特有的、模型层才修得好的恢复。
- **payload_too_large 走压缩不走 fallback**：`run_agent.py:8416-8428`，413 的动作是 `compression_attempts += 1` 然后压缩重试，上限 `max_compression_attempts`，超了返回 partial。

**凭据池轮换**（本项目完全没有的第三层）：

```python
# run_agent.py:8191-8198
recovered_with_pool, has_retried_429 = self._recover_with_credential_pool(
    status_code=status_code, has_retried_429=has_retried_429,
    error_context=error_context,
)
if recovered_with_pool:
    continue
```

它在 fallback 之前：`run_agent.py:8401-8413` 明确写着「如果凭据池还有货，不要急着 fallback，池的 retry-then-rotate 循环至少需要再跑一次」。租约机制在子智能体上也用（`delegate_tool.py:357-368`，`acquire_lease` 定义在 `credential_pool.py:768`）。

**GenericAgent —— 最弱的一套，但它有一条本项目没有的**

```python
# GenericAgent/llmcore.py:372-378
def _stream_with_retry(sess, url, headers, payload, parse_fn):
    _RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}
    def _delay(resp, attempt):
        try: ra = float((resp.headers or {}).get("retry-after"))
        except: ra = None
        return max(0.5, ra if ra is not None else min(30.0, 1.5 * (2 ** attempt)))
```

HTTP 状态码白名单 + retry-after + 指数退避，**没有错误分类、没有换模型、没有换凭据**。整个 retry 循环在一个函数里，没有 seam。

它独有的一条：**中断检查内嵌在流读取循环里**：

```python
# llmcore.py:397-403
while True:
    if _interrupt_event is not None and _interrupt_event.is_set():
        try: r.close()
        except Exception: pass
        yield "\n[Interrupted by user]\n"
        return [{"type": "text", "text": "[Interrupted by user]"}]
    streamed = True; yield next(gen)
```

`streamed` 这个 flag 同时被 `except` 分支用来决定错误文案（`llmcore.py:414`）：已流式输出则报「流异常中断」，未流式则报普通 error——**这就是 outputStarted 判定的同构物**，只是靠异常路径上的局部变量而不是显式 flag。

### 2.2 本项目现在怎么做

**已有：FailureKind 五值分类 + 换模型的异常分层（M1 + M2）**

```python
# ai_coding/ai/openai_engine.py:34-51
class FailureKind(enum.Enum):
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    TRANSPORT = "transport"
    AUTH_ERROR = "auth_error"
    FATAL = "fatal"
```

分类是纯函数、键在 SDK 异常层级和 `status_code` 上（`openai_engine.py:84-103`），默认 `FATAL` 的理由写得很清楚：多花三个 round trip 去猜是净亏。

「换一个模型是否值得」由**异常类型**而非字符串承担：

```python
# ai_coding/ai/openai_engine.py:59-66
WORTH_ANOTHER_MODEL = frozenset({
    FailureKind.RATE_LIMITED, FailureKind.SERVER_ERROR,
    FailureKind.TRANSPORT, FailureKind.AUTH_ERROR,
})   # FATAL 缺席是有意的

# ai_coding/ai/openai_engine.py:69-81
def _exhausted(kind, message) -> EngineError:
    return FallbackWorthwhile(message, kind=kind.value) if kind in WORTH_ANOTHER_MODEL else EngineError(message)
```

「已产生输出后失败」也已经是一个硬不变量：

```python
# ai_coding/ai/openai_engine.py:395-404
except Exception as exc:
    if text_parts or acc:
        raise EngineError(
            f"stream failed after {len(text_parts)} text chunk(s) and "
            f"{len(acc)} partial tool call(s) had already been delivered: {exc}"
        ) from exc
    raise
```

注释明确说这就是 OpenCode 的 `Retry` / `Continue` 之分（`openai_engine.py:399-400`）。**并且 `AUTH_ERROR` 立刻退出重试**（`openai_engine.py:313-319`）——等一个被拒的 key 是纯浪费，但换 provider 有用。

链的解析在启动时做，含环检测（`ai/factory.py:21-37`），环检测的注释说明了为什么不能延迟到第一次故障：

```python
# ai_coding/ai/factory.py:17-19
Waiting for the first outage to discover it would turn a typo in
a config file into a failure that looks like a provider outage.
```

**缺什么**：

1. **没有「换凭据」这一层**。`ModelConfig` 持一个 `api_key`（`openai_engine.py:240`），一个 key 坏了只能换模型。同 provider 的多 key 轮换是 Hermes 在 auth/rate_limit/billing 三种 reason 上都做的事。
2. **没有 timeout 单独计数**。`retry_delay` 对 `TRANSPORT` 和 `RATE_LIMITED` 用同一个公式（`openai_engine.py:140-142`），只有 RATE_LIMITED 走 retry-after。一个超时的 attempt 本身可能已经耗了几十秒，它和「秒回 429」共用 4 次预算，是本项目最容易浪费 turn 预算的地方。
3. **退避没有 jitter**。`base * (2**attempt)`（`openai_engine.py:142`）是纯确定性退避。本项目是单进程单会话，thundering herd 风险确实低——但**降级链会让多个 turn 同时撞同一个 provider**，这时 jitter 有意义。
4. **`retry-after` 没有独立上限**。`min(max(0.0, retry_after), cap)` 里 `cap = float(cfg.timeout)`（`openai_engine.py:141`、`openai_engine.py:324`），而 `cfg.timeout` 是单请求超时——也就是说一个声称「等 3600 秒」的 provider 会被钳到「单请求超时」那么久。方向是对的（turn deadline 会再钳一层），但 OpenCode 明确钳到 15 分钟的意图没有在本项目留下痕迹。
5. **没有 context_overflow 驱动的恢复**。本项目 `classify_failure` 把所有 4xx 都归到 `FATAL`，所以 provider 拒绝「上下文过长」= turn 结束，不会触发压缩重试。见 §5。
6. **换策略这一层是有的**，且做得比三个参考都好（`core/turn_policy.py`），见 §8。

### 2.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G2-1 timeout 与 rate_limit 共用重试预算 | **高**。长会话里一次超时会连带烧掉后面三次的机会 | **做** | 1 文件 `openai_engine.py`，~8 行 |
| G2-2 退避无 jitter | 中。多 turn 降级到同 provider 时会同步重试 | **做** | 1 文件，~5 行 |
| G2-3 `retry-after` 无独立上限 | 低-中。已被 per-request timeout 间接钳住 | 做（顺手） | 1 文件，~3 行 |
| G2-4 无凭据池轮换 | 中。当前 `api_key` 是单值，配置层就不支持 | **暂不做**。需要先改 `ModelConfig`（让一个 model 名下挂多个 key），这是配置 schema 变更，影响面超出编排层 | 3+ 文件 + schema |
| G2-5 无 thinking-signature 类 provider 特例恢复 | 低。只在 Anthropic 系 endpoint 上出现 | **不做**。本项目 `openai_engine.py` 只处理 chat-completions 协议，且 OpenAI/DeepSeek 兼容端点没这个问题 | — |
| G2-6 分类表只有 5 项 vs Hermes 18 项 | 低。多数 Hermes 项（billing / long_context_tier / format_error）在本项目使用的 provider 上不发生 | 不做，除非接 billing 耗尽的 provider | — |

**明确不建议搬的**：Hermes 那张 18 项表。它的一半条目对应本项目架构里不存在的失败源（凭据池、Anthropic 签名、ChatGPT token sharing）。抄表不抄执行路径 = 抄一张没有调用方的常量表。

**建议搬的具体三行**（全部在 `retry_delay` 附近）：

1. 给 `TRANSPORT` + timeout 一个独立的、更小的重试上限；
2. 退避加 jitter（Hermes 的 `jittered_backoff` 是现成实现，`agent/retry_utils.py:19-57`，57 行纯函数，可直接照抄思路）；
3. `retry_after` 加一个独立的绝对上限常量，语义与 per-request timeout 解耦。

---

## 3. 工具 schema 按权限裁剪

### 3.1 参考系统怎么做

**OpenCode：snapshot 时按 permission ruleset 过滤工具**

```ts
// packages/core/src/tool.ts:225-232
snapshot: Effect.fn("Tool.snapshot")((permissions) =>
  Effect.sync(() => {
    const data = state.get()
    const active = new Map<string, Tool.Info>()
    const rules = permissions ?? []
    for (const [name, tool] of data.tools) {
      if (whollyDisabled(tool.options?.permission ?? name, rules)) continue
      active.set(name, tool)
    }
```

一个工具的 permission key 默认是自己的名字，但可以覆盖（`tool.options?.permission ?? name`）。`whollyDisabled` 判定的是「该 action 下这个 resource 被彻底 deny」。

更重要的一层：**权限不只是过滤执行，也过滤「工具发现」本身**。

```ts
// packages/core/src/session/context.ts:131-141
      // Session permissions narrow discovery the same way they narrow the tool snapshot.
      const permissions = Permission.merge(agent.info.permissions, session.permissions ?? [])
      const loaded = yield* Effect.all(
        {
          tools: registry.snapshot(permissions),
          builtins: builtins.load(),
          discovery: discovery.load(),
          skills: skillInstructions.load(permissions),
          references: referenceInstructions.load(),
          mcp: mcpInstructions.load(permissions),
          entries: entries.load(sessionID),
        },
        { concurrency: "unbounded" },
      )
```

`skills` / `mcp` 的指令加载也吃同一个 permissions。**被 deny 的工具，模型连它存在都不知道。**

Hermes：toolset 是命名集合，权限体现为 toolset 级白名单（`hermes/toolsets.py:195` 的 `delegation` toolset 只含 `delegate_task`），子智能体构造时按名单剥掉（`hermes/tools/delegate_tool.py:108-114`）：

```python
# tools/delegate_tool.py:108-114
def _strip_blocked_tools(toolsets: List[str]) -> List[str]:
    """Remove toolsets that contain only blocked tools."""
    blocked_toolset_names = {"delegation", "clarify", "memory", "code_execution"}
    return [t for t in toolsets if t not in blocked_toolset_names]
```

GenericAgent：一个全局 `BANNED_TOOLS`，按 argv 过滤（`agentmain.py:17-22`），而且**只在启动时算一次**，之后所有轮次复用同一个 `TOOLS_SCHEMA`。

### 3.2 本项目现在怎么做

工具 schema 在 loop 构造时算一次，然后**永远不变**：

```python
# ai_coding/core/agent_loop.py:215-223
self._tool_schemas = tool_schemas(tools)
self._tools_text = short_json(self._tool_schemas)
self._tools_chars = len(self._tools_text)
self._tools_by_origin = _tools_by_origin(tools)
self._tools_warned = False
```

`_tool_schemas` 在每次 `_model_step` 原样传入（`agent_loop.py:788`：`tools=self._tool_schemas`）。

`tool_schemas` 本身也只是「全部注册工具，按名字排序」，无过滤：

```python
# ai_coding/tools/schema.py:32-39
def tool_schemas(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Return schemas for every registered tool, in ``ToolRegistry`` order."""
    return [tool_schema(tool) for tool in registry.all_tools]
```

**已有的相关能力**（但作用在别处）：

- **MCP 工具按 server 白名单过滤**：`MCPManager.open_all` 里 `wanted = {str(name) for name in getattr(server, "tools", []) or []}`（`core/mcp.py:297`）。这是**配置时过滤**，不是权限时过滤。
- **trusted_servers**：`PermissionGate` 对 `mcp.servers[].auto_approve` 的 server 名做 ALLOW（`security/permission_gate.py:147-152`），但这是**执行时**放行，工具照样出现在 schema 里。
- **工具面过大时有告警**：`ToolSurfaceWarning`（`core/agent_loop.py:536-556`），按 origin 归因并给出「关掉哪个 MCP server」的提示。**只告警，不裁剪。**

**结论**：本项目有「工具面成本」意识（R7e-3），但没有「工具面权限」概念。deny 一个工具 ≠ 模型看不见它。

### 3.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G3-1 deny 的工具仍出现在 schema 里 | **中**。模型会反复尝试一个必然被拒的工具，浪费 iteration 并诱发 `[unknown tool]` 式困惑。本项目的 unknown tool 已经是被结构化处理的（`core/dispatch.py:466-472`），但「已知但被 deny」和「未知」给模型的信息量不同 | **做**，但要等 §4 的 permission 三元组先落地，否则没有「按资源 deny」的依据 | 依赖 G4 |
| G3-2 子智能体工具栈是固定裁剪，无权限继承 | 中。`include_subagent=False` 硬编码（`tools/builder.py:41`），子智能体拿不到任何权限覆盖 | 做（和 G4 同批） | 1-2 文件 |
| G3-3 `ToolSurfaceWarning` 只告警 | 低-中。超过阈值就裁剪是激进的（会改变能力），但可以做成「超阈值时提示 + 列出可裁剪项」 | 已有，可加一个「本次运行禁用哪些 server」的运行时开关 | 1 文件 |
| G3-4 schema 在 loop 生命周期内冻结，运行时新增 MCP 工具不会进入下一轮 | 低。`MCPManager.open_all` 在启动时调用一次 | **不做**。冻结是 prompt cache 稳定的前提（`tools/schema.py:33-38` 的注释），动态化会摧毁缓存命中 | — |

**明确不建议搬的**：OpenCode 的 **codemode**（`tool.ts:235-250`）——把工具集折成一个 `execute` 工具 + 一份目录，让模型用代码调用工具。这确实能把工具 schema 压掉一大截，但它是**一个新的编程范式**（需要模型具备 code-execution 能力、需要独立的安全边界、需要重写全部工具的输入校验），不是编排层的差距。对本项目这种工具数不多（~15 个内建 + 可选 MCP）的形态，收益不足以抵消复杂度。

---

## 4. 权限模型粒度

### 4.1 参考系统怎么做

**OpenCode：`action` / `resource` / `effect` 三元组 + glob + 落库**

schema 定义（`packages/schema/src/permission.ts:59-63`）：

```ts
export const Rule = Schema.Struct({
  action: Schema.String,
  resource: Schema.String,
  effect: Effect,
})
```

求值是 `findLast`（后者覆盖前者，声明序即优先级）：

```ts
// packages/core/src/permission.ts:87-97
export function evaluate(action: string, resource: string, ...rulesets: Permission.Ruleset[]): Permission.Rule {
  return (
    rulesets.flat()
      .findLast((rule) => Wildcard.match(action, rule.action) && Wildcard.match(resource, rule.resource)) ?? {
      action, resource: "*", effect: "ask",
    }
  )
}
```

`Wildcard.match` 是一个 ~14 行的 glob，支持 `*` 和 `?`，还特判了尾随 `*`：

```ts
// packages/core/src/util/wildcard.ts:3-13
export function match(input: string, pattern: string) {
  const normalized = input.replaceAll("\\", "/")
  let escaped = pattern.replaceAll("\\", "/")
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*/g, ".*").replace(/\?/g, ".")
  if (escaped.endsWith(" .*")) escaped = escaped.slice(0, -3) + "( .*)?"
  return new RegExp("^" + escaped + "$", ...).test(normalized)
}
```

ruleset 的合成顺序有明确语义：agent 级 + session 级先合并（`permission.ts:162`），**deny 优先**（`denied()` 只看配置层，`permission.ts:165-167`），**always-allow 叠加在最后**（`savedRules` 是 `effect: "allow"` 的独立表，`permission.ts:148-156`、`:176`）：

```ts
// permission.ts:173-178
const evaluateInput = ... {
  const rules = yield* configured(input.sessionID, input.agent)
  if (denied(input, rules)) return { effect: "deny" as const, rules }
  const all = [...rules, ...(yield* savedRules())]
  const effects = input.resources.map((resource) => evaluate(input.action, resource, all).effect)
  const effect: Permission.Effect = effects.includes("ask") ? "ask" : "allow"
```

**always-allow 落库，且带 resources 列表**：

```ts
// permission.ts:299-305
if (input.reply === "always" && existing.request.save?.length) {
  yield* saved.add({
    projectID: location.project.id,
    action: existing.request.action,
    resources: existing.request.save,
  })
}
```

`save` 由**每个工具自己声明**，且粒度差异很大：

| 工具 | `save` | 文件 |
|---|---|---|
| `write` / `edit` / `patch` / `grep` / `glob` / `webfetch` / `websearch` | `["*"]` | `tool/plugin/write.ts:81`、`tool/plugin/edit.ts:183` 等 |
| `bash` | `parsed.commands.map(c => c.save)` —— **每条解析出的命令自己的 save** | `tool/plugin/shell.ts:138` |
| `subagent` | `[agent.id]` —— 只对这个 agent 生效 | `tool/plugin/subagent.ts:142` |
| `skill` | `[skill.id]` | `tool/plugin/skill.ts:54` |
| `mcp-resource` | `servers` 或 `` [`${server}:*`] `` | `tool/plugin/mcp-resource.ts:39`、`:68` |

也就是说 `bash` 的 always 存的是**具体命令模式**（由 `ShellParse.scan` 产出），不是「所有 bash」。

还有一个本项目完全没有的语义：**deny 一个会让整批都死**：

```ts
// permission.ts:289
// Feedback applies to the whole batch, so parallel asks don't end the step.
```

reject 一条 pending 会 reject 同 session 的所有 pending（`permission.ts:282-295`），且带 feedback 的 reject 变成 `CorrectedError` 而不是 `DeclinedError`——前者是给模型看的纠正信息，后者是静默失败（`permission.ts:66-68`、`:279`）。

**Hermes**：只有 toolset 级白名单 + `check_fn` 门控（`toolsets.py:59-61` 注释说 `send_message` gated on gateway running），没有 action/resource/effect 三元组。**在这项上比 OpenCode 弱一个量级。**

**GenericAgent**：完全没有权限层。9 个工具全部直接可调（`agent_loop.py:22` 的 `dispatch` 就是反射找 `do_<name>`）。

### 4.2 本项目现在怎么做

`PermissionGate.check(tool_name, params)`，工具名 + 参数，产出三值判定：

```python
# ai_coding/security/permission_gate.py:118-158
def check(self, tool_name: str, params: dict[str, Any]) -> Decision:
    name = tool_name.lower()
    if name == "bash":
        command = str(params.get("command", ""))
        for pattern, reason in _BASH_HARD_DENY_PATTERNS:
            if pattern in command:
                return Decision(PermissionDecision.DENY, f"bash command blocked: {reason} ('{pattern}')")
        return Decision(PermissionDecision.WARN, "bash command requires confirmation")
    if name in self._allow_tools: return Decision(PermissionDecision.ALLOW, "")
    if name in self._warn_tools:  return Decision(PermissionDecision.WARN, ...)
    server, sep, _remote = tool_name.partition("__")
    if sep and server in self._trusted_servers: return Decision(PermissionDecision.ALLOW, "")
    return Decision(PermissionDecision.WARN, f"unknown tool '{tool_name}' requires confirmation")
```

`_BASH_HARD_DENY_PATTERNS` 是 10 条字面子串（`permission_gate.py:70-81`）。注意 `partition("__")` 的注释说明了为什么只匹配 server 段：防止本地工具自称 `"bash__x"` 来骗取信任（`permission_gate.py:147-149`）。

**runtime guard 是第二层，做得比 OpenCode 好**：

| guard | 做什么 | 行 |
|---|---|---|
| `DuplicateCallGuard` | 本轮重复调用同 fingerprint 超阈值 → DENY，并指名早先的 call id | `core/guard.py:145-182` |
| `BashPolicyGuard` | 用户配的 bash 红线（子串匹配） | `core/guard.py:185-210` |
| `PathEscapeGuard` | bash 引用 workspace 外的绝对路径 token；默认 observe-only，`strict=True` 时 DENY | `core/guard.py:270-295` |
| `SecretGuard` | post-pass 脱敏（history 默认不动，因为改 history = 改模型看到的东西） | `core/guard.py:216-248` |

guard 链的 fail-closed 是硬的（`core/guard.py:112-127`）：任何一个 guard 抛异常 = DENY。rewrite 之后**必须重新过 gate**（`core/dispatch.py:300-307`），注释写明 "A rewrite must never be a way around the static gate"——这是 OpenCode 和 Hermes 都没做的细节。

**always-allow 只是 UI 内存里的一个 set**：

```python
# ai_coding/ui/textual_app.py:95
self._always_allow: set[str] = set()

# ai_coding/ui/textual_app.py:419
if tool_name not in ALWAYS_EXCLUDED and tool_name in self._always_allow:

# ai_coding/ui/textual_app.py:441
    self._always_allow.add(tool_name)
```

`ALWAYS_EXCLUDED = frozenset({"bash"})`（`ui/modals.py:32`），理由是 "an always-allow on bash would approve every command"。

**这是本项目和 OpenCode 差距最大的一项**，而且差距不只是「粒度」，是**三个正交的东西都缺**：

| 缺什么 | 后果 |
|---|---|
| resource 维度（路径 / 命令模式） | 「allow write」= allow 写整个文件系统，包括 `/etc` 和 `~/.ssh/authorized_keys` |
| glob 匹配 | 只能靠字面子串（`_BASH_HARD_DENY_PATTERNS`），无法表达 `rm -rf /home/*` 或 `git push --force-*` |
| 持久化 | 进程重启后所有 always-allow 消失，用户每次重启都要重新批准一遍。**这不是安全设计，这是 UX 缺陷被误当成安全设计** |

最后一条值得强调：`_always_allow` 是 per-process 的，所以它实际提供的安全保证是**零**——它唯一的作用是让当前进程内同一工具不重复弹窗。而 `ALWAYS_EXCLUDED` 排除 bash 的那份谨慎，在这个设计下没有意义：bash 本来就不进 set。

### 4.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G4-1 always-allow 不持久化 | **高**（UX + 认知混乱：用户以为记住了，其实没有） | **做**。最小形态：在 session 文件里存一个 `always_allowed: list[str]`（或路径模式），`SessionData` 加载时读入 | 2 文件（`domain/session.py` + `ui/textual_app.py`），~40 行 |
| G4-2 无 resource 维度 | **高**（安全边界实质缺失：`write`/`edit` 是 WARN 靠人，但人看不出会不会写 `.env`） | **做**。给 `PermissionGate` 增加「path-like 工具的资源提取 + 白/黑名单」，bash 已有 hard-deny 可作模板 | 1-2 文件 |
| G4-3 无 glob 匹配 | 中。当前只能子串 | 做（和 G4-2 同批，一个 ~14 行的 `wildcard.match` 即可） | 1 文件（新增小 util） |
| G4-4 `Action/Resource/Effect` 三元组通用框架 | 中-高 | **不做完整版**。理由：OpenCode 的三元组是为「多 agent × 多 session × 多工具集」的场景设计的，本项目是单 agent 单 session。完整版会引入 ruleset 合成、优先级、agent 级配置三个本项目不需要的概念 | — |
| G4-5 reject 不联动整批 | 低。串行模式下一次只有一个 pending | **不做** | — |
| G4-6 `CorrectedError`（带 feedback 的拒绝）语义 | 低 | **不做**。本项目没有「用户给纠正信息」这个交互 | — |

**建议的最小方案（不是照搬 OpenCode，是取其必要子集）**：

1. `always_allowed` 持久化到 session（按资源模式，不是工具名）；
2. `PermissionGate` 增加资源维度：`write`/`edit`/`read` 提取 `path` 参数，`bash` 提取命令模式，匹配时用 glob；
3. `ALWAYS_EXCLUDED` 改为「bash 可存但必须带具体命令模式」，即 `ALWAYS_EXCLUDED` 逻辑从「按工具名排除」变成「按资源粒度不足时排除」。

这个改动会让 §3 的工具面裁剪成为可能（deny 的资源 → 对应工具可以不出现在 schema）。

---

## 5. 上下文管理

### 5.1 参考系统怎么做

**OpenCode：三种触发 × 四种交付路径，本项目只有一种触发一种路径**

**触发**（`packages/core/src/session/compaction.ts:50-54`）：

```ts
export type Trigger =
  | { readonly reason: "auto" | "overflow"; readonly context: SessionContext.Loaded }
  | { readonly reason: "manual"; readonly context: SessionContext.Loaded; readonly inputID: SessionMessage.ID }
```

三种原因：
- `auto` —— 估算超过 ceiling（`compaction.ts:227-236` 的 `due`）
- `overflow` —— **provider 刚拒了这个上下文**。注意 budget 计算：
  ```ts
  // compaction.ts:211-213
  // The provider just rejected this context, so the estimate ran low; the first attempt already aims below it.
  const budget =
    trigger.reason === "overflow" ? Math.min(cap, Math.floor(estimateContext(context) * SHRINK_STEPS[0])) : cap
  ```
  `SHRINK_STEPS = [0.7, 0.5, 0.35]`（`compaction.ts:92`），即第一次被拒后按 70% 重试。
- `manual` —— `/compact`，结果通过 inbox item 呈现（`compaction.ts:53`）

**overflow 的收缩重试循环**（`compaction.ts:388-439`）：

```ts
while (true) {
  const fits = !asText && estimateRequest(prepared.request) <= target
  const request = fits ? prepared.request : (flattenAndDropOldest(...) ?? ...)
  ...
  if (tooLarge && request === prepared.request) { asText = true; continue }
  if (tooLarge || isContextOverflowFailure(cause)) {
    const step = SHRINK_STEPS[rejections]
    if (step === undefined) return yield* Effect.fail<Failure>({ error })
    rejected ??= estimateRequest(request)
    target = Math.floor(rejected * step)
    rejections++; continue
  }
```

三个阶段：原样 → 压平成文本并丢最旧 → 按 70/50/35 递减重试 → 放弃。`payload-too-large`（字节问题）走一条独立分支：把媒体换成文本（`asText = true`），不计入 `rejections`。

**模板不合格时 nudge 一次**：

```ts
// compaction.ts:274-291
const send = (request) => Effect.gen(function* () {
  const prompted = LLMRequest.update(request, { messages: [...request.messages, Message.user(prompt)] })
  const reply = yield* stream(context, prompted, prepared.options)
  if (filled(reply.text)) return { ...reply, recent: split.recent }
  const nudged = LLMRequest.update(prompted, { messages: [...prompted.messages, Message.user(NUDGE)] })
  const retry = yield* stream(context, nudged, prepared.options)
  if (filled(retry.text)) return { ...retry, recent: split.recent }
  return yield* Effect.fail<Failure>({ error: { type: "compaction.failed", ... } })
})
```

**provider 原生 compaction**（`compaction.ts:309-357`）：`model.compaction?.type === "native"` 时不自己摘要，而是调 endpoint 的 compact 接口。而且检查了 provenance 一致性——「Provider compaction requires the endpoint in provider/model settings, not a model.request rewrite」（`compaction.ts:325`）。

**ceiling 计算**（`compaction.ts:890-897`）：窗口的 10% 或 `RESERVE_MIN = 16_000`，取大者。

**压缩失败后的恢复**：`compact()` 返回 `{ status: "failed", error }`（`compaction.ts:60`），runner 把它变成 `StepFailedError` 结束这个 step（`runner/llm.ts:216-217`），但 **`recoverOverflow` 标志会被关掉**，避免无限重试：

```ts
// runner/llm.ts:279-283
Compacted: Effect.fnUntraced(function* () {
  recoverOverflow = false
  assistantMessageID = SessionMessage.ID.create()
}),
```

`RecoverFull` 同理，关掉 `recoverContinuation`（`runner/llm.ts:284-286`）。

**Hermes：压缩触发更简单（阈值 + preflight），但错误驱动的压缩恢复更完整**

```python
# hermes/agent/context_compressor.py:131-139
def should_compress(self, prompt_tokens=None) -> bool:
    tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
    return tokens >= self.threshold_tokens

def should_compress_preflight(self, messages) -> bool:
    rough_estimate = estimate_messages_tokens_rough(messages)
    return rough_estimate >= self.threshold_tokens
```

压缩是 5 阶段（`context_compressor.py:612-624` docstring）：剪旧 tool result（纯本地，不调 LLM）→ 保护 head → 按 token budget 找 tail 边界 → 摘要中间 → 反复压缩时增量更新前一份摘要。之后 `_sanitize_tool_pairs` 清理孤立配对（`:452`）。

**错误驱动**：413 → 压缩重试（有上限，超了返回 partial，`run_agent.py:8416-8461`）；context_overflow → 压缩重试（`run_agent.py:8465+`）。

**GenericAgent**：最粗暴但有效——`trim_messages_history` 按 `context_win * 3` 字符数 cap，然后**直接 pop 最旧的消息**，pop 后把开头的 user 消息里的 tool_result 改写成纯文本（`_sanitize_leading_user_msg`，`llmcore.py:85-103`）。`compress_history_tags` 每 5 轮做一次 tag 内截断（`llmcore.py:52-56`）。

### 5.2 本项目现在怎么做

**四层流水线，顺序是硬不变量**（这是本项目最强的一块设计，注释里明说）：

```python
# ai_coding/core/context_compression.py:306-308
"""Order is a hard invariant: L3 persist -> L1 snip -> L2 placeholder ->
L4 LLM summary (with circuit breaker) -> hard trim -> sanitize pairs.
The caller's message list is never mutated — ``compress`` works on a copy.
"""
```

四层：
- **L3** `persist_large_outputs`（`core/context_compression.py:169`）—— 超大 tool output 落盘，history 里留 `<persisted-output path=...>` 标记 + 2000 字 preview。内容寻址，同一 body 二次省略不产生第二个文件（`:105-107`）。
- **L1** `snip_middle`（`:62`）—— 超长历史中间段替换成 `[snipped N messages]`。
- **L2** `placeholder_old_results`（`:123`）—— 旧 tool result 换成信封 + 头尾窗口。**四条规则每条是一个测试**：不值得缩短的**原样不动**（`:157-158`）、总窗口预算有界且**新到旧花**（`:150`、`:162`）、配对字段不动、`<persisted-output>` 标记跳过。
- **L4** `ContextManager._run_l4`（`:414`）—— LLM 摘要，带熔断（`_L4_BREAKER_THRESHOLD = 3`，`:34`、`:410-411`），失败降级回截断历史（`:430-431`）。

**触发是预算驱动的**：

```python
# core/context_compression.py:375-378
def should_compact(self, messages) -> bool:
    soft = int(self._max_context_tokens * self._compress_soft_ratio)
    return estimate_tokens(_joined_history_text(messages)) > soft
```

`compress_if_needed` 在软水位以下原样返回（`:393-394`），注释给出实测依据：「92-93% hit rate with append-only turns, 26% when the middle was rewritten every turn」。

**压缩失败有清晰的三态**（`core/compaction.py:81-89`）：任何异常 → `_degraded(...)` → 返回原始 history，**且从不改写调用方的 list**。`compact_now`（用户显式 `/compact`）只在真的变了时才替换 session（`:111-113`），注释说得很直白："nothing changed; do not pretend it did"。

**这部分的诚实度纪律是全项目最好的**：L3 落盘失败就保留原 payload（`:203-204`）、L2 写不了文件就只少说一句话（`:96-99`）、audit copy 写失败就 pass（`:448-449`）。

**缺什么**：

| 缺什么 | 后果 |
|---|---|
| G5-1 无 provider-rejected 驱动的压缩重试 | provider 报 400/413 时 `classify_failure` 归到 `FATAL`（`ai/openai_engine.py:103`）→ turn 直接结束。用户的会话被丢掉，压缩一次都没试 |
| G5-2 无 shrink-and-retry 阶梯 | 即使有 G5-1，也没有「按 70/50/35 递减」的多级尝试 |
| G5-3 L4 摘要不合格无 nudge | OpenCode 会补一句 `NUDGE` 重试一次（`compaction.ts:280-282`）。本项目 `_run_l4` 拿到任何非空 summary 就接受（`:430`），一个「好的」summary 不做任何形状校验 |
| G5-4 无 provider 原生 compaction | DeepSeek / Anthropic 系端点的 server-side compaction 完全没用上 |
| G5-5 ceiling 是静态比例，无 reserve | 本项目 `max_context_tokens * 0.8`；OpenCode 还留了 `RESERVE_MIN = 16000` 给最后一次回复和摘要本身（`compaction.ts:94`） |
| G5-6 `estimate_tokens` 是字符启发式 | CJK 按 1/2、其他按 1/4（`core/context_compression.py:47`）。注释自己说是 "used only to drive compression thresholds, not exact"。这是一个**系统性偏差来源**：一个中英混合会话的实际 token 数会显著偏离估算 |

### 5.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G5-1 provider 拒绝时压缩重试 | **高**。这是唯一一个「用户已经付出全部成本但因为上下文超长而彻底失败」的场景 | **做**。需要：① `classify_failure` 增 `CONTEXT_OVERFLOW` / `PAYLOAD_TOO_LARGE`；② loop 在 `EngineError` 落到 `except` 之前，若 kind 是这两者则强制压缩一次并重放当前 step | 2 文件（`ai/openai_engine.py` 分类 + `core/agent_loop.py` 的 `_model_step` 加重试分支），~50 行。**注意：这会动 M1/M2 的异常分层，需要确认不破坏 `FallbackWorthwhile` 的语义** |
| G5-3 L4 摘要形状校验 + 一次 nudge | 中。摘要质量直接决定压缩后的 agent 能力 | 做（便宜）：`SUMMARY_TEMPLATE` + `filled()` 风格的检查 + 一次 nudge | 1 文件，~20 行 |
| G5-6 token 估算偏差 | 中-高。它是 G5-1 的放大器：估算偏低 → 压缩触发太晚 → provider 先拒 | **做**，成本低：本项目已有真实 usage（`domain/usage.py`），可以像 OpenCode 那样用「最近一次实测 + 之后按字符估」的混合口径（`compaction.ts:859-883`） | 1 文件 |
| G5-5 reserve | 低 | 顺手做，`max_context_tokens - max(0.1*W, 16000)` 一行 | 1 行 |
| G5-2 shrink 阶梯 | 低（G5-1 做了之后收益递减） | 不单独做，除非 G5-1 上线后实测需要 | — |
| G5-4 provider 原生 compaction | 中，但依赖 provider 能力 | **暂不做**。本项目 `ai/openai_engine.py` 是纯 chat-completions，没有 compaction 协议位 | — |

---

## 6. 子智能体

### 6.1 参考系统怎么做

**OpenCode：子智能体是一个真正的 session，深度可配，权限被断言**

深度限制是**显式可配 + 沿父链计数**：

```ts
// packages/core/src/tool/plugin/subagent.ts:116-127
let current = parent
let depth = 0
while (current.parentID) {
  depth++
  current = yield* sessions.get(current.parentID)...
}
const limit = Config.latest(yield* config.entries(), "experimental")?.subagent_depth ?? 1
if (depth >= limit)
  return yield* new ToolFailure({
    message: `Subagent depth limit reached (${limit}). Increase "experimental.subagent_depth" to allow nested subagents.`,
  })
```

默认 `1`（只能一层），但可以开。而且它支持**复用已有子会话**：`input.sessionID` 存在时检查 `existing.parentID === context.sessionID`（`subagent.ts:159-166`），可以「继续同一个子智能体」——这是**断点续跑**，不是一个全新的 run。

聚合：子智能体是独立 session，跑完后 `SubagentCompletion.deliver`（`session/subagent-job.ts:29`）。子会话本身**继承父的历史**，所以结果天然在父的上下文血缘里。

恢复：`SubagentJob.start` 会 `sessions.resume(childSessionID)`（`session/subagent-job.ts:45`）——进程死后重启可以从已记录的 Session 历史继续。

**Hermes：并行多任务 + 凭据租约继承**

```python
# hermes/tools/delegate_tool.py:357-368
child_pool = getattr(child, '_credential_pool', None)
leased_cred_id = None
if child_pool is not None:
    leased_cred_id = child_pool.acquire_lease()
    if leased_cred_id is not None:
        try:
            leased_entry = child_pool.current()
            ...
```

子智能体从池里**租**一个凭据而不是继承父的。`check_delegate_requirements()`（`:43`）是先决检查。结果结构化返回 `status` / `summary` / tool trace（`delegate_tool.py:398-400`）：

```python
# delegate_tool.py:392-398
if interrupted:   status = "interrupted"
elif summary:     status = "completed"     # 有 summary 就是有可用产出
else:             status = "failed"
```

注意这里 **exit_reason 单独传给父智能体**（注释："exit_reason ('completed' vs 'max_iterations') already tells the parent *how* the task ended"）——status 和 exit_reason 是两个正交维度。本项目把它们合并进了一个 `status`（见下）。

**GenericAgent**：无子智能体。

### 6.2 本项目现在怎么做

**已经是四个参考里最完整的**（除 OpenCode 的深度可配和 resume）：

- **同步 + 后台两条路径**，共用同一套 semaphore / timeout / ledger 纪律（`core/subagent.py:168-172` vs `:300-336`）。
- **信号量在 worktree 之前获取**，所以并发上限同时约束了 worktree 数量（`core/subagent.py:11-12` 的注释说明了这个顺序的理由）。
- **后台任务的两个漏洞都补了**：
  - 任务在第一次 step 之前就被 cancel → `asyncio` 不会执行它的 body → `_reap` done-callback 兜底（`core/subagent.py:267-270`、`:369-398`）；
  - 饱和时**大声拒绝**而不是静默排队：
    ```python
    # ai_coding/core/subagent.py:244-249
    if self._semaphore.locked():
        raise RuntimeError("sub-agent concurrency exhausted: ... retry later or raise ai.max_concurrent_subagents")
    ```
    注释理由："the turn must not pay for work that never starts"。
- **worktree 隔离降级可见**：`WorktreeError` → `report_degraded(..., "ran in the main workspace instead")`，且用子智能体的 scoped observer 包了一层（`core/subagent.py:198-208`），否则多个并发子智能体的降级记录无法归属。
- **结果 packet 结构化**：`status` / `conclusion` / `todos` / `artifacts` / `iterations` / `workdir` / `ledger_path` / `error` / `context_mode` / `truncated` / `exit_reason` / `usage` / `facts` / `schema_valid` / `schema_errors`（`core/subagent.py:544-559`）。
- **status 与 exit_reason 分离做了一半**：`_STATUS_MAP` 把 `budget` 映射成 `max_iterations`（因为「跑完预算」是部分完成不是失败，`core/subagent.py:62`），同时 `exit_reason` 保留原始状态（`:531-534`）。
- **`output_schema` 输出契约**：JSON 形状契约 + `schema_valid` 判定，且**原文永远保留**（`core/subagent.py:164-165`：「a bad brace must not kill an hour of work」）。facts 从**截断后**的 conclusion 解析，所以 facts 不会描述父看不到的文本（`:535-536`）。
- **ledger 落盘**：`transcripts/subagents/*.json`（`core/subagent.py:571-582`）。
- **上下文模式可配**：`full`（继承父的 view + instructions）vs `task_only`（干净窗口，父必须把事实写进 task 文本）（`core/subagent.py:139-141`、`:430-444`）。
- **子智能体的 continuation 被强制关闭**，且理由带实测数据：
  ```python
  # ai_coding/core/subagent.py:500-503
  # C3 off: the parent is blocked on this child, so a continuation buys
  # the parent nothing but latency — and it multiplies the child's
  # iterations by (1 + max_auto_continuations).  Measured with an open
  # todo item and max_tool_iterations=3: 12 model calls instead of 4.
  ```
  这个实测驱动的判断比任何参考系统的设计都扎实。

**缺什么**：

| 缺什么 | 现状 |
|---|---|
| 嵌套深度硬编码为 0 层 | `include_subagent=False` 是常量（`tools/builder.py:41`）。**不能嵌套**，且不可配置 |
| 无「继续已有子智能体」 | 每次 `spawn_background` 都是全新 `sub-{uuid}` session（`core/subagent.py:446`） |
| 无跨进程恢复 | 后台表是内存 dict（`core/subagent.py:129`），进程死即失；注释里也承认 "Restarts lose the whole table anyway (documented, Hermes rule)"（`:79`） |
| `_STATUS_MAP` 把 `stopped`/`aborted` 映射成 `error` | 语义损失。父看不到「子智能体是被 stop 的」和「子智能体崩了」的区别 |
| **子智能体的 guard 链是空的** | `core/subagent.py:452-461` 构造 `AgentLoop` 时**没传 `guards`**，所以子智能体里没有 `DuplicateCallGuard` / `PathEscapeGuard` / `BashPolicyGuard`。这是一个**实际的安全/质量缺口**：父有子没有 |

最后一条值得单独确认。`AgentLoop.__init__` 的 `guards` 参数默认 `None`（`core/agent_loop.py:161`），然后 `ToolDispatcher` 里 `guards if guards is not None else GuardChain([])`（`core/dispatch.py:163`）——空链恒定 ALLOW。而 `SubAgentRunner._run_in` 构造 `AgentLoop` 的完整参数列表里确实没有 `guards`。**子智能体在隔离 worktree 里跑、有自己的 bash，等于没有 bash 红线和路径逃逸检查。** 这是我在本次调研里发现的最实的一个缺口。

### 6.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G6-1 子智能体 guard 链为空 | **高**。子智能体跑的 bash 没有 `BashPolicyGuard` 红线、没有 `PathEscapeGuard`、没有 `DuplicateCallGuard`。父有子没有，隔离了工作区但没隔离策略 | **做，最高优先级** | 1 文件（`core/subagent.py:452-461` 加一个 `guards=` 实参 + `SubAgentRunner.__init__` 收一个参数 + `tools/builder.py:47-58` 传入） |
| G6-2 嵌套深度不可配 | 中。默认 0 层是安全的，但有些任务（一个子智能体去调研、结果再分给两个子智能体）需要 2 层 | 做（可选）：把 `include_subagent` 从 bool 换成 `max_depth: int`，默认 0 | 1-2 文件 |
| G6-3 无「继续已有子智能体」 | 中。子智能体超时后从零重来 | **暂不做**。需要持久化子会话 + resume 语义，是一轮独立设计 | 3+ 文件 |
| G6-4 `_STATUS_MAP` 丢失 stopped/aborted | 低-中。父拿到 `error` 无法区分「崩了」和「被叫停」 | **做**（便宜）：映射成 `cancelled` / `stopped` 而非 `error` | 1 文件 |
| G6-5 后台表不持久化 | 低。同 G1-3 | 不做 | — |

---

## 7. 可观测性

### 7.1 参考系统怎么做

**OpenCode：事件总线 + 事件溯源投影 + 孤儿收敛**

- 所有事件走 `Bus.publish`，每个事件有对应的 projector 把状态投到 SQLite（`inbox.ts:277`、`inbox.ts:324` 等）。**状态是从事件流推出来的**，不是被直接写的。
- **进程死后收敛孤儿**：两个专职函数。`settleStaleCompactions` 把 `status === "running"` 的 compaction 行补成 `compaction.interrupted`（`session/runner/llm.ts:295-322`），`settleStaleToolCalls` 把 `streaming`/`running` 的 tool 补成失败（`session/runner/llm.ts:324+`）。**这是「写入-重启」一致性的最小实现**。
- 成本核算：`SessionUsage.record(usage, model.cost)` 在每次模型调用结束时立即记账（`compaction.ts:330-331`），失败的压缩也计费（注释在 `compaction.ts:595-598`）。
- 每条 event 带 `metadata`（`compaction.ts:641` 的 `{ metadata: outcome.metadata }`）。

**Hermes**：span 化的观测 + 便宜模型路由

- `agent/trajectory.py`（56 行）记录轨迹。
- `smart_model_routing.py`（194 行）是一个**任务复杂度 → 便宜模型**的启发式：
  ```python
  # hermes/agent/smart_model_routing.py:11-38
  _COMPLEX_KEYWORDS = {"debug","debugging","implement","implementation","refactor","patch",
    "traceback","stacktrace","exception","error","analyze","analysis","investigate",
    "architecture","design","compare","benchmark","optimize","optimise","review",
    "terminal","shell","tool","tools","pytest","test","tests","plan","planning",
    "delegate","subagent","cron","docker","kubernetes"}
  ```
  函数文档明说 "Conservative by design"（`smart_model_routing.py:62`）。
- `agent/rate_limit_tracker.py`（242 行）、`agent/usage_pricing.py` 独立成本模块。

**GenericAgent**：观测是**插件**，且用量靠 monkey-patch SSE 解析器（见 `docs/r4a-genericagent-research.md` §0.3 已记录的判断）。

### 7.2 本项目现在怎么做

**这是本项目最强的一块，逐项对标后结论是「已达甚至超过 OpenCode」**：

| 能力 | 本项目 | 行号 |
|---|---|---|
| 事件值（frozen dataclass，25 个） | ✅ | `core/events.py:26-353` |
| 事件不能携带大 payload | ✅ | `preview()` 500 字上限，`core/events.py:412-418` |
| span/parent 关系 | ✅ turn→i{n}→step{n}/call_id | `core/trace.py:612-625` |
| 子智能体事件带归属 | ✅ `ScopedEvent(scope, parent, event)` | `core/events.py:242-253` |
| **write-only 观测纪律** | ✅ 明确写进 `core/trace.py:1-14` 和 `core/agent_loop.py:30-33` | |
| 观测不 break turn | ✅ `on_event` 全 try/except + `failures` 计数 | `core/trace.py:487-493` |
| 事件失败可诊断 | ✅ `Degraded` 事件（把静默降级变成可见） | `core/events.py:158-168` |
| 脱敏 | ✅ 高置信模式 + KV 标签保留 | `core/trace.py:65-97` |
| 真实 usage 而非估算 | ✅ provider 的 `cached_tokens` 是真值，字符估算并列 | `core/events.py:113-131`、`core/budget.py:1-13` |
| 缺失数字保持缺失 | ✅ `as_int` 返回 `None` 不返回 0，注释解释为什么 | `domain/usage.py:26-32` |
| trace 文件写失败可诊断 | ✅ `TurnWriter` 达到上限发一条 `truncated` 标记后拒绝一切 | `core/trace.py:224-241` |
| 原子写 | ✅ tmp + `os.replace` | `core/trace.py:333-335` |
| 保留期清理 | ✅ | `core/trace.py:339-358` |
| OTLP 导出（父子 span） | ✅ 幂等 id（sha256 of session\|turn\|span） | `core/export_otlp.py:1-11` |
| 跨 session 聚合 | ✅ `trace overview`，含 `mismatch` 检测（summary 与磁盘文件数不符） | `core/aggregate.py:45-47` |

**缺什么**：

| 缺什么 | 影响 | 现状 |
|---|---|---|
| G7-1 无成本核算 | **中**。`budget.py` 有 `Pricing` 数据类（`core/budget.py:37-42`）和 `configured` 属性，但**没有任何地方调用它算钱**。用户不知道一轮花了多少钱 | 完全没有 |
| G7-2 无孤儿收敛 | 中。进程死在一轮中间，trace 里那个 `iteration` 之后什么都没有（但会话 checkpoint 已落盘，所以 transcript 完整） | 完全没有 |
| G7-3 无 provider 维度 | 低-中。`ProviderFallback` 事件有 from/to model，但 `summary.json` 不按模型分账 | 完全没有 |
| G7-4 无事件溯源 | 低。本项目的 session JSON 是直接写的（不是事件投影），这是有意的简化且我认为是对的 | 不做 |

**关于 G7-1 的具体判断**：`Pricing` 数据类已经写好并且写了注释说明为什么 `cached_per_1k` 不能省（「DeepSeek roughly a tenth」，`core/budget.py:36-37`），说明**当时想过但没做完**。补一个 `cost = f(usage, pricing)` 的纯函数 + 在 `summary.json` 里存一行，是 ~30 行的事。这是投入产出比最高的可观测性补齐。

**关于 G7-2 的具体判断**：本项目已经有 checkpoint（会话在每轮后落盘），所以「进程死了」的信息损失比 OpenCode 小。但 `summary.json` 的 `turns` 计数会和磁盘文件数不符——**而这个不符已经有检测代码**（`core/aggregate.py:45-47` 的 `mismatch`）。所以 G7-2 的一半已经免费拿到了。

### 7.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G7-1 无成本核算 | **高**（对一个把 M1/M2 挂在重试和降级上的系统，「这次降级省了多少钱还是多花了多少钱」是核心问题） | **做** | 1 文件（`core/budget.py` 加纯函数）+ `core/aggregate.py`/`core/trace.py` 各几行存/显示 |
| G7-2 无孤儿收敛 | 中 | 做（半成品已有，见上） | 1-2 文件 |
| G7-3 无 provider 维度归因 | 中。与 M2 直接相关：降级链上不同模型的单价不同，不分账就没法评估 M2 的收益 | **做**（和 G7-1 同批） | 1 文件 |
| G7-4 事件溯源 | 低 | **不做**。本项目是同步单进程，事件投影是为多投影场景付的复杂度 | — |
| G7-5 smart cheap/strong routing | 中 | **不做**。它不是编排层改进，是模型选择启发式，而且 M2 的降级链已经覆盖了「换个便宜的」这个需求 | — |

---

## 8. 任务规划 / 预算

### 8.1 参考系统怎么做

**OpenCode**：step 上限 + 上限时的 prompt 注入，而不是硬停。

```ts
// packages/core/src/session/runner/llm.ts:222-238
const stepLimitReached = loaded.agent.info.steps !== undefined && step >= loaded.agent.info.steps
const transcript = SessionModelRequest.baseTranscript({...})
const prepared = yield* context.request.primary({
  ...
  messages: stepLimitReached
    ? [...transcript.messages, Message.assistant(MAX_STEPS_PROMPT)]
    : transcript.messages,
  // Keep tool definitions on the final Step to preserve the provider's cached prefix.
  toolChoice: stepLimitReached ? "none" : undefined,
  ...
})
```

达到上限后**不是拒绝**，而是：注入一段硬约束 prompt（`session/runner/max-steps.ts:1-16`：「CRITICAL - MAXIMUM STEPS REACHED … MUST provide a text response summarizing work done so far」）+ `toolChoice: "none"`（禁工具）。**保留 tool definitions 是为了不破坏缓存前缀**——注释明说。

重复调用检测：**没有显式机制**。它靠 permission 层 + tool 层拦（`Tool.snapshot` 里的 `Tool is not available for this request`，`tool.ts:270`），以及 tool-result 会被压缩掉。

**Hermes**：iterations 上限 + 预算压力注入 + 去重

```python
# hermes/run_agent.py:6900-6914
if not self._budget_pressure_enabled or self.max_iterations <= 0: return
progress = api_call_count / self.max_iterations
remaining = self.max_iterations - api_call_count
...
f"[BUDGET WARNING: Iteration {api_call_count}/{self.max_iterations}. ..."
```

上限到达时请求模型输出总结（`run_agent.py:6956`）——和 OpenCode 同构。子智能体默认 50、父默认 90，且注释解释了为什么父子要不同（`run_agent.py:175-179`）。

重复调用去重是**单轮内**的（`run_agent.py:2987-3001`）：移除同一轮里 `(tool_name, arguments)` 相同的调用对。**跨轮不去重。**

**GenericAgent**：`max_turns=40`（`agent_loop.py:43`），纯计数，无预算注入、无去重。还有一条有意思的：**每 10 轮重置工具描述**（`agent_loop.py:56`，`client.last_tools = ''`）——工具 schema 是可增量的，不是每次全量重发。

### 8.2 本项目现在怎么做

**四个参考里最强，而且强在「不是单一上限」而是「一堆各自有预算的阶梯」。**

```python
# ai_coding/core/turn_policy.py:62-90
@dataclass(frozen=True)
class TurnPolicyConfig:
    max_iterations: int = 10
    stop_policy: str = "todo_aware"          # natural | todo_aware | strict
    max_todo_nudges: int = 2
    max_turn_tokens: int = 0                   # 0 = 无上限
    failed_streak_threshold: int = 2           # 0 关闭
    max_strategy_nudges: int = 1
    stall_iterations: int = 3                 # 0 关闭
    max_acceptance_nudges: int = 2
    max_ledger_nudges: int = 1
    max_auto_continuations: int = 3
```

五级阶梯（`core/turn_policy.py:184-272` 的 `decide_next_iteration`）：

1. **turn deadline** 最高优先（`:188-189`）
2. **iteration 上限** → 但如果还有 open todo 且 continuations 没超，**不是停下，是注入 continuation 换一轮**（`:214-223`）：
   ```python
   actionable = tuple(item for item in snapshot.open_items if item.status != "blocked")
   if actionable and snapshot.continuations < self.config.max_auto_continuations and not token_gate:
       return TurnDecision(TurnAction.NUDGE, "continue unfinished work", inject=self.continuation_message(actionable))
   ```
   `blocked` 项被排除在 continuation 之外，理由带实测：「measured: 6 model calls and 3 continuations for a single blocked item, identical to a pending one」（`:205-210`）。
3. **token 预算**（`:225-226`）—— 且它**阻止** continuation（`:190-195` 的 `token_gate`，注释说这是「M1 audit fix」）
4. **stall 检测**：连续 N 轮没有任何工具成功 → 提前停，留预算给 handoff（`:234-245`）
5. **strategy nudge**：连续失败两次 → 一次「换做法」的 nudge（`:247-257`）

再加两个「完成」的 gate（`decide_after_step`，`:290-321`）：todo 未闭合、acceptance 未通过——且 acceptance gate 有独立预算（`max_acceptance_nudges`）。

**重复调用检测有两个层次**：

- 跨 iteration：`DuplicateCallGuard`，本轮内同 fingerprint 超阈值 → DENY，指名早先 call（`core/guard.py:145-182`）
- 计划层：`plan_runner.py` 的机械裁决优先于模型声明 —— 「A step whose proof commands fail is `blocked` even if the model marked it done; a step the model left open without proof commands is `stalled` (retryable next run), never silently retried inside one run」（`core/plan_runner.py:9-13`）。一次修复机会然后停，重新规划是用户的显式决定（`:15-16`）。

**还有一条本项目独有的：预算耗尽不撒谎。**

```python
# ai_coding/core/agent_loop.py:317-337
if decision.action is TurnAction.FINISH:
    if decision.reason != "turn deadline exceeded" and pending_items(session.acceptance):
        await self._verify_acceptance(session, books)
        if session.acceptance and all(item.passed is True for item in session.acceptance):
            outcome = TurnOutcome(status=TurnStatus.COMPLETED, ..., reason="acceptance verified (post-budget)")
            break
```

预算死了但有证明命令 → 先跑一次验证，绿色的 suite 把 `MAX_ITERATIONS` 翻成 `COMPLETED` 而不是谎报未完成。

**缺什么**：

| 缺什么 | 影响 | 现状 |
|---|---|---|
| G8-1 预算接近上限时无模型侧提示 | 中。OpenCode/Hermes 都在接近上限时注入 warning，本项目只在自己的计数里知道 | 完全没有 |
| G8-2 达到上限时不给模型「总结收尾」的机会 | 中-低。本项目直接 FINISH，然后 `TurnCloser` 生成 harness 写的 handoff（`core/turn_close.py:210-220`）。模型没机会自己说「我做到了 X，剩下 Y」 | 完全没有 |
| G8-3 无单轮内的重复调用去重 | 低。`DuplicateCallGuard` 跨轮已覆盖主要场景 | 完全没有 |

### 8.3 差距与判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G8-1 预算压力注入 | **中高**。本项目有 6 个独立预算，模型对它们一无所知；它只能靠 harness 的 nudge 猜。加一句 warning（剩余 10% 时）成本极低，能让模型主动收束 | **做** | 1 文件（`core/turn_policy.py` 加一个 `TurnAction.NUDGE` 分支，或更简单：在 `core/request_assembly.py` 里挂一个 tail block） |
| G8-2 达上限时给模型收尾机会 | 中。本项目的 handoff 是 harness 生成的，质量必然低于模型自己写一句总结。OpenCode/Hermes 都做了这一步 | **做**（和 G8-1 同批）。注意：需要在 iteration 用完时再发一次模型调用，会多花一次预算——所以必须和 G8-1 一起设计，只在「还有 token 预算」时做 | 1-2 文件 |
| G8-3 单轮去重 | 低 | 不做 | — |

**注意 G8-1/G8-2 的取舍**：这两项都会**增加**模型调用次数，和「省 token」的目标相反。G8-1 是纯文本注入（零额外调用），G8-2 是多一次调用。G8-1 应该先做，G8-2 需要先测「多一次调用省下的 handoff 质量」是否值得。

---

## 9. 并发

### 9.1 参考系统怎么做

**OpenCode**：per-key 串行 + 跨 key 并行 + 可中断 + 等待原语

```ts
// packages/core/src/session/run-coordinator.ts:6-30
/** Serializes execution for each key while allowing different keys to run concurrently. */
export interface Coordinator<Key, E, Reason = never> {
  readonly active: Effect.Effect<ReadonlySet<Key>>
  readonly isActive: (key: Key) => Effect.Effect<boolean>
  readonly run: (key: Key) => Effect.Effect<void, E>
  readonly wake: (key: Key, scope?: Promotable) => Effect.Effect<void>
  readonly interrupt: (key, reason?, options?: { awaitSettlement?: boolean }) => Effect.Effect<boolean>
  readonly awaitIdle: (key: Key) => Effect.Effect<void>
}
```

`interrupt` 的语义写得极细：「Resolves once the interruption is accepted, not when cleanup settles」（`:17-19`）——即「我说停了」和「真的停了」是两个时刻，中间有窗口。这对本项目有直接价值，见下。

**工具并发**：OpenCode 的工具是 `Effect.all` 并发跑的（`session/runner/llm.ts` 里的工具批处理），并发上限我没在 core 里找到显式常量——它靠 permission 的 pending 表保证「同一时刻最多一个 ask」（`permission.ts:213`：`pending.has(request.id)` 就 die）。

**Hermes**：线程池（`hermes/tools/delegate_tool.py:338` 的 docstring 明说 "Called from within a thread"），工具并发在 run_agent 里。

### 9.2 本项目现在怎么做

**工具并发有上限，而且并发条件很保守**：

```python
# ai_coding/core/dispatch.py:224-231
def _can_run_parallel(self, prepared) -> bool:
    """Concurrency requires *every* call to be an ALLOW-tier safe tool."""
    for item in prepared:
        if item.tool is None or not item.tool.parallel_safe: return False
        if _verdict(item.decision) is not PermissionDecision.ALLOW: return False
    return True
```

**全批次并发或全批次串行**（`core/dispatch.py:192-214`），没有混合模式。理由写在模块 docstring：「so approval prompts can never interleave and DENY/STOP abort cleanly」（`core/dispatch.py:9-11`）。

上限是 `asyncio.Semaphore(max_parallel)`（`core/dispatch.py:193`），值来自 `settings.max_concurrent_tools`（`core/agent_loop.py:228`）。

**可中断，且中断会杀子进程**：

```python
# ai_coding/core/dispatch.py:436-450
kill = threading.Event()
ctx = replace(ctx, kill_event=kill)
try:
    return await run_with_deadline(tool.execute(call.params, ctx), timeout=budget, label=f"tool:{call.name}", cancel=self.cancel)
except RunCancelled:
    # The wait is over, but the work is not: set the flag so a subprocess
    # group is killed rather than left running (docs/r8-analysis.md S2.1).
    kill.set()
    return self._cancelled(call)
```

「一个 Escape 掉一批而不是掉整个 turn」（`core/agent_loop.py:195-201`、`:695-697`）。取消结果给模型的措辞是经过设计的：「Do not repeat it unchanged - change approach, ask, or wrap the turn up」（`core/dispatch.py:611-616`）。

**每次工具执行有硬 deadline**：`min(tool.max_duration_seconds, remaining turn budget)`（`core/dispatch.py:105-113`），超时返回**结构化**的 `tool_timeout` 而不是挂起（`core/dispatch.py:592-606`），注释说明原因：「so the declared tool_calls still pair up and the model can react」。

**子智能体并发**：semaphore（`core/subagent.py:125`），信号量在 worktree 之前拿（`core/subagent.py:168`）。

**缺什么**：

| 缺什么 | 影响 | 现状 |
|---|---|---|
| G9-1 无 turn 级重入保护 | **高**。见 §1 G1-1。并发写同一个 session 会破坏 tool_call 配对 | 完全没有（只有前端的 `input.disabled`） |
| G9-2 无「已接受中断 / 已完成清理」的两阶段区分 | 中。Esc 之后用户不知道 turn 是立刻停了还是要等当前工具的 kill 生效 | 完全没有 |
| G9-3 无 per-session 串行化原语 | 中。是 G9-1 的解法 | 完全没有 |
| G9-4 工具并发无混合模式 | 低。当前 all-or-nothing 是有意的简化，且注释给了理由 | 不做 |
| G9-5 子智能体中断不传播到 worktree 清理 | 低-中。`WorktreeManager.temporary` 是 async context manager，取消时会走 `__aexit__`——**本报告未验证** | 待验证 |

### 9.3 判断

| 差距 | 影响 | 值得做 | 成本 |
|---|---|---|---|
| G9-1 / G9-3 turn 级重入保护 | **高**。这是唯一一个「架构不变量只由前端保证」的缺口 | **做** | 1 文件（`core/agent_loop.py`：`process_input` 入口拿锁，拿不到就按 steering 排队） |
| G9-2 两阶段中断 | 中 | 暂不做。本项目是交互式 REPL/TUI，用户就在屏幕前，看得见 | — |
| G9-4 | 低 | 不做 | — |
| G9-5 | 低 | 验证后可做 | — |

---

## 10. 明确不建议搬的设计

### 10.1 Hermes 的单文件 `run_agent.py`（9877 行）

**不搬。** 理由：

1. **它的分层在别处**。`agent/` 下已经有 27 个模块（`error_classifier.py` 813 行、`context_compressor.py` 755 行、`credential_pool.py` 1211 行），分类和压缩是抽出来的。9877 行的是**编排胶水**——里面 400 多行是 401 的 provider 特定诊断文案（`hermes/run_agent.py:8205-8245`），几百行是各平台的消息格式适配。
2. **它的错误处理是嵌套 6 层 if**：`run_agent.py:8188` 的分类 → 凭据池 → codex 刷新 → nous 刷新 → anthropic 刷新 → thinking 签名 → 主重试路径（`:8400-8470`）。这是把「恢复策略」写成顺序代码而不是策略表。本项目的 `FailureKind` + `WORTH_ANOTHER_MODEL` + `_exhausted()` 三个纯函数（`ai/openai_engine.py:59`、`ai/openai_engine.py:69`）在可测试性上明显更好。
3. **可测试性无法对比**。本项目 `classify_failure` / `retry_delay` 是无 IO 纯函数（`ai/openai_engine.py:131` 的 docstring 明说 "Pure, and therefore testable"），可以穷举。`run_agent.py` 的恢复逻辑依赖 self 的十几个可变字段，无法隔离测试。

**该搬的是它的表和它的几个布尔 hint**（见 §2.3），不是它的编排。

### 10.2 OpenCode 的 Effect 架构

**不搬。** 它的 `SessionInbox`、`SessionCompaction`、`SessionRunner` 全是 `Effect.gen` + `Service`/`Layer`/`Node` 三件套，`session/context.ts:184-201` 的 `node` 声明了 17 个依赖。这是为多入口 / 多投影 / 大团队设计的依赖注入框架。本项目是同步 CLI，依赖关系在 `cli.py:71` 的 `_build_loop` 里显式构造，**更直接可读**。搬 DI 框架只会让启动路径变成一串 `Layer` 组合。

### 10.3 OpenCode 的 codemode 工具压缩

**不搬。** 见 §3.3。核心问题：它要求模型具备 code-execution 能力，需要独立的安全边界（模型写的代码怎么限制），需要重写全部工具的输入校验。对本项目 ~15 个内建工具的形态，收益不足以抵消复杂度。

### 10.4 OpenCode 的 provider 原生 compaction

**暂不搬。** 本项目的 engine 是纯 chat-completions（`ai/openai_engine.py:337` 的 `_stream`），没有 compaction 协议位。要支持需要改 engine 层，超出编排层。

### 10.5 GenericAgent 的字符数预算（`context_win * 3`）

**不搬。** `GenericAgent/llmcore.py:110` 的 `cap = sess.context_win * 3` 是一个字符启发式，本项目的 `estimate_tokens`（`core/context_compression.py:38-47`）虽然是启发式但区分了 CJK，已经更好。GA 的 `history.pop(0)` 直接丢最旧消息的做法也不可取——本项目的 L3/L2 层保证**任何被省略的内容都有可恢复的落盘路径**，这是本项目明确优于 GA 的地方。

### 10.6 gstack/openclaw（0 源文件）

**没有可对标的代码。** 4 个 markdown 规范片段 + 4 个 SKILL.md。唯一有价值的是一条**纪律**：`gstack-plan-CLAUDE.md:4` 的「Do not implement anything. This is planning only」+ `gstack-full-CLAUDE.md:8` 的「Do not ask for human input until the PR is ready for review」。

本项目的 `plan_runner.py` 用代码做到了同一件事并且**更强**：每步一个 bounded turn（实测省 ~30% token，`core/plan_runner.py:7-8`）、证明命令机械验收、一步修复后停（`:15-16`）。所以这条不需要搬。

它的 `agents-gstack-section.md:5` 有条规则值得注意：「Always spawn, never redirect」——编排层不把工作推回给用户。本项目的对应行为是 `plan_runner` 的「重规划是用户的显式决定」，方向一致。

---

## 11. 按投入产出排序的差距清单

### 11.1 立刻做（低成本、高影响）

| # | 项 | 影响面 | 成本（文件数） | 风险 |
|---|---|---|---|---|
| **1** | **G6-1 子智能体 guard 链为空** — 子智能体在隔离 worktree 里跑 bash，但没有 `BashPolicyGuard` / `PathEscapeGuard` / `DuplicateCallGuard`。父有子没有。 | 安全边界 | 1（`core/subagent.py`，加 `guards=` 实参 + runner 收参数）| **低**。guard 是纯函数无 IO，`GuardChain([])` → `GuardChain([...])` 是纯增量的；子智能体本来就用 `AutoApproveCallback`（`core/subagent.py:117`），deny 层不受影响 |
| **2** | **G9-1/G1-1 turn 级重入保护** — turn 进行中又来输入的闸门现在只在前端（`ui/textual_app.py` 的 `input.disabled`）。挪到 `AgentLoop`。 | 编排不变量 | 1（`core/agent_loop.py`，加锁 + 排队路径）| **低-中**。要决定「拿不到锁时怎么办」：按 steering 语义排队（推荐），还是直接拒绝。要小心不破坏现有 REPL/TUI 的行为 |
| **3** | **G7-1/G7-3 成本核算 + provider 维度归因** — `Pricing` 数据类已经写好（`core/budget.py:37`）但没人用。补一个纯函数 + 在 `summary.json` 存按模型分账。 | 可观测性 / M2 收益评估 | 1-2（`core/budget.py` 加函数，`core/aggregate.py`/`core/trace.py` 各几行）| **低**。纯函数 + 一个新字段。风险是 `summary.json` 的 schema 变化需要兼容旧文件（已有 `read_summary` 的 try/except，`core/trace.py:308-317`） |
| **4** | **G8-1 预算压力注入** — 剩余 10% 预算时往 volatile tail 挂一句 warning。本项目有 6 个独立预算，模型全都不知道。 | 预算效率 | 1（`core/request_assembly.py` 加一个 tail block，或 `core/turn_policy.py` 加分支）| **低**。纯文本注入，零额外模型调用。但要注意别破坏 prompt cache（必须挂在 volatile tail，不能动 system prompt 或历史）|

### 11.2 值得做（中等成本、高影响，需要设计确认）

| # | 项 | 影响面 | 成本（文件数） | 风险 |
|---|---|---|---|---|
| **5** | **G5-1 provider 拒绝时压缩重试** — provider 报 400/413 时压缩一次并重放当前 step。现在是 `FATAL` → turn 直接结束。 | 长会话可靠性 | 2（`ai/openai_engine.py` 分类 + `core/agent_loop.py` 的 `_model_step` 重试分支）| **中-高**。会动 M1/M2 的异常分层。必须确认：`CONTEXT_OVERFLOW` 进 `classify_failure` 后，`_exhausted()` 该返回 `EngineError` 还是 `FallbackWorthwhile`？我的判断是 **`EngineError`**（换 provider 解决不了上下文超长），但这需要显式确认，因为 `_exhausted` 现在只看 `WORTH_ANOTHER_MODEL` 集合（`ai/openai_engine.py:59`） |
| **6** | **G4-1 always-allow 持久化 + G4-2/G4-3 resource 维度 + glob** — `_always_allow` 是内存 set，进程重启即失；`write`/`edit` 是 WARN 靠人，但人看不出会不会写 `.env`。 | 权限模型 / UX | 2-3（`domain/session.py` 存字段、`security/permission_gate.py` 加资源提取、新增 ~15 行的 glob util）| **中**。改变了用户的实际行为（重启后不再重新批准）。安全方向上是**收紧**（bash 从「完全不能 always」变成「只能 always 具体命令模式」），但需要确认 bash 的命令模式提取能复用 `BashPolicyGuard` 的子串逻辑还是需要更解析 |
| **7** | **G5-6 token 估算改进** — `estimate_tokens` 是字符启发式（CJK 1/2、其他 1/4），是 G5-1 的放大器。用「最近一次实测 + 之后按字符估」的混合口径。 | 压缩触发准确度 | 1（`core/context_compression.py` 或 `domain/usage.py`）| **低**。纯函数改写，可穷举测试 |

### 11.3 可以做（低成本、低-中影响）

| # | 项 | 影响面 | 成本（文件数） | 风险 |
|---|---|---|---|---|
| **8** | **G2-1/G2-2/G2-3 退避改进** — timeout 独立计数上限、退避加 jitter、`retry-after` 加绝对上限。三个小改动。 | turn 预算利用 | 1（`ai/openai_engine.py` 的 `retry_delay` 及其调用点）| **低**。三个纯函数改动，现有测试能覆盖 |
| **9** | **G6-4 `_STATUS_MAP` 保留 stopped/aborted** | 子智能体结果语义 | 1（`core/subagent.py:56-66`）| **低**。但会改 `tools/subagent_tool.py` 的渲染和 `CHILD_ERRORS`（`tools/subagent_tool.py:29-33`），需要同步 |
| **10** | **G5-3 L4 摘要形状校验 + 一次 nudge** | 压缩质量 | 1（`core/context_compression.py` 的 `_run_l4`）| **低**。多一次模型调用，需权衡 |
| **11** | **G1-2 消息幂等** — `process_input` 接一个可选 `input_id`，重复时返回缓存 outcome。 | 计费正确性 | 1（`core/agent_loop.py` + `domain/session.py`）| **低** |
| **12** | **G7-2 孤儿收敛** — 进程死后把未完成的 iteration / compaction 标成 interrupted。`mismatch` 检测已有（`core/aggregate.py:45-47`），一半免费 | 可观测性 | 1-2 | **低** |

### 11.4 现在不做（附理由）

| 项 | 理由 |
|---|---|
| **G1-4 持久化 inbox 表 + doorbell 协调器** | OpenCode 那套 750 行 TS 是为多入口异步系统设计的（server/TUI/desktop/cron/gateway 五个投递方）。本项目是单进程同步 CLI，**只有一个投递方**。搬过来是纯复杂度增加。如果加了 IDE/web 前端或 `/plan` 之外的异步任务，再谈 |
| **G2-4 凭据池轮换** | 需要改 `ModelConfig` schema（一个 model 名下挂多个 key），影响面超出编排层。是配置层的事 |
| **G4-4 完整 action/resource/effect 三元组** | 需要 ruleset 合成 + 优先级 + agent 级配置三个本项目用不上的概念。取必要子集（#6）即可 |
| **G6-3 子智能体 resume** | 需要持久化子会话 + 恢复语义，3+ 文件独立设计 |
| **G7-4 事件溯源** | 本项目是同步单进程，事件投影是为多投影场景付的复杂度。直接写 session JSON 是有意的简化 |
| **G9-2 两阶段中断** | 交互式 REPL/TUI，用户就在屏幕前，看得见 |
| **G3-4 schema 动态化** | 冻结是 prompt cache 稳定的前提（实测 92-93% vs 26% 命中率，`core/context_compression.py:389-391`）。动态化会摧毁这个 |
| **G7-5 smart cheap/strong routing** | 不是编排层改进，是模型选择启发式。M2 的降级链已覆盖「换个便宜的」这个需求 |

---

## 12. 实际读过的文件

**本项目（`/home/linsir365/projects/ai-coding`，22514 行 Python）**

```
ai_coding/core/agent_loop.py          全文 924 行
ai_coding/core/dispatch.py            全文 622 行
ai_coding/core/guard.py               全文 295 行
ai_coding/core/turn_policy.py         全文 394 行
ai_coding/core/turn_close.py          全文 252 行
ai_coding/core/compaction.py          全文 121 行
ai_coding/core/context_compression.py 全文 468 行
ai_coding/core/subagent.py            全文 619 行
ai_coding/core/events.py              全文 449 行
ai_coding/core/trace.py               全文 625 行
ai_coding/core/observers.py          结构（ScopedObserver/Braid/SafeObserver）
ai_coding/core/steering.py            全文 59 行
ai_coding/core/request_assembly.py    前 60 行
ai_coding/core/budget.py              前 60 行
ai_coding/core/aggregate.py           前 50 行
ai_coding/core/export_otlp.py         前 60 行
ai_coding/core/plan_runner.py         前 80 行 + def 列表
ai_coding/core/mcp.py                 结构 + open_all
ai_coding/core/hooks.py               HookPoint + CAPABILITIES
ai_coding/ai/openai_engine.py         全文 421 行
ai_coding/ai/fallback_engine.py       全文 127 行
ai_coding/ai/factory.py               全文 65 行
ai_coding/ai/engine.py                结构（ChatEngine/EngineError/FallbackWorthwhile/on_retry/on_fallback）
ai_coding/security/permission_gate.py 全文 178 行
ai_coding/security/approval.py        全文 165 行
ai_coding/tools/schema.py             全文 47 行
ai_coding/tools/registry.py           全文 41 行
ai_coding/tools/builder.py            前 60 行
ai_coding/tools/subagent_tool.py      前 80 行
ai_coding/ui/modals.py                前 60 行（ALWAYS_EXCLUDED）
ai_coding/ui/textual_app.py           grep always_allow 上下文
ai_coding/ui/repl.py                  grep process_input 上下文
ai_coding/cli.py                      grep process_input / _llm_helpers_for
ai_coding/domain/usage.py             前 50 行
ai_coding/domain/session.py           前 60 行
docs/r4a-genericagent-research.md     前 80 行（确认已有调研记录）
```

**OpenCode（`/home/linsir365/projects/opencode`）**

```
packages/core/src/session/inbox.ts              全文 550 行
packages/core/src/session/runner/retry.ts       全文 115 行
packages/core/src/session/compaction.ts         全文 948 行
packages/core/src/session/runner/step.ts        前 200 行 + Outcome 定义
packages/core/src/session/runner/llm.ts         190-340 行
packages/core/src/session/runner/index.ts       全文 42 行
packages/core/src/session/runner/max-steps.ts   全文 16 行
packages/core/src/session/run-coordinator.ts    前 90 行
packages/core/src/session/execution.ts          140-190 行
packages/core/src/session/session.ts            100-200 行
packages/core/src/session/context.ts            100-208 行
packages/core/src/session/to-session-error.ts   全文 105 行
packages/core/src/permission.ts                 全文 347 行
packages/core/src/permission/saved.ts           全文 88 行
packages/core/src/tool.ts                       150-290 行（snapshot）
packages/core/src/util/wildcard.ts              全文 14 行
packages/core/src/agent.ts                      40-100 行
packages/core/src/tool/plugin/shell.ts          100-145 行
packages/core/src/tool/plugin/subagent.ts      100-175 行
packages/schema/src/permission.ts               Rule/Ruleset 定义
```

**GenericAgent（`/home/linsir365/projects/GenericAgent`）**

```
agent_loop.py     全文 133 行
llmcore.py        52-130 行（compress_history_tags / trim_messages_history）
                  315-330 行（_record_usage）
                  370-420 行（_stream_with_retry）
                  448-460 行（_prepare_oai_tools）
agentmain.py      17-22 行（BANNED_TOOLS）
docs/             4 个安装文档，无设计文档
```

**Hermes（`/home/linsir365/.hermes/hermes-agent`）**

> 上一版文档说「本沙箱无可核验材料」。**现核实：目录存在，非 venv 部分有 711 个文件。** 但需注意 `venv/` 下有 21499 个 .py 文件，绝大多数是第三方依赖（openai / anthropic / tenacity / mistralai 等），不是 Hermes 自己的代码。本报告只读 `venv/` 之外的项目文件。

```
agent/retry_utils.py            全文 57 行
agent/error_classifier.py       前 120 行（FailoverReason 18 项 + ClassifiedError）
agent/smart_model_routing.py    前 80 行
agent/credential_pool.py        结构（acquire_lease 在 768 行）
agent/context_compressor.py     结构（class/def 列表）+ 612-680 行
tools/delegate_tool.py          108-140 行、338-400 行、结构列表
toolsets.py                     185-230 行、255-300 行
run_agent.py                    8180-8280 行、8380-8470 行
                                grep: failover / max_iterations / dedup / fallback_index
cron/{jobs,scheduler}.py        结构
gateway/session.py              结构
```

**gstack/openclaw（`/home/linsir365/gstack/openclaw`）**

```
0 个源文件
agents-gstack-section.md        前 20 行
gstack-full-CLAUDE.md           全文
gstack-lite-CLAUDE.md           全文
gstack-plan-CLAUDE.md           全文
skills/                         4 个 SKILL.md（仅目录列表）
```

---

## 13. 一句话结论

本项目的编排层**在「一轮之内」比三个参考系统中的两个都强**（纯函数错误分类、硬不变量压缩顺序、fail-closed guard、诚实度纪律、预算阶梯），差距集中在**「一轮之外」和「边界上」**：入口没有编排层保证、工具面在启动时冻结且权限只挡执行不挡可见、权限模型没有 resource 维度也没有持久化、子智能体拿到了工作区隔离却没拿到策略隔离。

**最高优先级不是补功能，是修一个现成的漏洞**：子智能体的 guard 链是空的（`ai_coding/core/subagent.py:452-461` 构造 `AgentLoop` 时未传 `guards`，而 `core/dispatch.py:163` 会把 `None` 变成空链）。这一项一个文件、十几行、修完之后整个 guard 层对并发子智能体生效。

---

## 14. 提炼

**可复用原则**

1. **「编排不变量不该由前端保证」** —— 本项目 turn 重入保护只在 `ui/textual_app.py` 的 `input.disabled`，REPL 侧则靠 `await` 阻塞。任何「前端做了正确的事所以后端不需要做」的推理，在换 UI 时都会失效。判据：**如果换掉 UI，这个不变量还在吗？** 不在，就属于编排层。
2. **策略缺失和策略为「全放行」在行为上等价，但只有一个是真漏洞** —— `AgentLoop(guards=None)` → `GuardChain([])` → 恒定 ALLOW，代码读起来像「没配置」，运行起来是「全部通过」。当某个组件的参数默认 `None` 且 `None` 的语义是「不检查」而不是「不配置」，默认值就是错的。
3. **可测试性的判据是「能不能穷举」** —— 本项目 `classify_failure` / `retry_delay` 是无 IO 纯函数所以能穷举测试；Hermes 的 `run_agent.py` 恢复逻辑依赖 self 的十几个可变字段所以不能。判断一个模块的编排设计好不好，看它的决策能不能被表驱动测出来。
4. **「当时想过但没做完」是可查信号** —— `Pricing` 数据类带完整注释却零调用点（`core/budget.py:37`），说明设计决策已做、实现未至。这类残留比缺失更危险，因为它看起来已完成。