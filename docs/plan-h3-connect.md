# H3 实施方案：安装分发 + `/connect`（模型自带搭配与自由切换）

> 状态：v3（v1/v2 各经两名评审员只读评审；v2 的 4 个 P0 与 11 个 P1 已逐条核对并修入）
> 范围：两条独立工作流，同一份文档
> A. **安装分发**——让 `pip install` / `uv tool install` 出来的 `ai-coding` 真的能跑起 TUI
> B. **`/connect` 与自由切换**——TUI 里自己搭配模型、自己切换模型，换 provider 不再打到旧端点

---

## §0 一句话结论

**A**：唯一的打包阻塞是"前端在包外"（`launcher.py` 用 `parents[2]/"frontend"` 定位源码树）。把 `frontend/` 移进包内、声明为 package-data、首次运行拷到 `data_home()` 并 `bun install`，`pip install` + 机器上有 `bun`（首次联网装依赖）即可用；版本三处不同步、CI 从不跑前端测试两个附带问题一并收掉。

**B**：`/connect` 是"provider 列表 → 认证方式列表 → key → 模型名"的列表驱动多步（opencode 形状），落盘到 `~/.config/ai-coding/models.local.yaml`（0600，与 `mcp.local.yaml` 同构，但形状是 **map**）；`/models` 打开列表（`model.list` 现取）。**同时必须修掉一个真缺陷簇**：`AgentLoop.set_model` 只改 `engine.model_config`，而 `OpenAIChatEngine._client` 在构造时就固定了 `base_url`/`api_key`——今天跨 provider 切换会**静默把请求发到旧端点、带旧 key**；同一个缺陷还藏在压缩摘要与记忆抽取里（`cli.py:171` 把 `model_config` 烘进了闭包）。切换改为重建引擎 + 重新指向这两个 LLM helper。

---

## §1 现状核实（全部实测，非推断）

### 1.1 切换模型：今天跨 provider 是错的，而且错在三个地方

| 位置 | 事实 |
|---|---|
| `core/agent_loop.py:463-475` | `set_model` 全部实现就是 `self.engine.model_config = self.models[name]` |
| `ai/openai_engine.py:295-301` | `self._client = client or AsyncOpenAI(base_url=model_config.base_url, api_key=model_config.api_key, ...)`，只构造一次 |
| `ai/openai_engine.py:410` | 请求走 `self._client.chat.completions.create(...)` |
| `cli.py:171` + `cli.py:321-328` | `_llm_helpers_for(engine)` 把 `engine.model_config` **烘进闭包**，产出 `summarizer` / `extractor` |
| `ai/llm_helpers.py:298,308,354` | 两个闭包每次调用都用那个烘死的 `model_config` 新建 client |
| `core/context_compression.py:590` / `memory/service.py:296` | 两个 helper 存进 `ContextManager._summarizer` / `MemoryService._extractor`（**私有字段，无公开 property**） |

推论（可复现）：`deepseek-chat` → `/model gpt-5` 之后，`model_config` 变成 gpt-5，`_client` 仍指向 `api.deepseek.com` 且带 DeepSeek 的 key；**压缩摘要与记忆抽取也仍然打到 DeepSeek**。命令打印 `[model] switched to gpt-5`，行为是假的。

| 位置 | 事实 |
|---|---|
| `ai/fallback_engine.py:43-52` | `FallbackEngine` 只暴露 `model_config`，**没有** `base_url` |
| `ai/fallback_engine.py:54-74` | `model_config` 的 setter 只把链内某成员提到首位；链外模型直接 `ValueError`（4 个用例锁着，`tests/test_fallback_engine.py:159,168,262,277`） |
| `ai/factory.py:40-65` | `build_engine_from_config` 是唯一的引擎装配点（`world.py:159` 调一次；`tools/builder.py:117` 给子智能体另建一个） |
| `core/agent_loop.py:324` | `self.models = dict(models or {})`——**浅拷贝**，运行时新增的模型不会自己进来 |
| `core/agent_loop.py:376-381` | `engine.on_retry` / `engine.on_fallback` 由 loop 在 `__init__` 里挂上；重建引擎若不重挂，重试/回退事件全部静默 |
| `core/agent_loop.py:255-256,293-294` | `context` / `memory` 是可选的（默认 `None`），`test_model_switching.py:42-53` 就没传 |

### 1.2 TUI 侧：列表是启动时的快照

| 位置 | 事实 |
|---|---|
| `server/handlers.py:450-470` | `initialize` 返回 `models: [{name, key}]`，来源 `world.config.models`，**只此一次**（`tests/test_server_ipc.py:279` 锁着这个键） |
| `frontend/src/app.tsx:357` | `<DialogModels models={props.hello.models} ...>`——启动快照，之后永不变 |
| `server/handlers.py:1083-1093` | `model.set` 存在；**没有** `model.list`，也**没有**任何"新增模型"的 RPC |
| `server/handlers.py:163-173` | `IpcConsole` 明确没有 `ask`（"the command surface never asks"）→ 向导不能由后端 slash 处理器驱动 |
| `server/handlers.py:890-910` | `session.command` 只收 `{id, text}` → 同上，多步交互只能在 TUI 里做 |
| `frontend/src/util/submission.ts:39-46` | 一行是不是命令，**只信后端的 `slash.list`**（`app/slash.py::_ALIASES`） |
| `frontend/src/util/slash.ts:42-47` | 目录里叫 `models`（复数），后端 `app/slash.py:38` 叫 `model` |
| `frontend/test/slash.test.ts:73` | `filterSlashCommands("mod")[0]!.name === "models"`——**加一条 `model` 会与它同分**，锁死 |
| `frontend/src/component/dialog-mcp.tsx:104-107` | 面板级动作的既有做法：`ctrl+a` + `SelectAction`（不能是裸字母，搜索框有焦点） |
| `frontend/src/component/dialog-mcp-add.tsx:66-160` | 多步向导的既有做法：`kind → field… → trust`，一屏一个字段，input 只读用户输入 |
| `frontend/src/component/dialog-jobs.tsx:71-97` | `props` 是惰性 getter，`options={options()}` 本身就会重算；**不要**用 `<Show>` 包住 `DialogSelect`（会丢焦点） |

### 1.3 配置落盘：今天没有任何代码写 `models`

| 位置 | 事实 |
|---|---|
| `config/loader.py:587-609` | `append_mcp_server` 是唯一的"运行时写入"先例：写 **sidecar**（`mcp.local.yaml`），从不改主配置（"that one holds comments and the API key"），写完 `chmod 0600` |
| `config/loader.py:597-606` | 它是 `safe_load` + `safe_dump` **整体重写**，不是文本追加 → "列表才不撞重复键"这个理由不成立 |
| `config/loader.py:400-405` | `app_from_doc` 对 `doc["models"]` 直接 `.items()` → **合并目标必须是 map** |
| `config/secrets.py:85-87` | `secret_env_names` 对非 `Mapping` 直接返回空集（静默） |
| `config/loader.py:538-551` | `secret_env_names` 在 `app_from_doc` **之前**从 `merged["models"]` 算——之后 `${VAR}` 的名字就没了 |
| `config/loader.py:502-504` | `if not document: continue` 会吞掉两种情形：用户配置不存在、用户配置损坏 |
| `config/loader.py:511` | `user_document` 取的是**合并后**的 document → 侧车条目会被项目层当成"用户已定义" |
| `config/models.py:774-806` | `_coerce_model` 读 `name`/`base_url`/`provider`/`api_key`，并在这一步把 `${VAR}` 解析成明文；`ModelConfig` 不带 `provider`，也不带原始引用 |
| `config/secrets.py:52-59,65-68` | `${VAR}` 未设 → `MissingSecret`；provider 有约定变量但未设 → 返回 `""`（不抛） |
| `security/process_sandbox.py:228-229` | `ProcessSandboxPolicy` 是 `frozen=True` 的 dataclass，**没有** `secret_env_names` 字段 |
| `security/process_sandbox.py:638-640,921-923` | `--unsetenv` 读的是 `BwrapBackend._secret_env_names`（构造期 tuple，普通类，可加方法） |
| `server/world.py:54,292` | `AppWorld.sandbox_backend` **已存在**，且与 `JobRegistry`、工具栈共享同一实例（`world.py:189-226`） |
| `server/world.py:278` | `config_path = str(path) if path else None`——只有 `--path` 时才非空（目前只有一个消费者：`mcp.add` 的落盘锚点） |
| `paths.py:102-113` | 用户层 = `~/.config/ai-coding/config.yaml`；`Paths` 自带 `config_home` 属性 |
| `config/models.py:73-106` | `default_read_deny_paths` 含 `config_home()`，但**只被 `process_sandbox` 使用**（`process_sandbox.py:826`）；`read` 工具走 `security/sandbox.py`，不查这张表 |
| `cli.py:599-690` | `config init` 已存在，写 0600 内联模板（不读 `config.example.yaml`）；首启不是死路（M-H2 已收口） |

### 1.4 分发：一个真阻塞 + 两个附带问题

| 位置 | 事实 |
|---|---|
| `server/launcher.py:33-38` | `_DEFAULT_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"` → 只在源码树里成立 |
| `server/launcher.py:44,66-68` | `entry = frontend/"src"/"index.tsx"` 以**绝对路径**传给 `bun`，同时 `cwd=frontend`；换成"包内 entry + 运行目录 cwd"会 `Cannot find package 'solid-js'`（实测） |
| `server/launcher.py:49-54` | 缺 bun → **`RuntimeError`**（`cli.py:81` 只接 `ValueError`/`FileNotFoundError` → traceback） |
| `pyproject.toml:52-53` | 只有 `packages.find`，**无 `package-data`**；且 `namespaces` 默认 `true`，`find_namespace_packages` 会把 `ai_coding/frontend/src` 当命名空间包（实测） |
| `pyproject.toml:3` / `ai_coding/__init__.py:3` / `frontend/package.json:5` | 版本三处各写一遍，`0.1.0` |
| `.github/workflows/ci.yml` | **没有前端 job**（`grep bun` 0 命中，注释还写着 "Four jobs"）→ 50 个 `frontend/test/*.test.ts` CI 从不跑 |
| `pyproject.toml:37-43` | dev extras 里**没有 `build`** → CI 里 `python -m build` 会失败，需改用 `pip wheel` |
| `frontend/` | `src/` 516K，`test/` 352K，`node_modules/` 124M，`bun.lock` 是 JSON（`--frozen-lockfile` 可用） |
| `frontend/test/*.ts`、`tsconfig.json`、`bunfig.toml`、`package.json` | 全是目录内相对路径 → **移动目录后不需要改任何一个**（已逐个核对） |
| `scripts/*.py` | **没有**读 `frontend/` 的地方（`tui_shot.py` 不存在；`eval_fullstack_task.py` 操作的是它自己造的 `ws/frontend`） |

**实测（本机）**：

- `bun build src/index.tsx --target=bun` **失败**：缺 `@opentui/core-linux-arm64-musl` 等平台包。
- `bun build --compile` **成功**：119,535,386 B → `--minify` 113.0 MB → `strip --strip-unneeded` **96.2 MB**（PyPI 单文件上限 100 MB，勉强进）；产物是 ELF x86-64，能启动并打印 `AI_CODING_SOCKET is not set …`（exit 0）。
- **打包链路已实测通过**（在 `/tmp` 复制仓库实跑）：`dynamic version` + `package-data` 的 `**` 在 setuptools 68 与 84 都递归有效；wheel 共 238 项、frontend 69 项；从 wheel 解出的 frontend 拷到临时目录后 `bun install --frozen-lockfile` 117 packages / 1.0s，`tsc --noEmit` exit 0，`bun src/index.tsx` 能走到 socket 连接错误（所有 import 已解析）。
- **前端测试现状**：`bun test` → 484 pass / 52 files；`tsc --noEmit` exit 0。

---

## §2 外部调研结论（opencode / Codex / Crush / Claude Code / dsh）

| 问题 | 业界做法 | 我们的取舍 |
|---|---|---|
| `/connect` 形状 | opencode：provider 列表（75+，可过滤）→ 认证方式列表 → 输入框；**连完不自动切模型** | 照抄：列表驱动、可过滤、不自动切 |
| 密钥落盘 | opencode `auth.json` = `{provider: {type, key/access/refresh/expires}}`；**没有一个项目文档化 0600** | 我们写 0600 + 目录 0700，并把这条写进文档（业界空白） |
| 项目层能否选 provider | Codex：项目级配置**忽略** `model_provider`/`model_providers`/`profile`，启动告警 | 与我们 N-4b 一致，已由 `MODEL_DESTINATION_MEMBERS` 实现，不动 |
| 切换默认的语义 | Claude Code：`Enter` = 切换并持久为默认，`s` = 仅本会话；`fallbackModel` 是**独立静态列表**，`/model` 从不触碰 | 见 §3 分叉 2 |
| 是否探测端点 | **7 个项目 0 个**在 `/connect` 时发请求验证 key | 不探测（§9） |
| fallback 链头移动 | **0 先例** | 不做链的可视化编辑（§9） |
| 编号选项 | 0/7 项目还在用 | 一律列表 + ↑/↓ |
| 2026 分发默认 | PyPI wheel + `[project.scripts]` + `uv tool install` 为主，`pipx` 兼容；PEP 668 封了全局 pip | 我们走 wheel；bun 依赖见 §3 分叉 1 |
| `dsh` 是否真实 | 是（DeepSeek Harness，MIT，developer preview）；其 `dsh-cli` 的 `/model` 用**会话 fork** 切换 | 唯一"fork 会话"先例；我们不 fork（代价：历史里混着两个模型的输出，已如此） |

---

## §3 待拍板分叉（每个选项都经过调研，不是凭空给的）

### 分叉 1：分发路线

| 选项 | 内容 | 代价 |
|---|---|---|
| **A（推荐）** | wheel 内带前端源码（~0.5 MB）+ 首次运行 `bun install --frozen-lockfile` | 首次启动需网络、需 bun；一个 wheel 通吃 5 平台 |
| B | 预编译 TUI 二进制（实测 96.2 MB）打进 wheel | 每平台一个 wheel（5 个 × ~96 MB），贴 PyPI 100 MB 上限；发布矩阵 ×5；无需 bun、离线可用 |
| C | 不打包前端，只把报错做成可执行指令 | 改动≈0，但 `pip install` 之后 `ai-coding` 仍不可用——不算分发 |

### 分叉 2：`/model` 里 `Enter` 的语义

| 选项 | 内容 | 代价 |
|---|---|---|
| **A（推荐）** | `Enter` = 仅本会话切换（不写盘）。持久默认仍由 YAML 的 `default_model` 声明，`/connect` 也不改它 | 想换默认得改一次文件 |
| B | `Enter` = 切换并写成持久默认（Claude Code 语义），另给 `s` = 仅本会话 | 出现**两个默认源**（YAML `default_model` 与侧车），合并顺序必须写死，且 `loop.default_model`（"配置默认"）与"当前模型"的分离被打破——`tests/test_model_switching.py:94` 正是在测这个分离 |

### 分叉 3：`/connect` 成功后是否自动切到新模型

| 选项 | 内容 | 代价 |
|---|---|---|
| **A（推荐）** | 不切，回列表，用户按 `/model` 切（opencode 同） | 多一步 |
| B | 自动切 | 用户可能只是想先登记；"新增"与"启用"合成一个动作，回滚要多一步 |

### 分叉 4：首启（一个模型都没有）怎么办

| 选项 | 内容 | 代价 |
|---|---|---|
| **A（推荐）** | 维持 M-H2 的 `ai-coding config init` + `export KEY`。`/connect` 只服务"已经有至少一个模型"的运行 | 新装用户要先走两条 CLI 命令才见到 TUI |
| B | 让 TUI 在**无引擎**状态下也能启动，`/connect` 救活它 | `engine` 是 `AgentLoop` 的非可选协作者（`step`/状态栏/子智能体/`aclose` 都直接用），改成 Optional 会波及十余处，并引入"半个世界"状态 |

---

## §4 设计

### 4.1 架构图（B：`/connect` 与切换）

```
                      ┌──────────────────────── TUI (bun) ────────────────────────┐
                      │  /connect → dialog-connect.tsx                             │
                      │      util/connect.ts  ← 纯逻辑：步骤机、选项、请求体       │
                      │  /models  → dialog-models.tsx  (首帧 hello，mount 后现取)  │
                      └───────────────┬───────────────────────────────────────────┘
                                      │ JSON-RPC (Unix socket)
                      ┌───────────────▼───────────────────────────────────────────┐
                      │ handlers.py                                               │
                      │   _active_engine(world)  ← 唯一的"当前引擎"读法            │
                      │   model.providers → providers.PROVIDERS（唯一来源）        │
                      │   model.list      → world.config.models + _active_engine   │
                      │   model.connect   → 校验 → 落盘原文 → 热加入 → 补 secret   │
                      │   model.set       → loop.set_model → 重建引擎              │
                      └───────┬───────────────────────┬───────────────────────────┘
                              │                       │
        ┌─────────────────────▼──────┐   ┌────────────▼──────────────────────────┐
        │ ConfigLoader.append_model  │   │ AgentLoop.set_model                    │
        │  → config_home()/          │   │   build_engine(loop.models, key)       │
        │    models.local.yaml 0600  │   │   ├ 重挂 on_retry / on_fallback        │
        │    （map，写 api_key 原文） │   │   ├ _rewire_llm_helpers()              │
        └────────────────────────────┘   │   ├ 退役旧引擎（有 loop 就地关）       │
                                         │   └ loop.engine = 新引擎               │
                                         └────────────────────────────────────────┘
```

### 4.2 数据流（B）

**写入路径（本次运行 + 下次启动）**

```
/connect 提交 {provider, model, baseUrl?, apiKey?}
  1. world.config_path 非空（--path 运行）              → {ok:false, reason:"…写进那个文件"}
  2. key = model（模型名字段即配置键）
     已在 world.config.models 里                        → {ok:false, reason:"已存在，换个名字或 /model 切"}
  3. provider 不在 PROVIDERS                            → {ok:false, reason:已知列表}
     provider == generic 且 baseUrl 为空                → {ok:false, reason:"custom 必须给 baseUrl"}
     provider 有约定变量 且 环境未设 且 apiKey 为空      → {ok:false, reason:"export X 或粘贴 key"}
  4. 保留请求原文 api_key_text（"" / "${VAR}" / 字面量）
  5. cfg = _coerce_model({provider, name, base_url, api_key: api_key_text})   ← 只进内存
     包一层 try/except ValueError → {ok:false, reason}   ← 兜住 "${VAR} 未设"（MissingSecret）
  6. ConfigLoader.append_model(key, {provider, name, base_url?, api_key: api_key_text})  ← 落盘原文
  7. world.config.models[key] = cfg ; world.loop.add_model(key, cfg)
  8. 若 api_key_text 是 ${VAR} → world.sandbox_backend.add_secret_env_names({VAR})
  → {ok, key, provider, name, hasKey}

下次启动
  ConfigLoader.load_layers_report
    user 层：_read_layer(user_config)（可能不存在 → {}）
            + _merge_model_sidecar(document, paths)   ← 新：paths.config_home/"models.local.yaml"
            （放在 `if not document: continue` **之前**，否则"没有用户配置"与"用户配置损坏"两种情形都跳过）
    project 层：_filter_project_document(...)          ← user_document 已是合并后的（loader.py:511）
    secret_env_names(merged["models"])                 ← 在合并之后，${VAR} 才会被沙箱剥掉
    app_from_doc → 每项 _coerce_model
```

侧车形状（**map**，与 `models:` 同构；整体 `safe_dump` 重写，不是文本追加）：

```yaml
models:
  gpt-5:
    provider: openai
    name: gpt-5
    api_key: ${OPENAI_API_KEY}
```

**切换路径**

```
/model 选择 → model.set {name: key}
  → world.loop.set_model(key)
      if key not in self.models: raise ValueError("Unknown model: …")   ← 原文案保留
      new = build_engine(self.models, key, stream_usage=self.settings.stream_usage)
      new.on_retry    = self._restate_engine_retry
      new.on_fallback = self._restate_engine_fallback
      old, self.engine = self.engine, new
      self._rewire_llm_helpers()        ← 见下：context/memory 为 None 时跳过
      self._retire(old)                 ← 只入 _retired_engines；**不**当场关（见下）
  → provider = provider_from_base_url(str(getattr(engine.model_config, "base_url", "")))
```

`_rewire_llm_helpers`（`context`/`memory` 都是可选协作者，`test_model_switching.py` 传的是 `None`）：

```python
def _rewire_llm_helpers(self) -> None:
    cfg = getattr(self.engine, "model_config", None)
    if cfg is None:
        return
    from ai_coding.ai.llm_helpers import build_extractor, build_summarizer
    if self.context is not None:
        self.context.summarizer = build_summarizer(cfg)
    if self.memory is not None:
        self.memory.extractor = build_extractor(cfg)
```

**为此要给两个类加一个公开的可写 property**（今天是私有字段、无 property）：

- `ContextManager.summarizer` ⇄ `self._summarizer`（`context_compression.py:590`，消费点 `710`/`907`）
- `MemoryService.extractor` ⇄ `self._extractor`（`memory/service.py:296`，消费点 `741`/`764`/`1090`）

否则 `context.summarizer = …` 只会新建一个没人读的属性——**静默失效**，那正是 v1 的"半修"。

**"当前引擎"只有一个读法**（`world.engine` 不再被任何地方赋值）：
```python
def _active_engine(world):
    return getattr(world.loop, "engine", None) or world.engine
```

`initialize` / `model_set` / `model_list` 都用它。这样手打 `/model x`（走 `commands.py:1170` 的 `loop.set_model`，不经过 handler）也不会让 `model.list` 报旧模型。

**旧引擎不当场关闭。** `_retire` 只把它放进 `_retired_engines`，`aclose` 时统一关。理由：`/model` 有全局按键（`<leader>m` → Models 面板 → 回车），一个正在流式输出的回合仍握着旧引擎的 client；当场 `close()` 会掐断那次请求。停在一个只增不减的列表里是有意的——代价是每次切换多留一个空闲 client 到进程退出，换的是"切换永远不会弄坏正在跑的回合"。

### 4.3 数据流（A：分发）

```
pip install / uv tool install
  └─ wheel 内：ai_coding/**  +  ai_coding/frontend/{src,package.json,bun.lock,bunfig.toml,tsconfig.json}
ai-coding
  └─ run_tui
       frontend = _frontend_dir():
         AI_CODING_FRONTEND 有值 → 直接用它（开发热改：不拷贝）
         否则 → 包内 ai_coding/frontend（源码树与 wheel 是同一个路径）
       runtime = _runtime_dir(frontend):
         frontend 已有 node_modules → 就地用（装过依赖的 checkout）
         否则 → data_home()/frontend/<__version__>/（首次 copytree，ignore node_modules/.bun）
       _ensure_deps(runtime): node_modules 不在 → bun install --frozen-lockfile
                              （asyncio.to_thread，600s；失败先删半截 node_modules 再抛 ValueError）
       entry = runtime/"src"/"index.tsx"      ← **必须是 runtime 下的相对位置**
       spawn bun src/index.tsx  (cwd = runtime)
```

`entry` 必须是 `runtime` 下的路径：`bun` 从**入口文件所在目录**向上找 `node_modules`，传包内绝对路径会 `Cannot find package 'solid-js'`（实测）。

**所有启动期失败都抛 `ValueError` / `FileNotFoundError`**（`cli.py:81` 只接这两个），用户看到一行提示而不是 traceback。

### 4.4 关键设计决定（为什么是这个，不是别的）

1. **侧车是 map，不是 list**。`app_from_doc` 对 `models` 直接 `.items()`、`secret_env_names` 对非 Mapping 静默返回空集——list 会让"连过一个模型的用户下次启动直接崩"。`append_mcp_server` 用 list 是因为 `mcp.servers` 本身就是 list；`models` 是 map，形状跟着数据走。
2. **侧车写 `api_key` 的原文，不写解析结果**。`_coerce_model` 会把 `${VAR}` 解析成明文，`ModelConfig` 也不带 `provider`——拿它落盘就是**把 key 明文写进磁盘**，同时丢掉 `key ≠ name` 的信息。落盘走请求原文，`_coerce_model` 的产物只进内存。
3. **侧车锚定 `paths.config_home`，不锚定 `chosen`**。`mcp_sidecar` 要求 `base.is_file()`，新装用户没有 `config.yaml` 时会返回 `None`；而侧车恰恰要在"没有主配置"时也生效。用 `Paths.config_home` 还能让测试靠 `XDG_CONFIG_HOME` 隔离。
4. **侧车合并进 user 层文档，而不是 `app_from_doc` 之后**。这样：① 项目层的 `_filter_project_document` 把侧车条目视为"用户已定义"，项目只能调参不能改端点；② `secret_env_names` 在合并后计算，`${VAR}` 会被沙箱剥掉。若在 `app_from_doc` 里合并，②会漏。
5. **`model.connect` 拒绝 `--path` 运行**。`--path` 的语义是"只读这个文件"，往别处写会让模型"下次启动就消失"。`world.config_path` 由此获得第二个消费者。拒绝文案要给可执行下一步（`docs/how-to-run.md:50,61` 的示例就是 `--path` 开发流）。
6. **切换 = 重建引擎**。单一路径，顺带修掉三件事：跨 provider 用错端点；`FallbackEngine` 拒绝链外模型（今天 `/model` 会列出切不过去的模型）；目标模型**自己的** `fallback_models` 生效（走 setter 会忽略它）。
7. **切换还要重新指向压缩与记忆抽取**。它们是闭包里烘死的 `model_config`，不重指就只修好了主循环。为此给两个类各加一个可写 property——**没有它，赋值是静默无效的**。
8. **`/connect` 不探测端点**。业界 0/7；探测要绕沙箱/代理，收益不足以抵。
9. **`model.list` 是活数据，`hello.models` 退成首帧**。`DialogModels` 先用 `hello.models` 画第一帧（不闪空），mount 后换成 `model.list`。这样既消灭了"启动快照过期"，也不动 `tests/test_server_ipc.py:279`。
10. **前端目录移进包内**（`frontend/` → `ai_coding/frontend/`）。根因是"前端在包外"，用 build hook 复制是绕过根因。移动后 `launcher` 的定位就是包内路径，源码树与安装形态**同一套解析**。
11. **不给 `/model` 加补全条目**。`model` 与既有 `models` 对查询 `"mod"` 同分，`frontend/test/slash.test.ts:73` 锁着 `models` 胜出；加一条会红，且两份词表本就该收敛（§10.4）。`/model gpt-5` 走文本路径，后端受理。
12. **`AI_CODING_FRONTEND` 也走 `_ensure_deps`**。只跳过拷贝，不跳过依赖检查——否则指向一个干净 checkout 时 `bun` 会报模块找不到，而不是我们的 `ValueError`。

---

## §5 逐文件改动

### 5.1 后端（B）

| 文件 | 改动 |
|---|---|
| `ai_coding/ai/factory.py` | 新增 `build_engine(models, start, *, stream_usage=True)`；`build_engine_from_config` 改为委托（保留 `"no default model configured"` 原文案）。`__all__` 加 `build_engine` |
| `ai_coding/core/agent_loop.py` | `set_model` 改为重建（§4.2）；新增 `add_model(key, cfg)`（同名即 `ValueError`）、`_rewire_llm_helpers()`、`_retire(engine)`；`__init__` 加 `self._retired_engines: list = []`；`aclose` 关闭退役引擎 |
| `ai_coding/core/context_compression.py` | 新增 `summarizer` property（get/set ⇄ `_summarizer`） |
| `ai_coding/memory/service.py` | 新增 `extractor` property（get/set ⇄ `_extractor`） |
| `ai_coding/config/loader.py` | 新增 `models_sidecar(paths)`、`_merge_model_sidecar(document, paths)`、`ConfigLoader.append_model(key, entry)`；`load_layers_report` 在 user 层合并（**空层不 `continue`**） |
| `ai_coding/security/process_sandbox.py` | `BwrapBackend.add_secret_env_names(names)`（并集、去重、保序；`_bwrap_prefix` 每次 spawn 重读该 tuple，所以下一次 spawn 即生效） |
| `ai_coding/server/handlers.py` | 新增 `_active_engine(world)`、`model_list`、`model_providers`、`model_connect`；`initialize` / `model_set` 改用 `_active_engine`；表里注册三条 |
| `ai_coding/app/slash.py` + `app/commands.py` | `_ALIASES` 加 `/connect`；`handle_input` 加一个分支：TUI 里提示 `ctrl+a`，非 TUI 指向 `config init`（否则手打 `/connect` 会被当成 prompt 发给模型）；同时改掉 `commands.py:1175-1179` 那段"切链外模型会被拒绝（G7）"的过时注释 |
| `ai_coding/providers.py` | 不改（`PROVIDERS` 就是唯一来源） |

`model_list` 返回（**永不含 api_key**）：

```json
{"ok": true, "current": "gpt-5", "default": "deepseek-chat",
 "models": [{"key": "gpt-5", "name": "gpt-5", "provider": "openai",
             "baseUrl": "https://api.openai.com/v1", "hasKey": true}]}
```

`current` 的定义写死一句话：**引擎此刻正在用的那个**（`is` 优先，`name` 兜底）；有 fallback 链时它就是最后答话的成员。`default` 是 `world.config.default_model`（配置默认，两者不是一回事）。

`model_providers` 返回：

```json
{"ok": true, "providers": [{"id": "openai", "baseUrl": "…", "envVar": "OPENAI_API_KEY", "hasEnv": false}]}
```

`hasEnv` 是启动期环境快照，只用于 UI 默认值；`model_connect` 仍会自己再判一次（环境可能在两者之间变化）。

`model_connect` 返回 `{"ok": true, "key": …, "provider": …, "name": …, "hasKey": …}`，拒绝时 `{"ok": false, "reason": "…"}`（沿用 `mcp.add` 的 `{ok, reason}` 形状）。

### 5.2 前端（B）

| 文件 | 改动 |
|---|---|
| `ai_coding/frontend/src/util/connect.ts` | **新增**。纯逻辑：`providerOptions(providers)`、`authOptions(provider)`、`nextStep(step, choice)`、`buildConnectBody(values)`。所有决策在这里，可单测 |
| `ai_coding/frontend/src/component/dialog-connect.tsx` | **新增**。`provider → auth → key → model` 四屏；列表屏复用 `DialogSelect`，输入屏照 `dialog-mcp-add.tsx` 的"一屏一字段、input 只读用户输入、`keyed` 保证新元素" |
| `ai_coding/frontend/src/component/dialog-models.tsx` | 加 `client` + `onConnectNew` 入参；`props.models`（= `hello.models`）作首帧，mount 后 `model.list` 覆盖；`options={options()}` **原样保留**（props 是惰性 getter，见 `dialog-jobs.tsx:71-97`）；`actions=[{key:"ctrl+a", title:"connect"}]`，footer `ctrl+a connect · enter switch · esc close` |
| `ai_coding/frontend/src/util/slash.ts` | 新增一条 `connect → dialog.connect.show`（`models` 不动，见 §4.4 第 11 条） |
| `ai_coding/frontend/src/app.tsx` | 注册 `dialog.connect.show`；`DialogModels` 传 `client` 与 `onConnectNew`（打开向导，成功回列表——照 `openMcpPanel` 的既有写法）。`Hello.models` **保留**（首帧数据源） |

### 5.3 分发（A）

| 文件 | 改动 |
|---|---|
| `.gitignore` **先改** | `frontend/node_modules/` → `ai_coding/frontend/node_modules/`（同理 `.bun/`），**然后**才 `git mv`，移动后 `git status --porcelain \| grep node_modules` 必须为空 |
| `frontend/` → `ai_coding/frontend/` | `git mv`。目录内相对路径全部有效（§1.4），**不需要**改 `tsconfig.json`/`bunfig.toml`/`package.json`/`test/*.ts`；`scripts/*.py` 也不涉及 |
| `pyproject.toml` | ① **删掉** `version = "0.1.0"`（与 `dynamic` 并存会 `ValueError`）；② `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "ai_coding.__version__"}`；③ `packages.find` 加 `namespaces = false`（否则 `frontend/src` 会被当命名空间包，`node_modules` 里的 `.py` 可能混进 wheel）；④ `[tool.setuptools.package-data] ai_coding = ["frontend/src/**/*.ts", "frontend/src/**/*.tsx", "frontend/package.json", "frontend/bun.lock", "frontend/bunfig.toml", "frontend/tsconfig.json"]` |
| `ai_coding/server/launcher.py` | `_PACKAGED_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"`；`_frontend_dir()` / `_runtime_dir()` / `_ensure_deps()` 见 §4.3；`entry` 改为 `runtime/"src"/"index.tsx"`；缺 bun、安装失败改抛 `ValueError` |
| `.github/workflows/ci.yml` | ① 新增 `frontend` job：`oven-sh/setup-bun@v2`（`bun-version: "1.2.14"`，与 `bun.lock` 的生成版本一致）→ `bun install --frozen-lockfile` → `bun run typecheck` → `bun test`（`working-directory: ai_coding/frontend`）；② `quality` job 加一步用 `pip wheel` 断言包内文件（见下）；③ 改顶部 "Four jobs" 的注释 |
| `docs/how-to-run.md` | 新增"安装"一节（`uv tool install --from .` / `pipx install .` → `config init` → `export KEY` → `ai-coding`），写明首次启动需要 `bun` 与网络（分叉 1 选 B 则改文案），并说明 `--path` 开发流不适用 `/connect` |
| `docs/README.md:5`、`docs/architecture.md:31,94,157`、`docs/architecture-v2.md:263`、`docs/features.md:38,44-47,49,54,1044` | 路径同步（`frontend/` → `ai_coding/frontend/`）。`README.md` 本身无 `frontend` 引用，不需要改 |

CI 的 wheel 断言（`dev` extras 里没有 `build`，用 `pip wheel`）：

```yaml
- name: Wheel contains the frontend
  run: |
    python -m pip wheel . -w /tmp/wheelhouse --no-deps
    python - <<'PY'
    import glob, sys, zipfile
    wheels = glob.glob("/tmp/wheelhouse/ai_coding-*.whl")
    assert len(wheels) == 1, wheels
    names = set(zipfile.ZipFile(wheels[0]).namelist())
    required = {
        "ai_coding/frontend/src/index.tsx",
        "ai_coding/frontend/src/component/dialog-models.tsx",
        "ai_coding/frontend/bun.lock",
        "ai_coding/frontend/package.json",
        "ai_coding/frontend/bunfig.toml",
    }
    missing = required - names
    sys.exit(f"wheel missing: {sorted(missing)}" if missing else 0)
    PY
```

---

## §6 影响范围

| 区域 | 影响 | 依据 |
|---|---|---|
| 引擎生命周期 | 切换会新建/退役引擎；`aclose` 多关若干个 | `world.aclose → loop.aclose` 是唯一关闭路径 |
| 压缩 / 记忆抽取 | 切换后跟随新模型（**本轮修复**） | 两个新 property + `_rewire_llm_helpers`，§4.2 |
| 子智能体 | **不受影响**：`tools/builder.py:117` 用 `app_cfg.default_model` 另建引擎 | 见 §10.1 |
| 配置信任边界 | 不变：侧车在 user 层；`MODEL_DESTINATION_MEMBERS` 未动 | `_filter_project_document` 只看 `user_document`（已是合并后） |
| 沙箱 secret 集合 | 本轮 `/connect` 写入的 `${VAR}` 当场进入 `--unsetenv` | `BwrapBackend.add_secret_env_names`，§4.2 第 8 步 |
| `--path` 运行 | `/connect` 被拒绝（理由可执行），其余不变 | §4.4 第 5 条 |
| `world.engine` | 不再被赋值；三个读者统一走 `_active_engine` | §4.2 |
| 打包 | 前端进 wheel；版本单源（pyproject）；CI 新增前端 job | §1.4、§5.3 |
| 现有测试 | 预期**零改动**：`test_model_switching.py` 5 例（保留 `Unknown model` 守卫；`context`/`memory` 为 `None` 时 rewire 跳过；无 running loop 时 `_retire` 走列表）；`test_fallback_engine.py` 4 例（setter 不删）；`test_server_ipc.py:279,1430`（`hello.models` 保留；`_FakeLoop` 无 `engine` → 回落 `_FakeEngine`）；`test_repl.py:806-831`（它 monkeypatch 了 `set_model` 来验证"命令要报告而不是抛出"，契约不变）；`frontend/test/slash.test.ts`（不新增 `model` 条目） | 已逐个核对 |

---

## §7 RIPR 用例与变异计划

### 7.1 红相（先写、先红，且红在正确的原因上）

**`tests/test_model_switch_rebuild.py`**（新）

| 用例 | 断言 |
|---|---|
| `test_switching_providers_moves_the_client` | 切换后 `str(engine._client.base_url).rstrip("/")` == 新模型 `base_url`（**这条就是今天的缺陷**） |
| `test_switching_rebuilds_rather_than_mutating` | `loop.engine is not old` |
| `test_the_rebuilt_engine_keeps_the_retry_and_fallback_observers` | 新引擎 `on_retry`/`on_fallback` 与旧引擎同一对象 |
| `test_the_rebuilt_engine_keeps_stream_usage` | `settings.stream_usage = False`，**目标模型不带 `fallback_models`**（保证拿到 `OpenAIChatEngine`），断言 `engine._stream_usage is False` |
| `test_switching_repoints_compaction_and_memory` | `context.summarizer` / `memory.extractor` 是**新对象**（`is not` 旧值）；再 monkeypatch `llm_helpers._chat`（及 `_chat_stream`）捕获第一个位置参数，断言捕获到的是**新模型**的 `ModelConfig`。全程无网络 |
| `test_switching_without_context_or_memory_is_fine` | `context=None, memory=None` 时 `set_model` 不抛（`test_model_switching.py` 的形状） |
| `test_switching_to_a_model_outside_the_chain_works` | 链 `[a]`（a 有 `fallback_models: [b]`）时切到 `c` 不抛 |
| `test_the_target_models_own_chain_is_used` | 切到 b（b 有 `fallback_models: [c]`）后，`FallbackEngine._engines` 顺序是 `[b, c]` |
| `test_unknown_model_still_raises_value_error` | `match="Unknown model"`（原文案） |
| `test_add_model_refuses_to_overwrite` | 同名 `add_model` → `ValueError` |
| `test_closing_the_loop_closes_the_retired_engines` | 无 running loop 时进 `_retired_engines`，`aclose` 后旧引擎已关 |

**`tests/test_model_catalogue.py`**（新，走 `make_handlers` 的真实表 + 替身 world，照 `test_server_ipc.py` 的搭法）

| 用例 | 断言 |
|---|---|
| `test_model_list_marks_the_active_one` | `current` 是当前 key；`default` 是配置默认 |
| `test_model_list_follows_a_switch_made_outside_the_rpc` | 直接调 `loop.set_model` 后 `model.list` 的 `current` 跟着变（`_active_engine` 的用途） |
| `test_model_list_never_sends_the_key` | 返回 JSON 里不含 `api_key`/`sk-` |
| `test_model_providers_publishes_the_table` | 5 项，含 `envVar` 与 `hasEnv` |
| `test_connect_persists_and_hot_adds` | 侧车文件出现；`world.config.models` 与 `world.loop.models` 都有 |
| `test_connect_refuses_an_existing_key` | `{ok: false}`，侧车未变 |
| `test_connect_refuses_an_unknown_provider` | `{ok: false}`，理由含已知列表 |
| `test_connect_refuses_a_path_run` | `config_path` 非空 → `{ok: false}` |
| `test_connect_refuses_custom_without_a_base_url` | `{ok: false}` |
| `test_connect_refuses_an_unset_reference` | `apiKey: "${NOPE_NOT_SET}"` → `{ok:false}`（不是抛异常） |
| `test_connect_writes_the_env_reference_when_the_variable_is_set` | 侧车写 `${OPENAI_API_KEY}`，不写明文 |
| `test_connect_refuses_a_provider_whose_key_is_missing` | `OPENAI_API_KEY` 未设且未给 key → 拒绝 |
| `test_connect_accepts_a_keyless_local_provider` | `ollama` 无 key 可连 |
| `test_a_connected_env_reference_is_stripped_this_session` | `world.sandbox_backend` 的 secret 集合含新变量（`_bwrap_prefix` 会发 `--unsetenv`） |

**`tests/test_models_sidecar.py`**（新）

| 用例 | 断言 |
|---|---|
| `test_a_connected_model_survives_a_reload` | 写侧车 → `load_layers_report` 里出现，`base_url`/`api_key` 正确 |
| `test_a_key_that_differs_from_the_name_survives` | `key: qwen` / `name: qwen3.8-2.4t-a95b` → 合并后键是 `qwen`（不是 `name`） |
| `test_the_sidecar_is_created_at_0600` | `stat().st_mode & 0o777 == 0o600` |
| `test_the_sidecar_applies_without_a_user_config` | 只有侧车、没有 `config.yaml` 时仍生效 |
| `test_a_project_cannot_inject_a_model` | 项目层 `models: {evil: {...}}` 仍被丢 |
| `test_a_project_may_tune_a_sidecar_model` | 项目层对侧车模型的 `max_tokens` 生效，`base_url` 被剥 |
| `test_sidecar_env_references_are_stripped_from_children` | `layered.secret_env_names` 含 `${VAR}` 名 |
| `test_an_explicit_path_run_ignores_the_sidecar` | `-p` 运行；**工作区里放一个诱饵 `models.local.yaml`**，断言它不生效（这条才真的能杀死"锚定 workspace"的变异） |

**`tests/test_frontend_launch.py`**（新；`launcher` 今天 0 测试）

| 用例 | 断言 |
|---|---|
| `test_the_override_wins_and_is_not_copied` | `AI_CODING_FRONTEND` 指向 tmp → 返回它，不产生 `data_home` 副本 |
| `test_a_checked_out_frontend_is_used_in_place` | 包内目录已有 `node_modules` → 就地用，不 copytree |
| `test_the_packaged_frontend_is_copied_once` | 两次调用 → 同一目标，`copytree` 只发生一次；`node_modules` 不被拷 |
| `test_the_entry_point_lives_in_the_runtime_dir` | `entry` 是 `runtime/"src"/"index.tsx"`，不是包内绝对路径（否则 `bun` 找不到 `node_modules`） |
| `test_missing_node_modules_installs_once` | 替身 runner 被调 1 次；已有 `node_modules` 时 0 次 |
| `test_a_failed_install_is_a_value_error_naming_the_command` | `ValueError`（不是 `RuntimeError`），文本含 `bun install` 与目录；半截 `node_modules` 被清掉 |
| `test_a_missing_frontend_names_the_override` | `FileNotFoundError` 文本含 `AI_CODING_FRONTEND` |

**`tests/test_slash_connect.py`**（新）

| 用例 | 断言 |
|---|---|
| `test_connect_is_a_published_command` | `"connect" in command_names()`（否则 TUI 会把 `/connect` 当 prompt 发出去） |
| `test_connect_explains_where_to_do_it` | `handle_input` 输出含 `ctrl+a` 与 `config init` |

**前端**：`ai_coding/frontend/test/connect.test.ts`（新）——`providerOptions` 过滤、`authOptions`（有 env → 两个选项；无 env → 只有"不需要 key"）、`nextStep` 四步闭环、`buildConnectBody` 空 key 时省略 `apiKey`；`slash.test.ts`（**追加**）——`connect` 能被 `filterSlashCommands` 命中且 `command === "dialog.connect.show"`。

### 7.2 变异计划（P 相，18 个，每个都必须被上面某条用例杀死）

| # | 变异 | 杀它的用例 |
|---|---|---|
| M1 | `set_model` 退回 `self.engine.model_config = …` | `test_switching_providers_moves_the_client` |
| M2 | 重建后不重挂 `on_retry`/`on_fallback` | `test_the_rebuilt_engine_keeps_…observers` |
| M3 | 重建时丢掉 `stream_usage` | `test_the_rebuilt_engine_keeps_stream_usage` |
| M4 | `set_model` 不调 `_rewire_llm_helpers` | `test_switching_repoints_compaction_and_memory` |
| M5 | `add_model` 允许覆盖同名 | `test_add_model_refuses_to_overwrite` |
| M6 | 侧车按 `name` 建键（而非 `key`） | `test_a_key_that_differs_from_the_name_survives` |
| M7 | `model.list` 的 `default` 跟着 `current` 走 | `test_model_list_follows_a_switch_made_outside_the_rpc` |
| M8 | 侧车合并在 `secret_env_names` 之后 | `test_sidecar_env_references_are_stripped…` |
| M9 | 空层仍 `continue`（侧车在无用户配置时被跳过） | `test_the_sidecar_applies_without_a_user_config` |
| M10 | 侧车锚定 workspace（`_merge_model_sidecar(document, paths.workspace)`） | `test_a_workspace_sidecar_is_not_read` |
| M11 | `model.list` 返回 `apiKey` | `test_model_list_never_sends_the_key` |
| M12 | `append_model` 写 0644 | `test_the_sidecar_is_created_at_0600` |
| M13 | `model.connect` 接受无 `baseUrl` 的 `generic` | `test_connect_refuses_custom_without_a_base_url` |
| M14 | `model.connect` 只热加入、不落盘 | `test_connect_persists_and_hot_adds` |
| M15 | `model.connect` 落盘解析后的明文而非原文 | `test_connect_writes_the_env_reference…` |
| M16 | `model.connect` 不补沙箱 secret 集合 | `test_a_connected_env_reference_is_stripped_this_session` |
| M17 | `_active_engine` 退回只读 `world.engine` | `test_model_list_follows_a_switch_made_outside_the_rpc` |
| M18 | `_runtime_dir` 有 `node_modules` 也 copytree | `test_a_checked_out_frontend_is_used_in_place` |

**P 相实测：18/18 全被杀。** 过程中修正了三条原本杀不死的映射，并顺带砍掉一处冗余契约：

- **M13 原形态（"connect 不校验 provider"）杀不死**：`_coerce_model` 已经用同一张 `PROVIDERS` 表拒绝未知 provider，而且消息里还带模型名。这说明 handler 里那段显式校验是**第二份真值**，已删除——现在只剩 `generic` 必须有 `baseUrl` 这一条（`_coerce_model` 只告警不拒绝）。
- **M10 原形态（"锚定 cwd"）杀不死**：`Path.cwd()` 是仓库根，测试里的诱饵在 tmp 工作区。改成在调用点锚定 `paths.workspace`，并新增 `test_a_workspace_sidecar_is_not_read`——那才是真正的信任边界（仓库不能自带一个侧车来选端点）。
- **M7 原形态（"`user_document` 在合并前取快照"）是伪变异**：`_merge_model_sidecar` 就地改 `document`，两个顺序行为等价。换成"`default` 跟着 `current` 走"，并把 `test_model_list_follows_a_switch_made_outside_the_rpc` 补上 `default == "alpha"` 的断言。
- `test_a_project_cannot_inject_a_model` / `test_a_project_may_tune_a_sidecar_model` 保留为 N-4b 既有行为的回归守卫，不是本轮变异目标。

---

## §8 修改成功后的样子

```
$ uv tool install --from .
$ ai-coding config init
wrote ~/.config/ai-coding/config.yaml (0600)
next: export DEEPSEEK_API_KEY=...
$ export DEEPSEEK_API_KEY=sk-…
$ ai-coding                     # 首次：拷贝前端 + bun install（一次）
                                # 之后：直接进 TUI

TUI 里：
/models     → 列表（deepseek-chat ●）           ctrl+a connect · enter switch · esc close
ctrl+a      → /connect
  ┌ Connect a provider ────────────────┐
  │ ● openai      api.openai.com/v1     │   ← 可过滤
  │   deepseek    api.deepseek.com/v1   │
  │   qwen        dashscope…            │
  │   ollama      localhost:11434       │
  │   custom      any OpenAI-compatible │
  └─────────────────────────────────────┘
  → 认证方式：● use $OPENAI_API_KEY（已设置）   ○ paste a key
  → 模型名：gpt-5
  → [connect] ok

/models     → 列表里多了 gpt-5；回车切过去
              [model] switched to gpt-5
              → 下一次请求真的发到 api.openai.com，带 OPENAI_API_KEY
              → 压缩摘要与记忆抽取也跟着换了
              → 状态栏 provider 变成 openai

$ ai-coding                     # 重启：gpt-5 还在列表里（来自 models.local.yaml）
$ ls -l ~/.config/ai-coding/models.local.yaml
-rw------- 1 u u  … models.local.yaml
```

---

## §9 本方案不做的事

1. **不做无模型首启的 TUI 引导**（分叉 4 选 A）。`engine` 是 `AgentLoop` 的非可选协作者。
2. **不做终端版 `/connect`**。一份向导实现；`config init` 已覆盖首启。
3. **不做 `model.remove` / `model.edit`**。用户没要求；侧车是可手改的 0600 YAML。**不做**的代价明说：连错一个模型只能手改文件。
4. **不做 fallback 链的可视化编辑**。"链头被移动"语义业界 0 先例（§2），`fallback_models` 仍由 YAML 声明。
5. **不在 `/connect` 时探测端点**。业界 0/7；探测要绕沙箱/代理。第一次真实请求失败时照旧报错。
6. **不删 `FallbackEngine.model_config` 的 setter 与它的 4 个用例**。它是"把链内某成员提到首位"的操作，仍然成立；只是 `/model` 改走重建（走 setter 会忽略目标模型自己的 `fallback_models`）。删它就得删 4 个用例，且没有替代收益。其 docstring 里"`AgentLoop.set_model` assigns here"一句要改（会变成假话）。
7. **不给 `/model` 加补全条目**。会与 `models` 同分（`slash.test.ts:73`），且 `/model x` 文本路径已通（§4.4 第 11 条）。
8. **不引入加密/钥匙串**。M-H2 已定：0600 + 目录 0700 + `config_home()` 在进程沙箱的 `read_deny_paths` 里（注意：只对 bash/grep 子进程生效，`read` 工具取决于 `extra_workspace_roots`，见 §10.5）。
9. **不打包预编译二进制**（分叉 1 选 A 时）。
10. **不做 PyPI 发布流程**。没有包名归属与上传凭证；CI 只验证 wheel 内容，文档先写 `uv tool install --from .` / `pipx install .`，发布后改成正式名。
11. **不改 `MODEL_DESTINATION_MEMBERS` / `CREDENTIAL_NAMES` / 项目层信任规则**。
12. **不把子智能体的引擎接到"当前模型"**（§10.1）。
13. **不改 `docs/how-to-run.md:50,61` 的 `--path` 用法**——只把 `/connect` 的拒绝文案写成可执行下一步。
14. **不打包 `config.example.yaml`**。`config init` 用的是内联模板（`cli.py:599-629`），运行时不需要那个文件。

---

## §10 既有隐患（记录，不在本轮修）

1. **切换模型后子智能体仍用配置默认**。`tools/builder.py:117` 是 `lambda: build_engine_from_config(app_cfg)`，读 `app_cfg.default_model`。用户切到 gpt-5，子智能体还在 deepseek 上跑。修它要把"当前模型 key"喂进 builder（新增一个 callable 参数），波及 `_build_tool_registry` 与 `build_world` 的签名——独立一轮更合适。**因此 `/model` 的成功提示只说"主循环已切换"，不声称子智能体跟随。**
2. **`ContextManager.context_window` 在构造期冻结**（`cli.py:181-186` 调 `resolve_context_window(app_cfg)` 一次，读 `config.default_model`）。切到**更小**窗口的模型时水位仍按配置默认，可能溢出——不只是"不准"。本轮只换 summarizer，不换水位；这条要在 `/model` 的提示或文档里说清楚。
3. **`initialize` 的 `models` 是启动快照**。本轮让 `model.list` 成为活数据、`hello.models` 退成首帧；其它读者若出现会再犯同样的错。
4. **`frontend/src/util/slash.ts` 与 `app/slash.py` 两份词表**。本轮只补 `connect`，不合并（合并要一个跨语言的单一来源，成本远超收益）。
5. **`config_home()` 只在进程沙箱里被 mask**，`read` 工具不查 `read_deny_paths`。侧车里的明文 key 在 `tools.extra_workspace_roots: ["~/.config"]` 时会被 `read` 读到（默认配置下不会）。
6. **`world.config_path` 在分层模式下为 `None`**，`mcp.add` 的侧车因此锚到 `discover()`（cwd）。本轮不碰 `mcp`。
7. **`ModelConfig.streaming` 仍无消费者**（M-H2 已记录）。
8. **`load_app`（`scripts/*`、eval）看不到侧车**。生产路径 `build_world` 走 `load_layers_report`，没问题；脚本里的模型列表与 TUI 可能不一致。
9. **`frontend/package.json` 的 version 不参与发布**。唯一版本源是 `ai_coding/__init__.py`（pyproject 从它取）；前端私有、不发布 npm，所以那一份只作参考，CI 不校验。
10. **`frontend/test/**/*.tsx` 不在 `tsc --noEmit` 的范围内**。把 `test/**/*.tsx` 加进 `tsconfig.include` 会立刻暴露 20 处既存类型错误（`TextareaProps.value`、`TestRendererOptions`、`CommandResult` 等），全部与 H3 无关；`bun test` 照常运行它们（498 例）。属于独立一轮的清理。
11. **`_runtime_dir` 的"已复制"判据只是 `src/index.tsx` 存在**。`copytree` 被中断（Ctrl-C、磁盘满）时可能留下一个缺文件但入口还在的目录，后续启动不会再复制。恢复手段是删掉 `~/.local/share/ai-coding/frontend/<版本>/`。

---

## §11 修订记录

| 版本 | 变化 |
|---|---|
| v1 | 首版：A 分发 + B `/connect`；§3 四个分叉；§7 14 个变异 |
| v2 | 第一轮评审修入 15 项（侧车改 map、落盘写原文、`_rewire_llm_helpers`、`_active_engine`、`FallbackEngine` 无 `base_url`、`Unknown model` 守卫、`add_secret_env_names`、`ValueError`、`_runtime_dir`、删 `<Show>`、取消 `model` 补全条目、`/connect` 进 `_ALIASES`、变异 14→18、分发清单、`read` 工具过度声明） |
| v3 | 第二轮评审修入：① `_rewire_llm_helpers` 的属性名错了——真实字段是 `ContextManager._summarizer` / `MemoryService._extractor`（无公开 property），赋值会**静默失效**，改为给两类各加可写 property；② `add_secret_env_names` 挂错了类——`ProcessSandboxPolicy` 是 frozen dataclass 且无该字段，`--unsetenv` 读的是 `BwrapBackend._secret_env_names`，改挂 backend（`AppWorld.sandbox_backend` 已存在）；③ `entry` 必须是 `runtime/"src"/"index.tsx"`（包内绝对路径会让 `bun` 找不到 `node_modules`，实测）；④ 不删 `Hello.models`（会让 `test_server_ipc.py:279` 变红），改为"首帧 + 现取"；⑤ `_coerce_model` 外包 `try/except ValueError`（`${VAR}` 未设会抛 `MissingSecret`）；⑥ `context`/`memory` 为 `None` 时跳过 rewire、无 running loop 时 `_retire` 走列表；⑦ M4/M9/M10 的用例改成真的能杀死变异（M10 用工作区诱饵文件）；⑧ `pyproject` 删静态 `version`、加 `namespaces = false`；⑨ CI 用 `setup-bun@v2`（锁 1.2.14）+ `pip wheel` 断言；⑩ `AI_CODING_FRONTEND` 也走 `_ensure_deps`，失败时清半截 `node_modules`；⑪ 文档同步清单按实测补 `architecture.md`/`architecture-v2.md`/`features.md`，删掉空操作的 `README.md`；⑫ 新增 §9.10"不做 PyPI 发布"与 §9.14"不打包 `config.example.yaml`"；⑬ §0 措辞改为"`pip install` + 有 `bun`（首次联网）"；⑭ §10.1/§10.2 明确"不声称已跟随" |
| v4 | 实现评审（两名评审员，0 P0 / 2 P1 / 12 P2）修入：① **`_retire` 不再当场关旧引擎**——`/model` 有全局按键，正在流式输出的回合仍握着旧 client，当场 `close()` 会掐断它（顺带消掉"双关"与"task 无引用"两个 P2）；② `model_connect` 改为**先 `add_model` 再落盘**，碰撞预检也改看 `loop.models`（唯一会抛的那张表）；③ `/connect` 写入后按 `secret_env_names({key: entry})` 补沙箱集合（与配置路径同口径，字面 key 也要剥约定变量）；④ 侧车条目过 `_normalize`（可手改，`baseUrl` 不再静默失效）；⑤ 写侧遇到坏侧车**拒绝覆盖**（读侧仍宽容）；⑥ `append_model` 复用 `models_sidecar()`（一个文件名）；⑦ `sources["models"]` 指向侧车；⑧ `_merge_model_sidecar(document, paths.config_home)`（死参数有消费者）；⑨ `model_list` 的 provider 与状态栏统一走 `provider_from_base_url`，并守住 `active_name` 为空；⑩ `HELP_TEXT` 补 `/connect`，提示文案改成"先 `/models` 再 ctrl+a"（ctrl+a 只在面板内有效）；⑪ `FallbackEngine.model_config` 的 docstring 改准确；⑫ **`generic` 现在能带 key**：`nextStep` 改成 provider 感知（`provider → baseUrl → key → model`），key 可选，`keyIsOptional` 进纯逻辑并有测试；⑬ `_ensure_deps` 捕获 `TimeoutExpired`/`OSError` → `ValueError` + 清半截 `node_modules`；⑭ `build_world` 先于依赖安装（配置错误先报），`spawn` 挪进 `try`（失败也关 service/world）；⑮ CI 的 wheel 断言补 `.ts` 与 `tsconfig.json`；⑯ `how-to-run.md` 环境要求补 `bun`；⑰ P 相 18/18 全杀（含三条映射修正，见 §7.2 末） |
