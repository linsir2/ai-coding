# 项目配置信任（N-4）· 分层配置的执行类字段只认用户层

> 状态：**待拍板**
> 关联：[roadmap-milestones.md](roadmap-milestones.md) §十三 N-4
> 来源：M-B 两轮评审发现的同源风险（`static_check` 的 P0 只是其中一个字段）
> 调研：2026-10-25（子智能体全仓通读 + 我复核）

---

## 一、先回答：配置不可信的完整清单

`static_check` 那个 P0 不是孤例。调研把**所有"配置来源不可信、却被 harness 执行或产生高权限效果"**的项都列了出来：

| 配置项 | 会被执行吗 | 现有防护 | 风险 |
|---|---|---|---|
| `ai.static_check.commands` | 是（子进程） | ✅ env 门（M-B 已修） | 低 |
| **`hooks.enabled` + `hooks.events[].command`** | **是（任意程序）** | ❌ 无审批/无沙箱 | **高** |
| **`hooks.events[].module` + `plugin_dir`** | **是（`exec_module` 任意 Python）** | ❌ 无 | **高** |
| **`mcp.enabled` + `mcp.servers[].command`** | **是（spawn 子进程）** | ❌ 无审批 | **高** |
| `mcp.servers[].environment` / `cwd` | 是（传给子进程） | ❌ 无 | 高 |
| `models[].base_url` / `api_key` / `default_model` | 是（API 外发 + key 注入） | ❌ 无 | **高** |
| `tools.extra_workspace_roots` | 是（文件工具免问读写任意根） | 仅目录校验 | **高** |
| `mcp.servers[].headers` + `url` | 是（外发，同名可复用 OAuth token） | token 落 0600 外部文件 | 中高 |
| `mcp.servers[].auto_approve` / `permissions.allow` | 是（WARN→ALLOW 常驻） | deny 优先 | 中 |
| `tools.bash.timeout_seconds` | 是（成本） | 有界 | 低 |
| `ai.skill_permissions` / `guard.bash_deny` | 是（只能收紧） | 追加式 | 低 |

**共同根因只有一条**（这是"只改根源"的落点）：

```
loader.py::_overlay  ——  "top wins per key"，项目层逐键覆盖用户层
```

而 `_overlay` 的 docstring 自己写了理由：

> Lists replace on purpose: ``mcp.servers`` and ``tools.enabled`` are
> declarations of what exists, not additive layers, and concatenating them would
> let a project's ``enabled: [bash]`` silently re-enable everything the global
> layer turned off.

**它防住了"项目层放宽工具开关"，却没防"项目层新增执行能力"。** 这是同一个根因的两面。

---

## 二、关键事实（都已复核）

### 2.1 加载链：项目层确实覆盖用户层

[loader.py:171-186](file:///workspace/ai-coding/ai_coding/config/loader.py#L171-L186) `_layers_for`：

```python
layers = [("user", paths.user_config)]        # ~/.config/ai-coding/config.yaml
project = ConfigLoader.discover(paths.workspace)
if project is not None:
    layers.append(("project", project)
```

[loader.py:202-220](file:///workspace/ai-coding/ai_coding/config/loader.py#L202-L220) `_overlay`：深合并，`top`（项目层）逐键胜出；`mcp.servers` 例外（**追加**）。

实测（子智能体 + 我复核）：在一个只有 `config.example.yaml` 的目录里
`ConfigLoader.load_app_from('.')` → `commands = ['echo PWNED-FROM-REPO-EXAMPLE']`。

### 2.2 现成的复用点：`sources` 已经算好了，但没人用

[loader.py:222-248](file:///workspace/ai-coding/ai_coding/config/loader.py#L222-L248) `load_layers_report` 返回
`LayeredConfig(config, sources, layers)`，其中 `sources[key]` = **回答该顶层 key 的文件路径**。

`tests/test_config_layers.py:117/127` 已经在断言它。**但它只被 `load_layers` 内部消费（丢弃）**：

```python
def load_layers(paths, path=None) -> AppConfig:
    return ConfigLoader.load_layers_report(paths, path).config   # ← sources 丢了
```

而 `load_layers` 的循环**本来就逐层读取**（`for _role, layer in _layers_for(...)`），所以我可以在循环里按 `role` 决定"这一层的哪些 key 该被丢弃"——**不需要 `sources`，也不需要新机制**。

### 2.3 TUI 走分层；`ConfigManager` 已无消费者

- TUI：[world.py:119](file:///workspace/ai-coding/ai_coding/server/world.py#L119) `ConfigLoader.load_layers(paths, path)` → **分层**
- `ConfigManager.initialize`（[loader.py:327-340](file:///workspace/ai-coding/ai_coding/config/loader.py#L327-L340)）走 `discover()` 单文件 → **不分层**
- `grep -rn "ConfigManager()"` → **零消费者**（遗留类）

→ 只需改分层这一条路径。

### 2.4 危险字段的具体形态

**hooks**（[hooks.py:276-288](file:///workspace/ai-coding/ai_coding/core/hooks.py#L276-L288)）：

```python
outcome = await run_process_async(list(spec.command), cwd=self._workspace, env=hook_environment(), ...)
```

无审批、无权限门。`kind: "python"` 走 [hooks.py:358-370](file:///workspace/ai-coding/ai_coding/core/hooks.py#L358-L370)：

```python
path = Path(self._plugin_dir) / f"{spec.module}.py"
module_spec.loader.exec_module(module)      # ← 任意代码执行
```

`plugin_dir` 可配置覆盖，默认 `<workspace>/.ai-coding/hooks`（[cli.py:295-297](file:///workspace/ai-coding/ai_coding/cli.py#L295-L297)）。

**MCP**（[mcp.py:856-866](file:///workspace/ai-coding/ai_coding/core/mcp.py#L856-L866)）：`mcp.servers[].command` 直接构造 `StdioServerParameters` 并 spawn，`open_all` 只校验形状。

**两者默认都关**（`hooks.enabled=False`、`mcp.enabled=False`）—— 但**项目层可以自己打开**，这正是问题。

---

## 三、原设计 vs 目前设计

| | 原设计 | 目前设计 |
|---|---|---|
| 项目层覆盖用户层 | 全部 key | 全部 key（**本方案要收窄**） |
| 谁能授予执行能力 | 任何一层 | **只有用户层**（+ 显式 env 门） |
| `sources` | 算了但丢弃 | **算了，且被用于决定丢弃** |
| `static_check` | 已 env 门（M-B） | 不变 |

### 3.1 第一性原理：用户启动 agent 时隐含信任什么？

在一个目录里运行 `ai-coding`，用户隐含：

- ✅ 信任该目录作为工作区（读写文件）
- ✅ 信任该目录里的**代码**（agent 会读它、可能跑测试）
- ❌ **没有**隐含"信任该目录的**配置**去执行额外命令"
- ❌ **没有**隐含"信任该目录的**配置**把 API 请求发到别处"

所以规则是：**项目配置能决定"怎么干"，不能自己授予"能执行什么"。**

---

## 四、复用的方法契约（**不新建机制**）

| 契约 | 位置 | 怎么用 |
|---|---|---|
| `_layers_for(paths, path)` | [loader.py:171](file:///workspace/ai-coding/ai_coding/config/loader.py#L171) | **复用**：循环里已按 `role` 分层 |
| `_read_layer(layer)` | [loader.py:190](file:///workspace/ai-coding/ai_coding/config/loader.py#L190) | **复用**：拿到该层原始 document |
| `_overlay(base, top)` | [loader.py:203](file:///workspace/ai-coding/ai_coding/config/loader.py#L203) | **复用**：合并逻辑不变 |
| `LayeredConfig.sources` | [loader.py:304](file:///workspace/ai-coding/ai_coding/config/loader.py#L304) | **复用**：保留给诊断 |
| `_log.warning` | 既有 | **复用**：报告被丢弃的字段 |
| `static_check.enabled()` | [static_check.py](file:///workspace/ai-coding/ai_coding/core/static_check.py) | **复用**：env 门模式 |

**不新增**：类、模块、CLI 参数（除 1 个 env 变量，见 §九）、配置字段。

---

## 五、逐文件实现

### 5.1 `ai_coding/config/loader.py` —— 唯一的改动点

```diff
+#: Top-level keys whose value can make the harness *execute* something or send
+#: it somewhere.  A project's config is not trusted to grant that: the operator
+#: who runs ``ai-coding`` in a directory trusts its files, not its config, and
+#: ``config.example.yaml`` ships in the repository.  Only the user layer (and the
+#: explicit env gate, see ``_project_execution_allowed``) may set these.
+#:
+#: Deliberately narrow: ``models`` and ``tools`` stay trusted because their
+#: dangerous members (``base_url``, ``extra_workspace_roots``) are a different
+#: problem with a different fix, and lumping them in would break every project's
+#: model selection and tool list at once.
+EXECUTION_KEYS: frozenset[str] = frozenset({"hooks", "mcp"})
+
+
+def _project_execution_allowed() -> bool:
+    """Did the operator explicitly trust this project's execution settings?
+
+    Off by default.  The channel has to be one a repository cannot set for the
+    agent process, which is why it is an environment variable and not a config
+    key -- the same reasoning ``core/static_check.py`` documents.
+    """
+    return os.environ.get("AI_CODING_TRUST_PROJECT") == "1"
+
+
+def _drop_project_execution(document: dict[str, Any]) -> dict[str, Any]:
+    """Remove the execution keys a project layer tried to set (N-4)."""
+    return {k: v for k, v in document.items() if k not in EXECUTION_KEYS}
```

`load_layers_report` 的循环里（[loader.py:235-243](file:///workspace/ai-coding/ai_coding/config/loader.py#L235-L243)）：

```diff
     for _role, layer in ConfigLoader._layers_for(paths, path):
         document = ConfigLoader._read_layer(layer)
         if not document:
             continue
+        # N-4: a project layer may not grant execution.  Its hooks/mcp are
+        # dropped here, before they can be merged over the user's, and said so
+        # once -- a silent drop would read as "the feature is broken".
+        if _role == "project" and not _project_execution_allowed():
+            dropped = [k for k in document if k in EXECUTION_KEYS]
+            if dropped:
+                _log.warning(
+                    "project config ignored for %s (a repository cannot grant "
+                    "execution; set AI_CODING_TRUST_PROJECT=1 to allow it)",
+                    ", ".join(sorted(dropped)),
+                )
+                document = _drop_project_execution(document)
         if chosen is None or layer is not None:
             chosen = layer
         for key in document:
             sources[key] = str(layer)
         merged = ConfigLoader._overlay(merged, _normalize(document))
```

`__all__` 加 `"EXECUTION_KEYS"`。`import os` 补上（`loader.py` 现在没有）。

### 5.2 文档与配置样例

- `config.example.yaml`：`hooks` / `mcp` 段各加一句"项目层不生效，需 `AI_CODING_TRUST_PROJECT=1`"
- `docs/features.md` 的「安全与权限」章节：加一小节「项目配置不被信任授予执行」
- `docs/data-contracts.md`：记 `EXECUTION_KEYS` 与 `AI_CODING_TRUST_PROJECT`
- `docs/roadmap-milestones.md`：N-4 标 ✅

### 5.3 **不改**（明确边界）

- ❌ `hooks.py` / `mcp.py` 的执行逻辑（它们是对的，问题在谁能喂给它们）
- ❌ `models[].base_url` / `api_key`（外发类，**另一个问题**，见 §十一）
- ❌ `tools.extra_workspace_roots`（越权类，同上）
- ❌ `static_check.py`（已 env 门）
- ❌ `jobs.py` 的 `shell=True`（命令来自**模型**，有 bash 权限门）
- ❌ `acceptance.py` 的 verify 执行（有红线 + 越界 + 审批三道）
- ❌ 分层加载本身（"项目层覆盖用户层"对**行为参数**是对的）

---

## 六、数据流

### 6.1 改前

```
 ~/.config/ai-coding/config.yaml   ─┐
                                    ├─► _overlay（top wins）─► AppConfig
 <repo>/config.example.yaml        ─┘         ▲
                                             │ 项目层可设 hooks/mcp
                                             ▼
                              hooks.py 执行任意命令 / mcp.py spawn 任意进程
```

### 6.2 改后

```
 ~/.config/ai-coding/config.yaml   ─┐
                                    ├─► 逐层读取 ──┬─ role=user     → 全部保留
 <repo>/config.example.yaml        ─┘              └─ role=project  → 丢 EXECUTION_KEYS
                                                               (+ warning)
                                                               除非 AI_CODING_TRUST_PROJECT=1
                                             ▼
                              hooks.py / mcp.py 只看到用户层（或显式授权）的配置
```

### 6.3 架构图

```
                    ┌───────────────────────────────────────────┐
                    │ ConfigLoader.load_layers_report           │
                    │                                           │
   user layer ─────►│  for role, layer in _layers_for(...):     │
   (可信)           │      doc = _read_layer(layer)             │
                    │      if role == "project"                │
                    │         and not _project_execution_allowed():
                    │           doc = _drop_project_execution(doc)│  ◄── N-4
                    │           _log.warning(...)               │
   project layer ──►│      merged = _overlay(merged, doc)       │
   (不可信授予执行)  │                                           │
                    └───────────────────────────────────────────┘
                                        │
                                        ▼
                              AppConfig（hooks/mcp 只来自用户层）
                                        │
                    ┌───────────────────┴───────────────────┐
                    ▼                                       ▼
            hooks.py（有 command 才跑）              mcp.py（有 server 才 spawn）
```

---

## 七、TDD / RIPR

### 7.1 红相（三条，可机检）

```bash
# ① 项目层的 hooks 现在就能执行任意命令（改前）
#    写一个测试：用户层空 + 项目层 hooks.enabled:true + command:["touch X"]
#    → 断言 X 未生成（当前会生成 → 红）

# ② 项目层的 mcp server 现在就能 spawn（改前）
#    断言 mcp.servers 不含项目层那条（当前含 → 红）

# ③ 没有 warning（改前）
grep -c "AI_CODING_TRUST_PROJECT" ai_coding/config/loader.py   # 0
```

### 7.2 RIPR（缺一不可）

| 相 | 动作 | 判据 |
|---|---|---|
| **R** | 记录基线 | 项目层 hooks/mcp **生效**；无 warning |
| **I** | §5.1 丢弃逻辑 | 项目层 hooks/mcp **不再生效**；用户层的**仍生效** |
| **I** | env 门 | `AI_CODING_TRUST_PROJECT=1` 时项目层**恢复生效** |
| **P** | 用户层 hooks 回归 | `tests/test_hooks*.py` 全绿 |
| **P** | MCP 回归 | `tests/test_mcp*.py` 全绿 |
| **P** | 分层回归 | `tests/test_config_layers.py` 全绿 |
| **P** | 全量 `pytest` | **0 failed**，skipped ≤ 6 |
| **P** | `ruff` / `mypy ai_coding` | **0 / 0** |
| **P** | **突变 A**：把 `_drop_project_execution` 改成恒返回原 dict | 安全用例**重新变红** |
| **P** | **突变 B**：把 `_project_execution_allowed` 改成恒 True | env 门用例**重新变红** |
| **P** | **突变 C**：把 `role == "project"` 改成 `role == "user"` | 用户层回归用例**变红** |
| **R** | `git status --porcelain` 白名单 | 见 §7.3 |

### 7.3 R 相白名单

```
M  ai_coding/config/loader.py
M  config.example.yaml
M  docs/features.md
M  docs/data-contracts.md
M  docs/roadmap-milestones.md
?? docs/plan-config-trust.md
M  tests/test_config_layers.py
```

即 **1 个源文件 + 1 个测试文件 + 4 份文档 + 1 份方案**。

### 7.4 具体用例

| # | 用例 | 类型 | 期望 |
|---|---|---|---|
| U1 | 用户层空 + 项目层 `hooks.enabled:true` + `command:["touch X"]` | **安全** | `X` **未生成**；`hooks.enabled is False` |
| U2 | 同上，但 `mcp.enabled:true` + `servers:[{command:[...]}]` | **安全** | `mcp.servers == []` |
| U3 | 用户层配了 hooks + 项目层也配 | 回归 | 用户层的**保留**（不被丢弃） |
| U4 | 用户层空 + `AI_CODING_TRUST_PROJECT=1` + 项目层 hooks | 逃生门 | 项目层的**生效** |
| U5 | 项目层设了 `hooks` 与 `mcp` 之外的 key（如 `default_model`） | 回归 | **照常生效**（证明只丢执行类） |
| U6 | `sources["hooks"]` 在丢弃后指向**用户层** | 契约 | 断言来源 |
| U7 | 丢弃时**有 warning** | 可观测 | `caplog` 捕获含 `AI_CODING_TRUST_PROJECT` |
| U8 | 项目层**没有** hooks/mcp 时**无 warning** | 静默 | 不产生噪音 |
| U9 | 显式 `--path`（单文件，不分层） | 边界 | **不受影响**（`explicit` 层不是 `project`） |

> U9 重要：`--path` 是操作者显式给的，语义上就是"我信这个文件"，不该被丢弃。

---

## 八、修改成功后的效果

| 观测点 | 改前 | 改后 |
|---|---|---|
| 项目层的 `hooks.command` | **执行任意命令** | 丢弃 + warning |
| 项目层的 `hooks.module` | **执行任意 Python** | 丢弃 + warning |
| 项目层的 `mcp.servers[].command` | **spawn 任意进程** | 丢弃 + warning |
| 用户层的 hooks/mcp | 生效 | **仍生效** |
| 项目层的 `default_model` / `max_tool_iterations` | 生效 | **仍生效**（只丢执行类） |
| `AI_CODING_TRUST_PROJECT=1` | — | 项目层的 hooks/mcp **恢复生效** |
| `static_check` | env 门 | 不变 |
| 操作者体验 | — | 要配 hooks/MCP 就写 `~/.config/ai-coding/config.yaml`（或开 env） |

### 8.1 按此实施后的最终模样

- `ai-coding` 在一个带 `config.example.yaml`（含 `mcp.servers`）的仓库里启动 → 日志一行 warning，MCP 不加载
- 操作者在 `~/.config/ai-coding/config.yaml` 里配好 MCP → 正常加载
- 操作者确实信任该仓库 → `AI_CODING_TRUST_PROJECT=1 ai-coding` → 项目配置生效
- `ruff`/`mypy` 全绿；全量 pytest 0 failed，skipped 仍为 6
- `docs/features.md` 的「安全与权限」多一节，写清"项目配置不被信任授予执行"

---

## 九、待拍板（2 项）

### 9.1 逃生门的形态

- **① 环境变量 `AI_CODING_TRUST_PROJECT=1`（推荐）**：与 `static_check` 同一模式，仓库碰不到。
- **② 首次启动时的一次性确认**：TUI 弹"该仓库的配置含执行项，是否信任？"，答"总是"则落盘。
  更安全但**要新增持久化层**（`GrantStore` 已有，但要扩语义），工作量大。
- **③ 无逃生门**：项目层的 hooks/MCP 永远不生效，操作者必须写用户层配置。

### 9.2 本轮修到哪一层

- **① 只修"能执行代码/命令"的（`hooks` + `mcp`）（推荐）**：直击 RCE，改动 1 个文件。
  `models[].base_url` / `extra_workspace_roots` 是"外发/越权"类，**修法不同**（前者要区分"端点白名单"，后者要"项目层只能缩小不能扩大"），本里程碑不混进来。
- **② 一并修 `models` + `tools`**：一次收口所有高权限字段，但要给每个字段定义"收紧/放宽"语义，**估算 +200 行、易误伤**（比如项目的 `tools.enabled` 是常见配置）。

---

## 十、自我核验

1. **改的是根源吗？** ✅ 是。根源是"`_overlay` 让项目层覆盖用户层，且没有'哪些 key 能被覆盖'的概念"。我在**分层读取处**按 role 过滤，不是在消费处打补丁（那样要改 hooks/mcp 两处且会漏）。
2. **会不会误伤项目级功能？** ⚠️ 会 —— 项目自带的 hooks/MCP 配置不再自动生效。这是**有意的行为变更**，所以给了逃生门，并写进文档。`default_model` / `max_tool_iterations` 等**不受影响**（U5 锁）。
3. **`--path` 会不会被误伤？** ✅ 不会。`_layers_for` 对 explicit 返回 `[("explicit", path)]`，role 不是 `project`（U9 锁）。
4. **`sources` 语义会变吗？** ⚠️ 会变：丢弃后 `sources["hooks"]` 指向用户层。这是**正确的**（值确实来自用户层），且 U6 锁住。
5. **会不会重复造轮子？** ✅ 没有。复用了 `_layers_for` / `_read_layer` / `_overlay` / `LayeredConfig`，以及 `static_check` 的 env 门模式。
6. **丢弃会不会静默？** ✅ 不会，有 `_log.warning`（U7/U8 锁）。
7. **既有测试会破吗？** ⚠️ 可能 —— 如果有测试用"项目层配 MCP/hooks"来验证分层。实施时先跑 `tests/test_config_layers.py` 确认；若破，**那是测试写错了**（它假设项目层可授予执行），需要改测试并说明理由。
8. **`ConfigManager` 要不要一起改？** ❌ 不用 —— 它**零消费者**，且走单文件路径（没有分层，就没有"项目层覆盖"问题）。

---

## 十一、边界声明

| 项 | 归属 |
|---|---|
| **本方案改** | `config/loader.py`（1 个文件）+ 1 个测试文件 + 4 份文档 |
| **不改** | `hooks.py` / `mcp.py` / `static_check.py` / `jobs.py` / `acceptance.py` / 分层加载本身 |
| **不做（另立项）** | `models[].base_url` / `api_key`（外发）、`tools.extra_workspace_roots`（越权）、`mcp.servers[].headers`（token 复用） |
| **已修（M-B）** | `static_check.commands` 的 env 门 + argv + 白名单 |
