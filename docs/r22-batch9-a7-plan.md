# 批次 9-5 方案（R22·A-7 `/config` 与 `/status` 的模型字段口径）

> 只出方案。依据：`docs/r22-feature-matrix.md` §5 A-7、真机帧 `08-status` / `10-config`。

## 1. 调研

| 面板 | 代码 | 实际打印 | 数据来源 |
| --- | --- | --- | --- |
| `/config` | `app/commands.py:303` | `[config] model: qwen` | `loop.default_model`（配置键，指向 `models.qwen`） |
| `/status` | `frontend/component/dialog-status.tsx:38` | `Model  127 · qwen3.7-flash` | `hello.provider` + `hello.model`（运行时 provider/模型名） |

配置形状（`config.yaml`）：

```yaml
models:
  qwen: { name: qwen3.7-flash, ... }   # 键 = 配置里的别名；name = 线上模型名
default_model: qwen
```

所以两处都没"错"，错的是 `/config` 把这个键**标成了 `model:`**，读者会把它当成模型名，于是
`qwen` 看起来像 `qwen3.7-flash` 被截断了。`loop.models: dict[str, ModelConfig]` 与
`ModelConfig.name`（`domain/model_config.py`）已经能解析出线上名，不需要新数据源。

## 2. 方案（一处文案 + 一个解析）

**FIX-A7-1：`_handle_config` 打真实配置键，并附解析出的模型名。**

```python
key = getattr(loop, "default_model", "") or "?"
config = getattr(loop, "models", {}).get(key)
resolved = getattr(config, "name", "") or ""
model = f"{key}（{resolved}）" if resolved and resolved != key else key
lines = [f"[config] default_model: {model}", ...]
```

- 键名与 `config.yaml` 的 `default_model` 完全一致，不再冒充模型名；
- 括号里是 `/status` 显示的同一个线上名，两个面板可以互相对照；
- `loop.models` 没有该项（测试 stub、旧调用）时退回只打键，行为与今天兼容。

**不动 `/status`**：它描述的是运行时（provider + 模型名），语义本来就不同，且已有真机帧。

## 3. 影响面

- 只有 `/config` 的第一行文本（后端 REPL/TUI notice）；
- 无 IPC 契约、无前端、无配置读取逻辑变化；
- 既有 `test_repl.py::test_handle_config_shows_settings` 仍含 `gpt-4`（stub 无 `models`，
  退回只打键），不需要改断言；新增两条精确断言。

## 4. RIPR

### Red
`tests/test_repl.py` 新增：
| 用例 | 断言 |
| --- | --- |
| `test_config_names_the_key_and_the_resolved_model` | `loop.models={"gpt-4": S(name="gpt-4-turbo")}`，输出含 `default_model: gpt-4（gpt-4-turbo）` |
| `test_config_keeps_the_bare_key_when_it_is_the_model_name` | `name == key` 或没有 `models` → 输出 `default_model: gpt-4`，不含 `（` |
| 既有 `test_handle_config_shows_settings` | 不改：仍断言含 `gpt-4` 与 `48000` |

### Implement
`app/commands.py::_handle_config` 一处。

### Prove
`pytest tests/test_repl.py -q`、`ruff`、`mypy`。

### Refactor
无新 helper；解析三行内联（只在 `/config` 用一次）。

## 5. 真机与验收

`r22-s1` TUI 输入 `/config`，第一行应为
`[config] default_model: qwen（qwen3.7-flash）`，其余四行不变；帧存 `/tmp/r22-shots/164-*`。

## 5. 完成记录

- 实现：`_handle_config` 解析 `loop.models[key].name`，行改为 `default_model: key（name）`；
  `models` 不是 dict（stub/None）时退回只打键。
- 评审修正：`getattr(loop,"models",{}).get` 在 `models=None` 会崩，改为 `isinstance` 判定；
  既有 `test_handle_config_shows_settings` 补 `[config] default_model:` 断言，防止标签回退。
- 测试：`test_repl.py + test_todo_command.py` 62 例绿；ruff/mypy 干净。
- 真机（帧 164）：`[config] default_model: qwen（qwen3.7-flash）`。
