# M2 模型降级链 — 设计文档

- 日期：2026-10-07
- 状态：已拍板，待实现
- 前置：M1（provider 重试可观测，`cea2d87` 之后的未提交改动）

---

## 1. 要解决的问题

M1 落地后，一次模型调用失败的最坏路径是：同一模型退避重试 4 次（最长 254 秒），耗尽后整个回合以 `ERROR` 结束。用户换无可换，只能自己改配置重启。

M2 让这件事在**本回合内**自动恢复：模型 A 的预算耗尽后换模型 B 重跑同一个 model step，B 成功则回合正常收尾。

## 2. 已拍板的决策

| 决策点 | 选择 |
|---|---|
| 切换的生命周期 | **仅本回合内切换**，无跨回合粘滞，无持久状态 |
| 触发条件 | **按「换模型能否解决」划分**，不是按现有 `FATAL` 布尔值 |
| 配置形状 | 每个模型挂 `fallback_models`，默认为空 |
| 承载位置 | 引擎层，`FallbackEngine` 组合多个 `OpenAIChatEngine` |
| 可见性 | 新增 `ProviderFallback` 事件（不复用 `Degraded`）→ transcript 立即出 info 行；状态栏模型名在回合结束时更新 |
| 异常契约 | `FallbackWorthwhile(EngineError)`，`test_an_authentication_error_is_not_retried` 的断言收窄为接受该子类 |

## 3. 承重事实（实施前已核验）

| # | 事实 | 出处 | 影响 |
|---|---|---|---|
| F1 | `EngineError` 一词二义：重试耗尽（可降级）vs 已吐字后流断（绝不可降级） | `ai_coding/ai/openai_engine.py:265` / `:349` | 必须拆异常类型 |
| F2 | 状态栏在 `run_command` 的 `finally` 读 `engine.model_config.name` | `ai_coding/ui/textual_app.py:237` | `FallbackEngine` 必须暴露 `model_config` |
| F3 | `AsyncOpenAI` client 在引擎构造期绑定 `base_url`/`api_key`/`timeout` | `ai_coding/ai/openai_engine.py:196-201` | 每个降级目标必须是独立引擎实例，各自持有 client |
| F4 | 架构文档明写「模型调用失败不降级」 | `docs/architecture.md:168` | 本设计推翻该立场，须同步改文档 |
| F5 | `AgentLoop.set_model` 是死代码（`AIConfig` 无 `models` 字段，生产路径恒抛 `ValueError`） | 实测 `hasattr(AIConfig(), "models") is False` | **不在 M2 范围** |

## 4. 方案选择与否决理由

考虑过三个方案：

- **A. 把降级循环塞进 `OpenAIChatEngine.step()` 内部。** 否决。`kwargs`（含 `model`/`max_tokens`/`temperature`/`top_p`）在 `openai_engine.py:232` 一次性构建，换模型必须重建；`_client` 也绑死在第一个模型的 `base_url`。等于把已验证的 client 生命周期管理重新打开，`step()` 会退化成 God method。
- **B. `FallbackEngine` 组合多个 `OpenAIChatEngine`。** **采用。** 每个子引擎对自己的模型负全责（client、kwargs、部分输出守卫），`OpenAIChatEngine` 内部一行不改。降级逻辑只存在于新文件。
- **C. 循环层捕获 `EngineError` 后改 `engine.model_config` 重调。** 否决。F3 已核验：改属性不重建 client，会打旧端点用旧 key。要修正就得额外实现 client 重建，反而多一块代码，且把降级策略散进循环层。

## 5. 异常契约变更

`ai_coding/ai/engine.py` 新增：

```python
class FallbackWorthwhile(EngineError):
    """本模型已尽力，换一个值得试。"""
```

继承 `EngineError` 是必需的：`core/agent_loop.py:442` 的 `except (DeadlineExpired, EngineError)` 必须继续兜住它，使「所有模型都耗尽」最终表现为 `TurnStatus.ERROR`。

`openai_engine.py:258` 的二分类改为三分类：

| FailureKind | 同模型内重试 | 换模型 | 理由 |
|---|---|---|---|
| `RATE_LIMITED` | 是（退避） | 预算耗尽后 | 限流是容量问题 |
| `SERVER_ERROR` | 是（退避） | 预算耗尽后 | 5xx 是服务端问题 |
| `TRANSPORT` | 是（退避） | 预算耗尽后 | 网络抖动 |
| `AUTH_ERROR`（新增，401/403） | **否** | **立即** | 坏 key 不会自己变好，重试是浪费；换家可能行 |
| `FATAL`（400 / 上下文超长等） | 否 | 否 | 请求本身不合法，换谁都一样 |

`classify_failure` 增补：401/403 → `AUTH_ERROR`，其余 4xx 仍为 `FATAL`。

### F1 的安全保证

`_stream` 在已吐字/吐 tool delta 后抛的是**普通** `EngineError`（`:349`）。`FallbackEngine.step` 只捕获 `FallbackWorthwhile`，因此已吐字路径**绝不进入降级**——第二个模型不会重复吐字、不会拼接半个 tool call。这是整个设计最关键的不变式，须有专门测试钉住。

## 6. 数据流

### 6.1 降级成功

```
用户输入
 └─ AgentLoop._model_step → engine.step(...)
     └─ FallbackEngine.step(...)
         ├─ engines[0].step() → 429 × 4 → FallbackWorthwhile
         ├─ on_fallback(from=qwen, to=deepseek, kind=rate_limited)
         │   └─ AgentLoop._restate_fallback → ProviderFallback 事件
         │       ├─ blocks_from_event → Block("info", "⤬ 降级 …", "dim")
         │       └─ TraceRecorder → 落盘（走 `_describe` 的通用 dataclass 分支，无需改 trace.py）
         └─ engines[1].step() → 成功 → StepResult
 └─ 回合继续；回合结束时 textual_app 的 finally 读 FallbackEngine.model_config
     → 状态栏显示 deepseek-chat（F2 满足）
```

### 6.2 已吐字后失败

```
engines[0].step() → 吐出 3 个 text chunk → 流断
 └─ _stream:349 raise EngineError（非 FallbackWorthwhile）
     └─ FallbackEngine.step 不捕获 → 上抛 → 回合 ERROR
```

### 6.3 全部耗尽

```
engines[N-1].step() → FallbackWorthwhile，且已到最后一个
 └─ FallbackEngine.step 上抛 FallbackWorthwhile（message 补入完整链）
     └─ agent_loop:442 → TurnStatus.ERROR
```

## 7. 组件与接口

### `FallbackEngine`（新增 `ai_coding/ai/fallback_engine.py`）

| 成员 | 读 | 写 | 作用 |
|---|---|---|---|
| `_engines: tuple[OpenAIChatEngine, ...]` | — | 构造 | 链，构造时即全部创建（各自持有 client，满足 F3） |
| `_active: int` | 内部 | 内部 | 当前活跃下标 |
| `model_config` | ✅ | ✅ | 读：`_engines[_active].model_config`（满足 F2）。写：跳到链中 `model_config` 相同的那个引擎，不在链中则 `ValueError` |
| `on_retry` | — | ✅ | setter 广播给所有子引擎，保持 M1 契约 |
| `on_fallback` | — | ✅ | setter 广播给所有子引擎，由 `AgentLoop.__init__` 订阅 |
| `step()` | — | — | 遍历链，仅捕获 `FallbackWorthwhile`；切换前触发 `on_fallback` |
| `aclose()` | — | — | 关闭所有子引擎的 client |

**只读还是可写——一个自查出来的隐患。** 初稿把 `model_config` 定为只读，理由是"`set_model` 是死代码，保持原状"。这是错的：只读 property 并非"保持原状"，而是把一个**原本无害的死代码**变成**会炸的雷**。`AgentLoop.set_model`（`core/agent_loop.py:256`）执行的是赋值语句；一旦有人修复 F5（`AIConfig` 补 `models` 字段），该行将开始执行，届时只读 property 会抛 `AttributeError` ——且**只在降级链场景下崩**，单引擎引擎不受影响。这种"配置一改就炸、且炸在别处"的隐患必须在本次设计里根除，因此 setter 的语义定义为「跳到链中对应模型的引擎」，约 8 行。

**两个回调都只写不读。** `on_retry` / `on_fallback` 的 getter 在全项目没有任何消费者（唯一读取发生在子引擎内部读自己的属性）。因此不实现 getter——写了就是无人调用的代码。

### `ProviderFallback` 事件（新增 `ai_coding/core/events.py`）

```python
@dataclass(frozen=True)
class ProviderFallback:
    from_model: str
    to_model: str
    kind: str
    reason: str = ""
```

不复用 `Degraded` 的理由：`Degraded` 的语义是「增强子系统放弃并回退」，其 `/trace` 汇总会把模型切换与记忆子系统降级混为一谈；且用户对两者的反应截然不同（一个无所谓，一个意味着"你现在用的是另一个模型"）。新事件与 M1 的 `ProviderRetried` 配对。

### `AgentLoop` 接线（与 M1 同构）

`__init__` 中若引擎存在 `on_fallback` 属性则订阅 `_restate_fallback`；`_restate_fallback` 构造 `ProviderFallback` 并 `_emit`。`reason` 截断至 200 字符（与 `_restate_engine_retry` 一致）。

## 8. 配置契约

`ModelConfig` 新增 `fallback_models: tuple[str, ...] = ()`。

```yaml
models:
  qwen:
    name: qwen3.8-2.4t-a95b
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: sk-...
    fallback_models: [deepseek-chat]     # 新增；省略即为空 = 不降级
  deepseek-chat:
    name: deepseek-chat
    base_url: https://api.deepseek.com/v1
    api_key: sk-...
default_model: qwen
```

标识用 `models` 字典的**键名**，不是 `ModelConfig.name`（两者可以不同，如 `qwen` → `qwen3.8-2.4t-a95b`）。

`build_engine_from_config` 在启动时解析链，用 `visited` 集合检测环；链中任一名字不存在即抛 `ValueError`——配置错误必须在启动时暴露，不能拖到运行时。链长为 1 时**直接返回单个 `OpenAIChatEngine`**，保持现有行为逐字节相同。

## 9. 逐文件实现步骤

| # | 文件 | 步骤 |
|---|---|---|
| 1 | `ai_coding/domain/model_config.py` | 加 `fallback_models: tuple[str, ...] = ()` |
| 2 | `ai_coding/config/models.py`（`_coerce_model`） | 解析 `fallback_models` → `tuple(str(x) for x in ...)` |
| 3 | `ai_coding/ai/engine.py` | 加 `FallbackWorthwhile`，补 `__all__` |
| 4 | `ai_coding/ai/openai_engine.py` | ①`FailureKind` 加 `AUTH_ERROR` ②`classify_failure` 401/403→`AUTH_ERROR` ③`:258` 二分类改三分类 |
| 5 | `ai_coding/ai/fallback_engine.py`（新建，约 60 行） | `FallbackEngine(ChatEngine)` |
| 6 | `ai_coding/ai/factory.py` | 解析链 + 环检测 + 链长 1 短路 |
| 7 | `ai_coding/core/events.py` | 加 `ProviderFallback`，进 union + `__all__` |
| 8 | `ai_coding/core/agent_loop.py` | 订阅 `on_fallback` + `_restate_fallback` |
| 9 | `ai_coding/ui/blocks.py` | `ProviderFallback` → `Block("info", …, "dim")` |
| 10 | `docs/architecture.md:167-168` | 改写立场 |
| 11 | `docs/data-contracts.md:72` | 补 `fallback_models`、`AUTH_ERROR` |

**明确不改**：`ai/__init__.py`（不导出 `FallbackEngine`，无外部消费者）、`core/trace.py`（通用分支自动支持）、`core/turn_policy.py`、任何既有断言的内容。

## 10. 架构图

```
              ┌──────────────── AgentLoop ──────────────────┐
              │  engine.on_fallback = self._restate_fallback │ ← 新增（与 M1 同构）
              │  engine.on_retry    = self._restate_retry    │ ← M1 已有
              └────────────────────┬──────────────────────────┘
                                   │ step(messages, tools, on_delta)
              ┌────────────────────▼─────────────────────┐
              │         FallbackEngine(ChatEngine)       │ ← 新文件
              │  model_config ──► _engines[_active]      │ ← F2
              │  on_retry ──► 广播到所有子引擎             │ ← M1 契约
              │  step(): for i, e in enumerate(_engines): │
              │      try:  return await e.step(...)       │
              │      except FallbackWorthwhile:           │
              │          on_fallback(...); _active = i+1  │
              │      except EngineError: raise           │ ← F1 安全闸
              │  aclose(): 关闭所有子引擎 client           │ ← F3
              └──┬───────────────────────────────┬────────┘
       ┌─────────▼──────────┐        ┌───────────▼─────────┐
       │ OpenAIChatEngine  │        │ OpenAIChatEngine    │
       │  qwen             │        │  deepseek-chat      │
       │  client@aliyun    │        │  client@deepseek    │ ← F3 各自绑定
       │  三分类 + 重试循环 │ ← 改动 │  （零改动）         │
       └───────────────────┘        └─────────────────────┘
                 │  │
    on_retry / on_fallback ──► ProviderRetried / ProviderFallback
                                      │
                      ┌───────────────┴──────────────┐
                      ▼                              ▼
        blocks_from_event                    TraceRecorder._describe
        Block("info","⤬ 降级…","dim")        通用 dataclass 分支，自动落盘
```

## 11. 成功判据

**改前**：qwen 连续 429 → 退避 14 秒 → 回合 ERROR，用户只见重试行后突然失败。

**改后**：qwen 连续 429 → `⤷ 重试` ×3 → `⤬ 降级 qwen → deepseek（rate_limited）` → deepseek 正常作答 → 回合结束时状态栏显示 `deepseek-chat`。

**回归保证**：现有 `config.local.yaml` 未声明 `fallback_models`，链长为 1，`factory.py` 短路返回单引擎，行为与改动前完全一致。

## 12. TDD 用例清单

### `tests/test_openai_engine.py`（改 1，增 4）

| 用例 | 类型 | 断言要点 |
|---|---|---|
| `test_classify_failure_uses_status_codes_not_just_sdk_classes` | **改** | 表中加 401→`AUTH_ERROR`、403→`AUTH_ERROR`、400→`FATAL` |
| `test_an_authentication_error_is_not_retried` | **改** | 异常类型收窄为 `FallbackWorthwhile`，`match="auth"` 保留 |
| `test_auth_error_is_not_retried_but_is_fallback_worthwhile` | 新 | 401 只调 1 次，抛 `FallbackWorthwhile` |
| `test_exhausted_retries_raise_fallback_worthwhile` | 新 | 429 ×4 → `FallbackWorthwhile` |
| `test_a_bad_request_is_not_fallback_worthwhile` | 新 | 400 → 普通 `EngineError` |
| `test_a_failure_after_text_started_is_not_fallback_worthwhile` | 新 | 已吐字 → 普通 `EngineError`（**F1 不变式**） |

### `tests/test_fallback_engine.py`（新建，9 个）

| 用例 | 断言要点 |
|---|---|
| `test_it_returns_the_first_models_result` | 链长 2，第一个成功 → 只调 1 次 |
| `test_it_moves_to_the_next_model_when_one_is_exhausted` | A 抛 `FallbackWorthwhile` → B 成功 |
| `test_it_stops_at_a_fatal_error` | A 抛 `EngineError` → B 从未被调用 |
| `test_it_stops_when_output_already_started` | A 抛已吐字 `EngineError` → B 从未被调用 |
| `test_it_raises_when_every_model_is_exhausted` | 抛 `FallbackWorthwhile`，message 含两个模型名 |
| `test_aclose_releases_every_engine` | 所有子引擎 `aclose()` 均被调用 |
| `test_model_config_reflects_the_active_engine` | 切换后 `model_config` 是 B 的（F2） |
| `test_on_retry_reaches_every_sub_engine` | 广播生效（M1 契约） |
| `test_a_chain_of_one_behaves_like_the_engine_itself` | 单引擎行为等价 |

### 其他

| 文件 | 用例 |
|---|---|
| `tests/test_config_loader.py` | `fallback_models` 正确解析；缺省为 `()` |
| `tests/test_ai_factory.py` | 链长 1 → `OpenAIChatEngine`；链长 2 → `FallbackEngine`；名字不存在 → `ValueError`；环 → `ValueError` |
| `tests/test_loop_events.py` | `ProviderFallback` 在 union 中 / frozen / `blocks_from_event` 出一行 info |

### 真实路径验证（测试不可替代）

用假 provider（`_SequencedClient`）经由真实 `AgentLoop` + 真实 `FallbackEngine` + 真实 `TraceRecorder` 跑通一次降级，确认：重试事件、降级事件、dim info 行、trace 落盘、状态栏最终模型名五者一致。

## 13. 明确不做

| 不做 | 理由 |
|---|---|
| 跨回合粘滞 | 已拍板「仅本回合内切换」 |
| 修复 `set_model`（F5） | 独立根因（`AIConfig` 缺 `models` 字段），与降级链无关 |
| 运行期环检测 | 环在 `factory` 解析配置时一次性检测并拒绝，启动即失败 |
| 重试趋势聚合 | M1 已定不做 |
| `ai/llm_helpers.py` 的 L4 摘要加降级 | 它自建 client、不经引擎，是独立路径 |
| 导出 `FallbackEngine` 到 `ai/__init__.py` | 仅 `factory.py` 内部使用，无外部消费者 |

## 14. 风险

| 风险 | 缓解 |
|---|---|
| 已吐字后换模型导致内容重复/工具调用拼接 | `FallbackWorthwhile` 与 `EngineError` 的类型区分 + 专门测试钉住（§12 F1 不变式） |
| 降级目标配置错误拖到运行时才暴露 | `factory` 启动时解析 + 环检测，配置错误即 `ValueError` |
| 现有配置行为变化 | 链长 1 时短路返回单引擎；`config.local.yaml` 未声明该字段，链长为 1 |
| 子 agent 是否继承降级链 | `subagent.py:452` 复用同一个 engine 对象，自动继承，无需改动 |