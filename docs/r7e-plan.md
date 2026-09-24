# R7e Implementation Plan（记忆写 / 转向 / 工具面告警 / 子智能体上下文模式）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让模型能自己写一条耐久记忆、让用户能在回合进行中补一句话、让工具面超支时主动报警、
让子智能体可以选择「干净窗口」——四件互不依赖，任何一件单独上线都完整可用。

**Architecture:** 四件都挂在既有接缝上，不新增子系统：记忆写走 `MemoryService._reconcile`
（R3 起就是唯一写入口）；转向走「一个读取器 + 队列 + 迭代边界抽干」；工具面告警走
`AgentLoop` 构造时就已知的静态数字；子智能体上下文模式走 `SubAgentRunner` 的起始 session。
四者共同遵守 R3（缓存前缀）、R4（观测只写不改）、R7b（可回收）、R7c（一个终端一个读取器）。

**Tech Stack:** Python 3.13（mypy 按 3.10 下限）、asyncio、pydantic v2、Typer/rich/
prompt_toolkit（`prompt_async` + `patch_stdout`）、pytest（`pytest.mark.asyncio`）、ruff、mypy。

**Spec:** `docs/r7e-analysis.md`（本文的每条判断都出自它的 §1 实测、§2 一手材料与 §3 设计；
§12 的决策点已按「推荐」锁定，见下方 Global Constraints）

## Global Constraints

以下每一条都是 `docs/r7e-analysis.md` §12 拍板后的结果，**每个任务都隐含包含**：

- **Q1**：`memory_write` 门卫档位 = **ALLOW**（与后台 harvest 一致；靠 `evidence="model"` 与
  `/memory` 可见性治理，不做写入前问人）。
- **Q2**：备注参数 = `text / kind / importance / supersedes`；**文本上限 500 字符**，超出截断并在结果里说明。
- **Q3**：**子智能体不注册任何记忆工具**（连 `memory_search` 都没有）。
- **Q4**：steering 行**持久化**为 `[steering] …` 的 user 消息（回放保真，同 `[task-gate]` 的理由）。
- **Q5**：steering **重置失败连胜**（`failed_streak`），**不动** `attempted_calls`/`successful_calls`。
- **Q6**：steering 上限 = 每回合 **5** 条、每条 **2000** 字符；超出留在队列；
  **以 `/` 开头的行不当作 steering**，留到回合结束交回 REPL；`ui.steering` 默认 **true**。
- **Q7**：`ai.tools_chars_warn` 默认 **12000**（0 = 关闭）；告警**每个 loop 一次**，纯观测，不进请求。
- **Q8**：`ai.subagent_context_mode` 默认 **`full`**（今天的行为）；`task_only` 用组合根的
  `BASE_INSTRUCTIONS`，**空串必须落到 `prompt=None`**（否则打破既有断言
  `test_subagent_without_parent_uses_empty_history`）。
- **Q9**：R7e-2 的终端侧**要做**，但**先跑 Task 10 的 Phase 0 spike**；任何一项不通过就停下报告，
  只交付循环侧（Task 11/12），终端侧另开一轮。
- **既有断言一条都不改**；新增测试优先。
- 不新增 `TurnStatus` 成员；新状态用事件 / `ToolResult.error` / 包内字段表达。
- 默认值不得改变未配置用户的行为（`subagent_context_mode=full`、`tools_chars_warn` 只在超阈值时发一次
  且不改请求、`memory_write` 是**新增工具**、`ui.steering` 是 UI 层）。
- 每个任务结束跑：
  `.venv/bin/python -m pytest tests/ -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
- 提交信息含反引号时必须 `git commit -F <file>`。

---

## File Structure

| 文件 | 职责 | 任务 |
|---|---|---|
| `ai_coding/config/models.py` | 4 个 `AIConfig` 键 + 新 `UIConfig`（`AppConfig.ui`） | 1 |
| `ai_coding/ai/prompt.py` | 新增 `BASE_INSTRUCTIONS`（默认系统提示词文本的唯一出处） | 3 |
| `ai_coding/domain/subagent.py` | `SubagentResult.context_mode` | 2 |
| `ai_coding/core/subagent.py` | 起始 session 的模式分叉 + `base_instructions` + `context_mode` 属性 | 2 |
| `ai_coding/tools/subagent_tool.py` | 描述按模式措辞（干净窗口时要求把背景写进 task） | 3 |
| `ai_coding/tools/builder.py` | 把 `base_instructions` 传进 runner | 3 |
| `ai_coding/cli.py` | 传 `BASE_INSTRUCTIONS`；注册 `memory_write` | 3, 9 |
| `ai_coding/tools/base.py` | `BaseTool.origin`（默认 `"local"`）+ `ToolContext.turn` | 4, 8 |
| `ai_coding/core/mcp.py` | `MCPToolAdapter.origin` = server 名 | 4 |
| `ai_coding/core/events.py` | `ToolSurfaceWarning` / `InputSteered` | 5, 12 |
| `ai_coding/core/agent_loop.py` | 工具面分母与一次性告警；steering 抽干；`turn` 传给工具上下文 | 5, 12 |
| `ai_coding/ui/console.py` | 渲染 `ToolSurfaceWarning` 一行 | 6 |
| `ai_coding/memory/service.py` | `write_note()`（复用 `_reconcile`） | 7 |
| `ai_coding/tools/memory_write_tool.py`（新） | `memory_write` 工具 | 8 |
| `ai_coding/core/request_builder.py` | `tool_context(..., turn=0)` | 8 |
| `ai_coding/security/permission_gate.py` | allow 表加 `memory_write` | 9 |
| `ai_coding/core/steering.py`（新） | `SteeringQueue`（有界 FIFO） | 11 |
| `ai_coding/core/turn_books.py` | `steering_messages` + `note_steering` | 11 |
| `ai_coding/ui/reader.py`（新） | `InteractiveReader`（唯一读取器 + 仲裁） | 13 |
| `ai_coding/ui/repl.py` | `run_repl(..., reader=None)` | 13 |
| `ai_coding/security/user_prompt.py` | reader 代答路径 | 14 |
| `scripts/r7e_cases.py`（新） | 验收脚本（离线 + 3 真实） | 15 |

---

### Task 1: 配置面（4 个 AI 键 + `UIConfig`）

**Files:**
- Modify: `ai_coding/config/models.py`（`AIConfig` 内；`AppConfig` 内）
- Test: `tests/test_config_loader.py`（**只新增**用例，不改既有断言）

**Interfaces:**
- Produces: `AIConfig.subagent_context_mode: Literal["full","task_only"]`、
  `AIConfig.tools_chars_warn: int`、`AIConfig.steering_max_messages: int`、
  `AIConfig.steering_max_chars: int`、`UIConfig.steering: bool`、`AppConfig.ui: UIConfig`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_loader.py —— 追加到文件末尾
def test_r7e_defaults_keep_every_behaviour_unchanged():
    ai = AIConfig()
    assert ai.subagent_context_mode == "full"  # Q8: 今天的行为
    assert ai.tools_chars_warn == 12_000  # Q7
    assert ai.steering_max_messages == 5  # Q6
    assert ai.steering_max_chars == 2_000  # Q6
    assert AppConfig().ui.steering is True  # Q6


def test_an_unknown_subagent_context_mode_is_rejected_at_load():
    with pytest.raises(ValidationError):
        AIConfig(subagent_context_mode="clean")


def test_the_ui_block_is_independent_of_the_rest():
    cfg = AppConfig.model_validate({"ui": {"steering": False}})
    assert cfg.ui.steering is False
    assert cfg.ai.subagent_context_mode == "full"
```

（同一次改动里把 `from pydantic import ValidationError` 与
`from ai_coding.config.models import AIConfig, AppConfig` 补到文件顶部既有 import 处。）

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_config_loader.py -o addopts="" -q`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'subagent_context_mode'`

- [ ] **Step 3: Implement**

```python
# ai_coding/config/models.py —— AIConfig 内，subagent_timeout_seconds 之后
    # R7e-4: how much of the parent a sub-agent starts with.  "full" is the R1a-5
    # behaviour (the parent's request view + system prompt); "task_only" gives it a
    # clean window and the composition root's base instructions instead, which is
    # what Anthropic's sub-agent guidance describes - and it means the parent MUST
    # put every fact the child needs into the task text (docs/r7e-analysis.md 3.4).
    subagent_context_mode: Literal["full", "task_only"] = "full"
    # R7e-3: warn once per loop when the serialised tool schemas cost more than
    # this.  0 disables.  Advisory only: the request is byte-identical either way
    # (measured: 4,280 chars without MCP, 13,301 with a 13-tool server).
    tools_chars_warn: int = 12_000
    # R7e-2: what one turn may absorb from a user who keeps typing (docs §12 Q6).
    steering_max_messages: int = 5
    steering_max_chars: int = 2_000


class UIConfig(BaseModel):
    """Terminal-facing switches (R7e-2)."""

    model_config = ConfigDict(extra="ignore")

    #: Show a steering prompt while a turn is running.  Off = today's quiet REPL.
    steering: bool = True


# ai_coding/config/models.py —— AppConfig 内，mcp 之后
    ui: UIConfig = Field(default_factory=UIConfig)
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: **945 passed**（942 + 3），2 skipped

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c1.txt <<'MSG'
R7e-4a: the switches come first, and every default is today's behaviour

subagent_context_mode ("full" | "task_only"), tools_chars_warn (12000, 0 = off),
steering_max_messages (5) and steering_max_chars (2000) on AIConfig, plus a small
UI block for the steering prompt.  Nothing reads them yet; a bad mode fails at
config load because it is a Literal, exactly like stop_policy.
MSG
git add ai_coding/config/models.py tests/test_config_loader.py
git commit -F /tmp/c1.txt
```

---

### Task 2: 子智能体的两种起始上下文

**Files:**
- Modify: `ai_coding/domain/subagent.py`（`SubagentResult`）
- Modify: `ai_coding/core/subagent.py`（`__init__` / `_run_in` / `_packet`）
- Test: `tests/test_subagent_context_mode.py`（新）

**Interfaces:**
- Consumes: `AIConfig.subagent_context_mode`（Task 1）
- Produces: `SubAgentRunner(..., base_instructions: str = "")`；
  `SubAgentRunner.context_mode -> str`；`SubagentResult.context_mode: str = "full"`（含 `to_dict()`）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_context_mode.py
"""R7e-4: a sub-agent can be given a clean window instead of the parent's view."""

from __future__ import annotations

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.subagent import ParentContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append({"messages": list(messages), "instructions": instructions})
        return StepResult(text="做完了", tool_calls=[], finish_reason="stop")


def _runner(engine, *, base_instructions: str = "BASE INSTRUCTIONS", **overrides):
    cfg = AIConfig(max_tool_iterations=2, stop_policy="natural", **overrides)
    return SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
        base_instructions=base_instructions,
    )


def _parent() -> ParentContext:
    return ParentContext(
        messages=(ChatMessage(role="user", content="parent-said-this", timestamp="t"),),
        instructions="PARENT INSTRUCTIONS",
    )


@pytest.mark.asyncio
async def test_the_default_mode_is_full():
    engine = _Engine()
    result = await _runner(engine).run("do it", parent=_parent())
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert any("parent-said-this" in c for c in contents)
    assert engine.requests[0]["instructions"] == "PARENT INSTRUCTIONS"
    assert result.context_mode == "full"


@pytest.mark.asyncio
async def test_task_only_starts_with_an_empty_history():
    engine = _Engine()
    await _runner(engine, subagent_context_mode="task_only").run("do it", parent=_parent())
    contents = [m.content or "" for m in engine.requests[0]["messages"]]
    assert not any("parent-said-this" in c for c in contents)
    assert any("do it" in c for c in contents)  # 任务本身还在


@pytest.mark.asyncio
async def test_task_only_uses_the_base_instructions():
    engine = _Engine()
    await _runner(engine, subagent_context_mode="task_only").run("do it", parent=_parent())
    assert engine.requests[0]["instructions"] == "BASE INSTRUCTIONS"


@pytest.mark.asyncio
async def test_task_only_with_no_base_instructions_has_no_system_prompt():
    """兼容性护栏：既有用例断言 instructions is None，空串不能变成空 PromptAssembler。"""
    engine = _Engine()
    cfg = AIConfig(subagent_context_mode="task_only")
    runner = SubAgentRunner(
        cfg,
        engine_factory=lambda: engine,
        build_tools=lambda workdir: ToolRegistry(),
        settings=cfg,
        workspace=".",
    )
    await runner.run("do it", parent=_parent())
    assert engine.requests[0]["instructions"] is None


@pytest.mark.asyncio
async def test_the_packet_reports_the_mode():
    result = await _runner(_Engine(), subagent_context_mode="task_only").run("t", parent=_parent())
    assert result.context_mode == "task_only"
    assert result.to_dict()["context_mode"] == "task_only"


@pytest.mark.asyncio
async def test_full_mode_still_inherits_the_parent_instructions():
    engine = _Engine()
    runner = _runner(engine)
    assert runner.context_mode == "full"
    await runner.run("t", parent=_parent())
    assert engine.requests[0]["instructions"] == "PARENT INSTRUCTIONS"


@pytest.mark.asyncio
async def test_task_only_without_a_parent_uses_the_base_instructions():
    engine = _Engine()
    await _runner(engine, subagent_context_mode="task_only").run("t")
    assert engine.requests[0]["instructions"] == "BASE INSTRUCTIONS"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_subagent_context_mode.py -o addopts="" -q`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'base_instructions'`

- [ ] **Step 3: Implement**

```python
# ai_coding/domain/subagent.py —— SubagentResult 内，ledger_path 之后
    context_mode: str = "full"  # full | task_only (R7e-4)

# 同文件 to_dict() 内，"ledger_path" 那一行之后
            "context_mode": self.context_mode,
```

```python
# ai_coding/core/subagent.py —— __init__ 的只读参数区，observer 之后
        base_instructions: str = "",
    ) -> None:
# 同一 __init__ 的赋值区
        # R7e-4: what a task_only child gets instead of the parent's system prompt.
        self._base_instructions = str(base_instructions or "")

# 新属性，放在 timeout_seconds 之后
    @property
    def context_mode(self) -> str:
        """``full`` (the parent's view) or ``task_only`` (a clean window), R7e-4."""
        return str(getattr(self._settings, "subagent_context_mode", "full") or "full")

# _run_in 内：替换原来的 session 与 prompt 构造（原第 196–205 行那一段）
        mode = self.context_mode
        if mode == "task_only":
            # A clean window: the child sees the task and nothing else.  The parent
            # is responsible for putting the facts into `task` (docs §3.4).  An empty
            # base string must stay None - a blank PromptAssembler is a different
            # request, not an equivalent one (it would break an existing assertion).
            seeded: list[ChatMessage] = []
            prompt = (
                PromptAssembler(self._base_instructions)
                if self._base_instructions
                else None
            )
        else:
            seeded = list(parent.messages) if parent is not None else []
            prompt = (
                PromptAssembler(parent.instructions)
                if parent is not None and parent.instructions
                else None
            )
        session = SessionData(
            session_id=f"sub-{uuid.uuid4().hex[:12]}",
            title=name,
            created_time=ts,
            last_access_time=ts,
            messages=seeded,
        )

# _packet 签名加参数，并在返回里带上模式
    def _packet(
        self, loop: AgentLoop, session: SessionData, workdir: str | None, mode: str
    ) -> SubagentResult:
        ...
            context_mode=mode,

# _run_in 末尾
        return self._packet(loop, session, workdir, self.context_mode)
```

（`core/subagent.py` 顶部若未导入 `ChatMessage`，补
`from ai_coding.domain.message import ChatMessage`。）

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_subagent_context_mode.py tests/test_subagent_runner.py -o addopts="" -q`
Expected: 新增 7 passed；既有 `test_subagent_runner.py` **零改动**通过

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c2.txt <<'MSG'
R7e-4b: a sub-agent can be given a clean window

task_only seeds an empty session and uses the composition root's base
instructions; full is untouched, so the existing assertions (parent messages,
parent instructions, no-parent means no system prompt) still describe the default
exactly.  An empty base string must stay None - a blank PromptAssembler would be a
different request, not an equivalent one.

The packet now says which mode ran, so the parent turn, the trace and /trace can
tell a clean window from an inherited one.
MSG
git add ai_coding/domain/subagent.py ai_coding/core/subagent.py tests/test_subagent_context_mode.py
git commit -F /tmp/c2.txt
```

---

### Task 3: 组合根的基础提示词，与「子智能体看不到什么」的措辞

**Files:**
- Modify: `ai_coding/ai/prompt.py`（新增常量）
- Modify: `ai_coding/tools/subagent_tool.py`（描述）
- Modify: `ai_coding/tools/builder.py`（`build_subagent_runner` / `build_tool_registry` 透传）
- Modify: `ai_coding/cli.py`（`_build_tool_registry` 透传；`_build_loop` 用常量）
- Test: `tests/test_tool_subagent.py`（**只新增**用例）

**Interfaces:**
- Consumes: `SubAgentRunner(..., base_instructions=...)`、`SubAgentRunner.context_mode`（Task 2）
- Produces: `ai_coding.ai.prompt.BASE_INSTRUCTIONS: str`；
  `build_subagent_runner(app_cfg, workspace, skills_path, *, base_instructions="")`；
  `build_tool_registry(..., base_instructions="")`；`_build_tool_registry(..., base_instructions="")`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_subagent.py —— 追加
def test_the_description_promises_shared_context_in_full_mode():
    from ai_coding.tools.subagent_tool import SubAgentTool

    runner = SimpleNamespace(timeout_seconds=10.0, context_mode="full")
    text = SubAgentTool(runner=runner).description
    assert "same conversation context" in text
    assert "cannot see" not in text


def test_the_description_warns_that_a_clean_window_cannot_see_the_conversation():
    from ai_coding.tools.subagent_tool import SubAgentTool

    runner = SimpleNamespace(timeout_seconds=10.0, context_mode="task_only")
    text = SubAgentTool(runner=runner).description
    assert "cannot see" in text and "task" in text
```

（`tests/test_tool_subagent.py` 顶部已有 `from types import SimpleNamespace`？没有就补。）

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_subagent.py -o addopts="" -q -k description`
Expected: FAIL —— 现有描述里没有 `cannot see`

- [ ] **Step 3: Implement**

```python
# ai_coding/ai/prompt.py —— 模块级常量，放在 PromptAssembler 之前
#: The composition root's default system prompt.  A sub-agent in ``task_only`` mode
#: gets exactly this instead of its parent's assembled prompt (R7e-4), so the text
#: has one home instead of being repeated in cli.py and the runner.
BASE_INSTRUCTIONS = (
    "You are an AI coding assistant. Respond to the user's coding questions "
    "clearly and concisely, in the same language the user writes in."
)

# __all__ 同步
__all__ = ["BASE_INSTRUCTIONS", "PromptAssembler"]
```

```python
# ai_coding/tools/subagent_tool.py —— 替换现有 description 属性
    @property
    def description(self) -> str:
        # R7e-4: say which window the child gets.  In task_only mode the model is the
        # only one who can hand over the facts, so the description has to ask for it.
        if getattr(self._runner, "context_mode", "full") == "task_only":
            window = (
                "starts with a CLEAN context: it cannot see this conversation, so put "
                "every fact, path and decision it needs into 'task'"
            )
        else:
            window = "sees the same conversation context and system prompt as you do"
        return (
            f"Delegate a task to a sub-agent that {window}, then returns a structured "
            "packet: its conclusion, any unfinished task items, changed files, and a "
            "ledger path holding the full report. Use 'task' for what to do and "
            "optionally 'name' for a short agent label. The sub-agent's own reasoning "
            "is not returned."
        )
```

```python
# ai_coding/tools/builder.py —— build_subagent_runner 签名与调用
def build_subagent_runner(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    *,
    base_instructions: str = "",
) -> Any:
    ...
    return SubAgentRunner(
        app_cfg,
        engine_factory=lambda: build_engine_from_config(app_cfg),
        build_tools=build_tools,
        ledger_root=main_ws,
        max_concurrent=int(getattr(ai_cfg, "max_concurrent_subagents", 3)),
        timeout_seconds=float(getattr(ai_cfg, "subagent_timeout_seconds", 600)),
        settings=ai_cfg if isinstance(ai_cfg, AIConfig) else AIConfig(),
        approval=AutoApproveCallback(),
        workspace=main_ws,
        # R7e-4: a task_only child gets this instead of the parent's prompt.
        base_instructions=base_instructions,
    )

# build_tool_registry / _assemble_registry 各加一个关键字参数并透传
def build_tool_registry(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None = None,
    *,
    include_subagent: bool = True,
    user_prompt: Any | None = None,
    base_instructions: str = "",
) -> Any:
    registry, _gate = _assemble_registry(
        app_cfg, workspace, skills_path, include_subagent, user_prompt, base_instructions
    )
    return registry

# _assemble_registry 内注册 subAgent 的那一段
        runner = build_subagent_runner(
            app_cfg, workspace, skills_path, base_instructions=base_instructions
        )
```

```python
# ai_coding/cli.py —— _build_tool_registry 透传
def _build_tool_registry(
    app_cfg: Any,
    workspace: str,
    skills_path: str | None,
    *,
    user_prompt: Any | None = None,
    base_instructions: str = "",
) -> Any:
    from ai_coding.tools.builder import build_tool_registry

    return build_tool_registry(
        app_cfg,
        workspace,
        skills_path,
        include_subagent=True,
        user_prompt=user_prompt,
        base_instructions=base_instructions,
    )

# _build_loop 内：把本地硬编码的 base_instructions 换成常量
    from ai_coding.ai.prompt import BASE_INSTRUCTIONS, PromptAssembler
    prompt = PromptAssembler(BASE_INSTRUCTIONS)

# repl() / ask 里调用 _build_tool_registry 的地方补
        registry = _build_tool_registry(
            app_cfg, workspace, skills, user_prompt=user_prompt,
            base_instructions=BASE_INSTRUCTIONS,
        )
```

（`cli.py` 的 `repl()` 需在函数内 `from ai_coding.ai.prompt import BASE_INSTRUCTIONS`；
`_run_turn_with_mcp` 的调用点同样补 `base_instructions=BASE_INSTRUCTIONS`。）

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: 947 passed（945 + 2），2 skipped

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c3.txt <<'MSG'
R7e-4c: the base prompt gets one home, and the child knows what it cannot see

BASE_INSTRUCTIONS moves into ai/prompt.py, so the composition root and a
task_only sub-agent share the same text instead of cli.py hardcoding it.  The
subAgent description now depends on the mode: a clean-window child cannot see the
conversation, and the only one who can hand the facts over is the model writing
the task - so the description says so.
MSG
git add ai_coding/ai/prompt.py ai_coding/tools/subagent_tool.py ai_coding/tools/builder.py ai_coding/cli.py tests/test_tool_subagent.py
git commit -F /tmp/c3.txt
```

---

### Task 4: 每个工具知道自己的来源

**Files:**
- Modify: `ai_coding/tools/base.py`（`BaseTool.origin`）
- Modify: `ai_coding/core/mcp.py`（`MCPToolAdapter.origin`）
- Test: `tests/test_tool_base.py`（**只新增**用例）

**Interfaces:**
- Produces: `BaseTool.origin -> str`（默认 `"local"`）；`MCPToolAdapter.origin -> str`（server 名）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_base.py —— 追加
def test_a_local_tool_says_where_it_came_from():
    from types import SimpleNamespace

    from ai_coding.core.mcp import MCPToolAdapter
    from ai_coding.tools.read_tool import ReadTool  # 任意一个内置工具

    class _Tool:
        @property
        def origin(self) -> str:
            return "local"

    assert _Tool().origin == "local"  # 这行的目的是让内置工具的默认值有名字
    assert ReadTool.__mro__  # 内置工具不覆写 origin: 见 Step 3 的断言

    adapter = MCPToolAdapter(
        server="everything", tool=SimpleNamespace(name="read_file"), session=object()
    )
    assert adapter.origin == "everything"
```

（更直接的两条断言放进 `tests/test_mcp.py` 与 `tests/test_base_tool.py` 也可以；
关键是**两个方向**都被锁住：默认 `local`、MCP 是 server 名。）

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_base.py -o addopts="" -q -k origin`
Expected: FAIL —— `AttributeError: 'MCPToolAdapter' object has no attribute 'origin'`

- [ ] **Step 3: Implement**

```python
# ai_coding/tools/base.py —— BaseTool 内，parallel_safe 之后
    @property
    def origin(self) -> str:
        """Where this tool came from: ``"local"``, or an MCP server's name (R7e-3).

        The tool surface is paid for on every model step, so a cost warning has to
        name the expensive part.  The default keeps every built-in tool out of it.
        """
        return "local"
```

```python
# ai_coding/core/mcp.py —— MCPToolAdapter 内，name 之后
    @property
    def origin(self) -> str:
        """The server this tool belongs to (R7e-3: the tool surface is per server)."""
        return self._server
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: 949 passed（947 + 2），2 skipped

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c4.txt <<'MSG'
R7e-3a: every tool says where it came from

BaseTool.origin defaults to "local"; MCPToolAdapter returns its server.  Nothing
reads it yet - the next task turns it into a per-origin breakdown, so the cost
warning can name the server that is actually expensive instead of printing one
number nobody can act on.
MSG
git add ai_coding/tools/base.py ai_coding/core/mcp.py tests/test_tool_base.py
git commit -F /tmp/c4.txt
```

---

### Task 5: 工具面超阈值时喊一次

**Files:**
- Modify: `ai_coding/core/events.py`（新事件 `ToolSurfaceWarning`）
- Modify: `ai_coding/core/agent_loop.py`（分母 + 一次性告警）
- Test: `tests/test_tool_surface_warning.py`（新）

**Interfaces:**
- Consumes: `BaseTool.origin`（Task 4）、`AIConfig.tools_chars_warn`（Task 1）
- Produces: `ToolSurfaceWarning(tools_chars: int, threshold: int, origins: tuple[tuple[str,int],...], hint: str)`；
  `AgentLoop._tools_by_origin: dict[str, int]`；`AgentLoop._warn_tools_chars_once()`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_surface_warning.py
"""R7e-3: the tool surface is paid for every step, so it says when it is expensive."""

from __future__ import annotations

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import RecordingObserver, ToolSurfaceWarning
from ai_coding.domain.loop import StepResult
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text="好", tool_calls=[], finish_reason="stop")


class _Tool(BaseTool):
    parallel_safe = True

    def __init__(self, name: str, description: str, origin: str = "local") -> None:
        self._name, self._description, self._origin = name, description, origin

    @property
    def name(self) -> str: return self._name
    @property
    def description(self) -> str: return self._description
    @property
    def params_json_schema(self): return {"type": "object", "properties": {}}
    @property
    def origin(self) -> str: return self._origin
    async def execute(self, params, ctx: ToolContext | None = None):
        raise NotImplementedError


class _Sessions:
    def save(self, session: SessionData) -> None: ...


def _loop(*tools: BaseTool, **overrides) -> tuple[AgentLoop, RecordingObserver]:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    observer = RecordingObserver()
    return (
        AgentLoop(
            _Engine(),
            _Sessions(),
            registry,
            approval=AutoApproveCallback(),
            settings=AIConfig(max_tool_iterations=1, stop_policy="natural", **overrides),
            observer=observer,
        ),
        observer,
    )


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_no_warning_below_the_threshold():
    loop, observer = _loop(_Tool("tiny", "x"))
    await loop.process_input(_session(), "一")
    assert observer.of_type(ToolSurfaceWarning) == []


@pytest.mark.asyncio
async def test_one_warning_above_the_threshold():
    loop, observer = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=100)
    await loop.process_input(_session(), "一")
    warnings = observer.of_type(ToolSurfaceWarning)
    assert len(warnings) == 1
    assert warnings[0].tools_chars > warnings[0].threshold == 100


@pytest.mark.asyncio
async def test_the_warning_is_emitted_once_per_loop():
    loop, observer = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=100)
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    assert len(observer.of_type(ToolSurfaceWarning)) == 1


@pytest.mark.asyncio
async def test_zero_disables_the_warning():
    loop, observer = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=0)
    await loop.process_input(_session(), "一")
    assert observer.of_type(ToolSurfaceWarning) == []


@pytest.mark.asyncio
async def test_the_origins_sum_to_the_tool_surface():
    loop, observer = _loop(
        _Tool("local_one", "z" * 400),
        _Tool("mcp_one", "z" * 400, origin="many"),
        _Tool("mcp_two", "z" * 400, origin="many"),
        tools_chars_warn=100,
    )
    await loop.process_input(_session(), "一")
    warning = observer.of_type(ToolSurfaceWarning)[0]
    total = sum(chars for _name, chars in warning.origins)
    assert set(dict(warning.origins)) == {"local", "many"}
    assert 0 <= abs(total - warning.tools_chars) <= 3 * 3  # 数组括号与逗号


@pytest.mark.asyncio
async def test_the_hint_names_the_most_expensive_origin():
    loop, observer = _loop(
        _Tool("local_one", "z" * 20),
        _Tool("mcp_one", "z" * 3000, origin="everything"),
        tools_chars_warn=100,
    )
    await loop.process_input(_session(), "一")
    warning = observer.of_type(ToolSurfaceWarning)[0]
    assert "everything" in warning.hint
    assert dict(warning.origins)["everything"] > dict(warning.origins)["local"]


@pytest.mark.asyncio
async def test_the_warning_never_changes_the_request():
    """R4 的纪律：观测只写不改。开不开告警，请求逐字节相同。"""
    quiet_loop, quiet = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=0)
    loud_loop, loud = _loop(_Tool("fat", "y" * 3000), tools_chars_warn=100)
    quiet_engine = quiet_loop.engine
    loud_engine = loud_loop.engine
    await quiet_loop.process_input(_session(), "一")
    await loud_loop.process_input(_session(), "一")
    assert [m.content for m in quiet_engine.requests[-1]] == [
        m.content for m in loud_engine.requests[-1]
    ]
```

（这个 `_Engine` 需要记录请求：把 `self.requests.append(list(messages))` 加进去，
与 `tests/test_loop_digest.py` 的 fake 同形。）

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_surface_warning.py -o addopts="" -q`
Expected: FAIL —— `ImportError: cannot import name 'ToolSurfaceWarning'`

- [ ] **Step 3: Implement**

```python
# ai_coding/core/events.py —— Degraded 之后
@dataclass(frozen=True)
class ToolSurfaceWarning:
    """The serialised tool schemas cost more than the configured ceiling (R7e-3).

    Advisory and write-only: it is emitted once per loop, never assembled into a
    request, and the request is byte-identical whether or not anyone listens.
    ``origins`` is ``((origin, chars), ...)`` biggest-first, so the user can see
    which MCP server to trim instead of one number nobody can act on.
    """

    tools_chars: int
    threshold: int
    origins: tuple[tuple[str, int], ...] = ()
    hint: str = ""

# __all__ 与 LoopEvent 联合各加一个 ToolSurfaceWarning
```

```python
# ai_coding/core/agent_loop.py —— 模块级 helper（放在 _TurnFrame 之前）
def _tools_by_origin(registry: ToolRegistry) -> dict[str, int]:
    """Serialised schema chars per origin, biggest first (R7e-3).

    Per *tool* rather than per group, so the numbers add up to the same schemas the
    request carries (the whole-array text differs only by its brackets and commas).
    """
    sizes: dict[str, int] = {}
    for tool in registry.all_tools():
        sizes[tool.origin] = sizes.get(tool.origin, 0) + len(short_json(tool_schema(tool)))
    return dict(sorted(sizes.items(), key=lambda item: (-item[1], item[0])))

# AgentLoop.__init__：紧跟 self._tools_chars 之后
        # R7e-3: the tool surface is static, so its breakdown is computed once and
        # the warning costs nothing per turn.
        self._tools_by_origin = _tools_by_origin(tools)
        self._tools_warned = False

# _begin_turn 内，reporter.begin_turn 之后
        self._warn_tools_chars_once()

# 新方法，放在 _begin_turn 之后
    def _warn_tools_chars_once(self) -> None:
        """Say once, per loop, that the tool surface is over its ceiling (R7e-3)."""
        if self._tools_warned:
            return
        threshold = int(getattr(self.settings, "tools_chars_warn", 0) or 0)
        if threshold <= 0 or self._tools_chars <= threshold:
            return
        self._tools_warned = True
        biggest = next(iter(self._tools_by_origin), "")
        self._emit(
            ToolSurfaceWarning(
                tools_chars=self._tools_chars,
                threshold=threshold,
                origins=tuple(self._tools_by_origin.items()),
                hint=(
                    "工具 schema 每一步都会重发；"
                    f"最贵的是 '{biggest}'。用 mcp.servers[].tools 只注册需要的工具，"
                    "或关掉用不到的 server。"
                ),
            )
        )
```

（`agent_loop.py` 的 import 区补 `short_json` 已在，新增
`from ai_coding.tools.schema import tool_schema, tool_schemas`。）

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_tool_surface_warning.py -o addopts="" -q`
Expected: 7 passed；随后全量 956 passed

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c5.txt <<'MSG'
R7e-3b: the tool surface says when it is expensive

The loop already knew the number; it now also knows the breakdown (per origin,
computed once at construction, because the tool list is static) and says so once
per loop when it crosses ai.tools_chars_warn.  The event names the most expensive
origin and the switch that fixes it, and a test asserts the request is
byte-identical with the warning on and off - an advisory must not change what we
send.
MSG
git add ai_coding/core/events.py ai_coding/core/agent_loop.py tests/test_tool_surface_warning.py
git commit -F /tmp/c5.txt
```

---

### Task 6: 把告警渲染成一行

**Files:**
- Modify: `ai_coding/ui/console.py`（`ConsoleObserver.on_event`）
- Test: `tests/test_tool_surface_warning.py`（追加一条渲染用例）

**Interfaces:**
- Consumes: `ToolSurfaceWarning`（Task 5）
- Produces: 无需新符号（渲染只在 console 内）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tool_surface_warning.py —— 追加
def test_the_console_renders_one_readable_line():
    from ai_coding.core.events import ToolSurfaceWarning
    from ai_coding.ui.console import ConsoleObserver, DialogueConsole

    printed: list[str] = []

    class _Console(DialogueConsole):
        def info(self, message: str) -> None:
            printed.append(message)

    ConsoleObserver(_Console()).on_event(
        ToolSurfaceWarning(
            tools_chars=13_301,
            threshold=12_000,
            origins=(("everything", 10_062), ("local", 4_280)),
            hint="ignore me",
        )
    )
    assert printed and printed[0].startswith("[tools]")
    assert "13301" in printed[0] and "12000" in printed[0]
    assert "everything=10062" in printed[0]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_tool_surface_warning.py -o addopts="" -q -k console`
Expected: FAIL —— `assert []`

- [ ] **Step 3: Implement**

```python
# ai_coding/ui/console.py —— on_event 内，TaskUpdated 分支之后
        elif isinstance(event, ToolSurfaceWarning):
            origins = " · ".join(
                f"{name}={chars}" for name, chars in event.origins[:3]
            )
            self._c.info(
                f"[tools] {event.tools_chars} 字符（阈值 {event.threshold}）"
                f"{' · ' + origins if origins else ''}"
            )

# 顶部 import 区
from ai_coding.core.events import ..., ToolSurfaceWarning
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: 957 passed

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c6.txt <<'MSG'
R7e-3c: the warning is a line, not a paragraph

One line in the REPL: the number, the threshold, and the two or three origins
that make it up.  The hint stays in the event for the trace and for any other
renderer; printing it every turn is exactly the noise the once-per-loop rule is
there to avoid.
MSG
git add ai_coding/ui/console.py tests/test_tool_surface_warning.py
git commit -F /tmp/c6.txt
```

---

### Task 7: 记忆的唯一写入口，多一个「模型要求写」

**Files:**
- Modify: `ai_coding/memory/service.py`（`write_note()`）
- Test: `tests/test_memory_write.py`（新，前半）

**Interfaces:**
- Consumes: `HarvestCandidate`（`memory/schema.py`）、`_reconcile`、`NoteKind`、`HarvestReport`
- Produces: `MemoryService.write_note(text, *, kind="fact", importance=3, supersedes="",
  turn=0, session_id="") -> HarvestReport`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_write.py
"""R7e-1: the model can write a note, through the same door as the harvest."""

from __future__ import annotations

from pathlib import Path

from ai_coding.config.models import MemoryConfig
from ai_coding.memory.note import NoteKind
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


def _service(tmp_path: Path, **overrides) -> MemoryService:
    return MemoryService(
        MemoryStore(tmp_path / ".memory"),
        config=MemoryConfig(**overrides),
    )


def test_write_note_adds_a_note(tmp_path: Path):
    service = _service(tmp_path)
    report = service.write_note("构建改用 uv，不用 poetry", kind="decision", importance=4)
    assert report.added
    note = service.store.get(report.added[0])
    assert note is not None
    assert note.kind is NoteKind.DECISION
    assert note.importance == 4


def test_write_note_records_evidence_model(tmp_path: Path):
    service = _service(tmp_path)
    report = service.write_note("用户偏好中文回答")
    assert service.store.get(report.added[0]).evidence == "model"


def test_write_note_dedupes_the_same_text(tmp_path: Path):
    service = _service(tmp_path)
    first = service.write_note("CI 缓存改走 uv")
    second = service.write_note("CI 缓存改走 uv")
    assert not second.added
    assert second.skipped == first.added  # 返回既有 id，不新增
    assert len(service.store.all()) == 1


def test_write_note_supersedes_an_explicit_id(tmp_path: Path):
    service = _service(tmp_path)
    old = service.write_note("依赖用 poetry")
    new = service.write_note("依赖改用 uv", supersedes=old.added[0])
    assert new.superseded
    assert service.store.get(old.added[0]).is_active is False


def test_write_note_ignores_an_unknown_target_id(tmp_path: Path):
    service = _service(tmp_path)
    report = service.write_note("随便一条", supersedes="does-not-exist")
    assert not report.added and not report.superseded
    assert report.skipped  # _reconcile 的 supersede 分支失败 → skipped


def test_write_note_is_a_noop_when_memory_is_disabled(tmp_path: Path):
    service = _service(tmp_path, enabled=False)
    report = service.write_note("不该写进去")
    assert not report.changed
    assert service.store.all() == []


def test_write_note_rejects_empty_text(tmp_path: Path):
    service = _service(tmp_path)
    assert not service.write_note("   ").changed


def test_write_note_evicts_above_the_cap(tmp_path: Path):
    service = _service(tmp_path, max_index_entries=2)
    for index in range(3):
        service.write_note(f"第 {index} 条互不相同的记忆内容")
    assert len(service.store.all()) == 2


def test_a_written_note_is_recallable(tmp_path: Path):
    import asyncio

    service = _service(tmp_path)
    service.write_note("这个仓库的验收命令是 make check", kind="fact")
    hits = asyncio.run(service.recall_notes("验收命令是什么", k=5))
    assert any("make check" in hit.note.text for hit in hits)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_memory_write.py -o addopts="" -q`
Expected: FAIL —— `AttributeError: 'MemoryService' object has no attribute 'write_note'`

- [ ] **Step 3: Implement**

```python
# ai_coding/memory/service.py —— recall_notes 之后、harvest 之前
    def write_note(
        self,
        text: str,
        *,
        kind: str = "fact",
        importance: int = 3,
        supersedes: str = "",
        turn: int = 0,
        session_id: str = "",
    ) -> HarvestReport:
        """Write one memory the model asked for, through the same door as harvest (R7e-1).

        ``_reconcile`` is this service's only write path: dedupe, similarity merge,
        explicit supersede, protection and eviction all live there, so a
        model-written note cannot bypass any of them.  It carries
        ``evidence="model"`` so ``/memory`` can tell it from a harvested one, which
        is the R6d conclusion applied to the tool path (docs/r7e-analysis.md 3.1).
        """
        body = str(text or "").strip()
        if not self.enabled or not body:
            return HarvestReport()
        target = str(supersedes or "").strip()
        candidate = HarvestCandidate(
            text=body,
            kind=NoteKind.parse(kind),
            importance=max(1, min(_MAX_IMPORTANCE, int(importance))),
            action="supersede" if target else "add",
            target_id=target or None,
        )
        return self._reconcile(
            [candidate],
            turn_index=int(turn or 0),
            session_id=str(session_id or ""),
            now=None,
            evidence="model",
        )
```

（`_MAX_IMPORTANCE` 已存在于 `memory/note.py`；若 service 未导入它，
用字面量 `5` 也行——保持一致即可。`HarvestCandidate`/`NoteKind`/`HarvestReport`
都已在 service.py 的 import 区。）

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_memory_write.py -o addopts="" -q`
Expected: 9 passed；随后全量 966 passed

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c7.txt <<'MSG'
R7e-1a: the only write path, now with a caller that is the model

write_note builds one HarvestCandidate and hands it to _reconcile, so dedupe,
similarity merge, explicit supersede, protection and eviction all apply without a
second write path existing.  evidence="model" is what makes a model-written note
distinguishable in /memory; the R6d decision was to record that, not to gate on it.
MSG
git add ai_coding/memory/service.py tests/test_memory_write.py
git commit -F /tmp/c7.txt
```

---

### Task 8: `memory_write` 工具（含把回合号传进工具上下文）

**Files:**
- Modify: `ai_coding/tools/base.py`（`ToolContext.turn`）
- Modify: `ai_coding/core/request_builder.py`（`tool_context(..., turn=0)`）
- Modify: `ai_coding/core/agent_loop.py`（`_run_tools(..., turn=...)`）
- Create: `ai_coding/tools/memory_write_tool.py`
- Test: `tests/test_memory_write.py`（后半）+ `tests/test_tool_context.py`（**只新增**）

**Interfaces:**
- Consumes: `MemoryService.write_note`（Task 7）
- Produces: `ToolContext.turn: int`；`tool_context(*, ..., turn: int = 0)`；
  `MemoryWriteTool(memory)`，`name == "memory_write"`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_write.py —— 追加
from ai_coding.domain.message import ToolResult  # noqa: E402
from ai_coding.tools.base import ToolContext  # noqa: E402
from ai_coding.tools.memory_write_tool import MemoryWriteTool  # noqa: E402


def _tool(tmp_path: Path) -> tuple[MemoryWriteTool, MemoryService]:
    service = _service(tmp_path)
    return MemoryWriteTool(service), service


@pytest.mark.asyncio
async def test_the_tool_requires_text(tmp_path: Path):
    tool, _service_obj = _tool(tmp_path)
    result = await tool.execute({}, ToolContext())
    assert result.success is False and result.error == "missing_text"


@pytest.mark.asyncio
async def test_the_tool_rejects_an_unknown_kind(tmp_path: Path):
    tool, _service_obj = _tool(tmp_path)
    result = await tool.execute({"text": "x", "kind": "gossip"}, ToolContext())
    assert result.success is False and result.error == "unknown_kind"


@pytest.mark.asyncio
async def test_the_tool_truncates_a_long_note_and_says_so(tmp_path: Path):
    tool, service = _tool(tmp_path)
    result = await tool.execute({"text": "x" * 900}, ToolContext())
    assert result.success is True
    assert result.data["truncated"] is True
    assert len(service.store.all()[0].text) <= 501


@pytest.mark.asyncio
async def test_the_tool_returns_the_note_id_and_the_action(tmp_path: Path):
    tool, _service_obj = _tool(tmp_path)
    result = await tool.execute({"text": "CI 缓存改走 uv", "kind": "decision"}, ToolContext())
    assert result.success is True
    assert result.data["action"] == "added"
    assert result.data["note_id"]
    assert result.data["kind"] == "decision"


@pytest.mark.asyncio
async def test_a_duplicate_write_is_not_an_error(tmp_path: Path):
    tool, _service_obj = _tool(tmp_path)
    first = await tool.execute({"text": "同一句话"}, ToolContext())
    second = await tool.execute({"text": "同一句话"}, ToolContext())
    assert second.success is True
    assert second.data["action"] == "skipped"
    assert second.data["note_id"] == first.data["note_id"]


@pytest.mark.asyncio
async def test_the_tool_survives_a_service_exception(tmp_path: Path):
    class _Broken:
        def write_note(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    result = await MemoryWriteTool(_Broken()).execute({"text": "x"}, ToolContext())
    assert result.success is False and result.error == "memory_error"


@pytest.mark.asyncio
async def test_the_tool_passes_the_turn_and_session_through(tmp_path: Path):
    seen: dict[str, object] = {}

    class _Recorder:
        def write_note(self, text, **kwargs):
            seen.update(kwargs, text=text)
            from ai_coding.memory.service import HarvestReport

            return HarvestReport(added=("note-1",))

    tool = MemoryWriteTool(_Recorder())
    await tool.execute({"text": "x"}, ToolContext(session_id="s1", turn=7))
    assert seen["session_id"] == "s1" and seen["turn"] == 7
```

```python
# tests/test_tool_context.py —— 追加
def test_a_tool_context_carries_the_turn_number_by_default_zero():
    from ai_coding.tools.base import ToolContext

    assert ToolContext().turn == 0
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_memory_write.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: ai_coding.tools.memory_write_tool`

- [ ] **Step 3: Implement**

```python
# ai_coding/tools/base.py —— ToolContext 内，call_id 之后
    # R7e-1: the session's turn counter, so a note the model writes can record
    # where it came from (source_turn means turn, R6d).
    turn: int = 0
```

```python
# ai_coding/core/request_builder.py —— tool_context 签名与返回
def tool_context(
    *,
    task: TaskState,
    workspace: str,
    session: SessionData,
    history: Sequence[ChatMessage],
    instructions: str | None,
    emit: Callable[[Any], None],
    turn: int = 0,
) -> ToolContext:
    return ToolContext(
        task=task,
        workspace=workspace,
        session_id=session.session_id,
        messages=tuple(history),
        instructions=instructions,
        emit=emit,
        turn=int(turn),
    )
```

```python
# ai_coding/core/agent_loop.py —— _run_tools 的签名与调用点
    async def _run_tools(
        self,
        step: StepResult,
        *,
        session: SessionData,
        task: TaskState,
        history: list[ChatMessage],
        instructions: str | None,
        deadline: Deadline | None,
        index: int,
        turn: int = 0,
    ) -> list[DispatchOutcome]:
        ...
            tool_context(
                task=task,
                workspace=self.workspace,
                session=session,
                history=history,
                instructions=instructions,
                emit=self._emit,
                turn=turn,
            ),

# 调用点（process_input 内）
                outcomes = await self._run_tools(
                    step,
                    session=session,
                    task=task,
                    history=assembled.history,
                    instructions=assembled.instructions,
                    deadline=frame.deadline,
                    index=index,
                    turn=frame.turn_number,
                )
```

```python
# ai_coding/tools/memory_write_tool.py（新）
"""``memory_write`` — the model writing its own durable note (R7e-1).

The harvest is retrospective: it runs after the turn, over the last N messages,
with an extractor.  Anything the model *knows* matters next session had no channel
of its own - it could only hope the extractor noticed.  This is that channel, and
it is deliberately narrow: one sentence, one of four kinds, and (optionally) an
explicit id to replace.

Writing is durable and cross-session, but it is not a side effect on the user's
repository, and the harness writes notes of its own accord anyway - so the gate
allows it (docs/r7e-analysis.md Q1).  ``evidence="model"`` is what makes these
notes visible as such in ``/memory``.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.memory.note import NoteKind
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["MemoryWriteTool"]

#: A memory is one line.  Anything longer is a file, or a todo item.
MAX_TEXT = 500
_KINDS = ", ".join(kind.value for kind in NoteKind)


class MemoryWriteTool(BaseTool):
    """Write one durable note (fact / decision / preference / open_thread)."""

    parallel_safe = True

    def __init__(self, memory: Any) -> None:
        self._memory = memory

    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return (
            "Write one durable note about this project, to be recalled in later "
            "sessions. Use it when something will still matter next time and is not "
            "already obvious from the code: a decision and why it was made, a user "
            "preference, a convention, or an open thread. Do NOT use it to record "
            "what you just did this turn - the harness keeps that itself. Keep it to "
            "one sentence; pass 'supersedes' with an existing note id to replace a "
            "note that is now wrong (search first with memory_search)."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "One sentence to remember (max 500 characters).",
                },
                "kind": {
                    "type": "string",
                    "description": f"One of: {_KINDS}. Defaults to fact.",
                },
                "importance": {
                    "type": "integer",
                    "description": "1-5, how much this should survive (default 3).",
                },
                "supersedes": {
                    "type": "string",
                    "description": "Optional id of an existing note this replaces.",
                },
            },
            "required": ["text"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        text = str(params.get("text", "") or "").strip()
        if not text:
            return ToolResult(
                success=False, output="error: 'text' is required", error="missing_text"
            )
        truncated = len(text) > MAX_TEXT
        if truncated:
            text = text[:MAX_TEXT] + "…"
        kind = str(params.get("kind", "") or NoteKind.FACT.value).strip().casefold()
        if kind not in {member.value for member in NoteKind}:
            return ToolResult(
                success=False,
                output=f"error: unknown kind '{kind}'; use one of: {_KINDS}",
                error="unknown_kind",
                data={"kind": kind},
            )
        importance = _as_importance(params.get("importance"))
        context = resolve_ctx(ctx)
        supersedes = str(params.get("supersedes", "") or "").strip()
        try:
            report = self._memory.write_note(
                text,
                kind=kind,
                importance=importance,
                supersedes=supersedes,
                turn=context.turn,
                session_id=context.session_id,
            )
        except Exception as exc:  # noqa: BLE001 - a memory write is never fatal
            return ToolResult(
                success=False,
                output=f"memory write failed: {exc}",
                error="memory_error",
                data={"text_chars": len(text)},
            )
        action, note_id = _classify(report)
        if not note_id:
            return ToolResult(
                success=False,
                output=(
                    "memory is disabled or the note could not be written; "
                    "carry on without it"
                ),
                error="memory_unavailable",
                data={"action": action},
            )
        spoken = (
            f"already remembered as [{kind}#{note_id[:8]}]"
            if action == "skipped"
            else f"remembered [{kind}#{note_id[:8]}] {text}"
        )
        if truncated:
            spoken += f" (note: truncated to {MAX_TEXT} characters)"
        return ToolResult(
            success=True,
            output=spoken,
            data={
                "action": action,
                "note_id": note_id,
                "kind": kind,
                "truncated": truncated,
                "text_chars": len(text),
            },
        )


def _as_importance(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 3
    return max(1, min(5, value))


def _classify(report: Any) -> tuple[str, str]:
    """Which of the four things happened, and to which note."""
    for action, ids in (
        ("added", getattr(report, "added", ())),
        ("superseded", getattr(report, "superseded", ())),
        ("updated", getattr(report, "updated", ())),
        ("skipped", getattr(report, "skipped", ())),
    ):
        if ids:
            return action, str(ids[0])
    return "none", ""
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_memory_write.py tests/test_tool_context.py -o addopts="" -q`
Expected: 15 passed（service 9 + 工具 7 - 复用）；随后全量 983 passed

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c8.txt <<'MSG'
R7e-1b: memory_write, and the turn number reaches the tool

The tool is deliberately narrow: one sentence (500 chars, truncated with a note),
four kinds, an optional id to replace, and a structured result that says which of
added/skipped/superseded/updated happened.  A duplicate write is not an error -
the dedupe working is the point - and a broken service degrades to
memory_error so a note can never fail a turn.

ToolContext gains the session's turn number, because source_turn means turn
(R6d) and the tool cannot see the session otherwise.
MSG
git add ai_coding/tools/base.py ai_coding/core/request_builder.py ai_coding/core/agent_loop.py ai_coding/tools/memory_write_tool.py tests/test_memory_write.py tests/test_tool_context.py
git commit -F /tmp/c8.txt
```

---

### Task 9: 门卫与注册（模型能看见它，子智能体不能）

**Files:**
- Modify: `ai_coding/security/permission_gate.py`（allow 表）
- Modify: `ai_coding/cli.py`（`_build_loop` 里与 `memory_search` 并排注册）
- Test: `tests/test_permission_gate_mcp.py`（追加）+ `tests/test_memory_write.py`（追加注册用例）

**Interfaces:**
- Consumes: `MemoryWriteTool`（Task 8）
- Produces: 门卫对 `memory_write` 返回 ALLOW；组合根注册它；子智能体栈没有它

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_write.py —— 追加
def test_the_gate_allows_memory_write():
    from ai_coding.security.permission_gate import PermissionDecision, PermissionGate

    assert PermissionGate().check("memory_write", {}) == PermissionDecision.ALLOW


def test_a_subagent_stack_has_no_memory_tools():
    from ai_coding.cli import _build_tool_registry
    from ai_coding.config.loader import ConfigManager

    manager = ConfigManager()
    manager.initialize("config.local.yaml")
    parent = _build_tool_registry(manager.app, ".", None)
    assert parent.get("memory_write") is not None  # 组合根有

    from ai_coding.tools.builder import build_tool_registry

    child = build_tool_registry(manager.app, ".", None, include_subagent=False)
    assert child.get("memory_write") is None  # Q3: 子智能体没有
    assert child.get("memory_search") is None
```

（第二条依赖仓库根有 `config.local.yaml`；没有就 skip：
`pytest.skip("needs config.local.yaml")`，照 `scripts/r6_cases.py` 的做法。）

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_memory_write.py -o addopts="" -q -k gate`
Expected: FAIL —— `assert Decision(warn, ...) == PermissionDecision.ALLOW`

- [ ] **Step 3: Implement**

```python
# ai_coding/security/permission_gate.py —— _allow_tools 集合内，memory_search 之后
            "memory_write",  # R7e-1: durable but not a side effect on the repo; the
                             # harness harvests notes without asking anyway (Q1)
```

```python
# ai_coding/cli.py —— _build_loop 里注册 memory_search 的那一段
    if memory is not None and getattr(registry, "get", None) is not None:
        if registry.get("memory_search") is None:
            from ai_coding.tools.memory_search_tool import MemorySearchTool

            registry.register(MemorySearchTool(memory))
        # R7e-1: the model may write a note of its own, next to the retrospective
        # harvest.  Parent-only: a sub-agent stack never gets memory tools at all.
        if registry.get("memory_write") is None:
            from ai_coding.tools.memory_write_tool import MemoryWriteTool

            registry.register(MemoryWriteTool(memory))
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: 985 passed（983 + 2），2 skipped

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c9.txt <<'MSG'
R7e-1c: the model can reach it, a sub-agent cannot

The gate allows memory_write for the same reason memory_search is allowed: the
harness already writes these notes by itself, so gating only the tool would be
inconsistent rather than safer - evidence=model plus /memory is the治理.  It is
registered in the composition root next to memory_search, and a sub-agent stack
still has no memory tools at all (Q3).
MSG
git add ai_coding/security/permission_gate.py ai_coding/cli.py tests/test_memory_write.py
git commit -F /tmp/c9.txt
```

---

### Task 10: Phase 0 spike —— 终端侧能不能做到（**先跑，不写产品代码**）

**Files:**
- Create: `/tmp/steering_spike.py`（**不进仓库**，一次性的）
- Modify: `docs/r7e-plan.md`（把结果填进「执行状态」）

**Interfaces:**
- Produces: 一个结论（4 项全过 → 继续 Task 13；任一项不过 → 停下报告，只交付 Task 11/12）

- [ ] **Step 1: Write the spike**

```python
# /tmp/steering_spike.py（一次性；不进仓库）
"""Four questions, one process.  Run inside a pty and drive it from a parent."""

import asyncio
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

mode = sys.argv[1] if len(sys.argv) > 1 else "callable"
state = {"asking": False}


def message() -> str:
    # Q1: can the displayed prompt change mid-turn (callable + invalidate)?
    return "问题> " if state["asking"] else "steer> "


async def reader(session: PromptSession, queue: asyncio.Queue) -> None:
    while True:
        try:
            with patch_stdout():  # Q2: does streamed output fight the prompt?
                line = await session.prompt_async(message if mode == "callable" else "steer> ")
        except EOFError:
            return
        queue.put_nowait(line)


async def main() -> None:
    session = PromptSession()
    queue: asyncio.Queue[str] = asyncio.Queue()
    task = asyncio.create_task(reader(session, queue))
    await asyncio.sleep(0.4)
    print("TURN-START", flush=True)
    for index in range(30):
        print(f"assistant chunk {index}", flush=True)
        await asyncio.sleep(0.1)
        if index == 10 and mode == "callable":
            state["asking"] = True  # Q1: swap the prompt mid-turn
            session.app.invalidate()
        while not queue.empty():
            print(f"LINE {queue.get_nowait()!r}", flush=True)
    print("TURN-END", flush=True)
    task.cancel()  # Q3: does the terminal survive the cancel?
    await asyncio.sleep(0.3)
    print("SURVIVED", flush=True)


asyncio.run(main())
```

- [ ] **Step 2: Drive it from a parent, twice**

```python
# 父进程：真 pty 起子进程，先等问题/转向提示，写一行，再检查输出
# 把 /tmp/spike_drive.py 写成与 tests/test_repl_ask_user_pty.py 同形的驱动器：
#   1) mode=callable：看 "steer> " 是否在 index==10 之后变成 "问题> "
#   2) mode=plain  ：看 30/30 块 assistant chunk 是否都渲染
#   3) 两种模式：写一行 → 看到 LINE '...'（送达）
#   4) 两种模式：看 "SURVIVED"（取消后终端没坏）
PY = """
import os, pty, select, subprocess, sys, time
...（同 tests/test_repl_ask_user_pty.py 的 read_until）
"""
```

- [ ] **Step 3: Record the four answers**

在 `docs/r7e-plan.md` 末尾的「执行状态」里如实写：

| 问题 | 结论 | 依据 |
|---|---|---|
| 可调用 message + `invalidate()` 能中途换提示词？ | 待填 | 子进程输出里 `问题>` 是否出现 |
| `patch_stdout()` 下流式输出不打架？ | 待填 | `assistant chunk` 计数是否 30/30 |
| 取消 reader 任务后终端恢复？ | 待填 | 是否打印 `SURVIVED` 且进程退出码 0 |
| 一行能送给队列（而不是被提示行吃掉）？ | 待填 | `LINE '...'` 是否出现 |

- [ ] **Step 4: Decide**

- 4 项全过 → 继续 Task 13，按计划写 `ui/reader.py`。
- 任一项不过 → **停下报告**（把失败的那一项、原始输出、我建议的替代方案写成一小段），
  只交付 Task 11/12（循环侧），把「交互式 steering」记回 `docs/r7e-backlog.md`。
  这是 Q9 的退路，不许绕过。

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c10.txt <<'MSG'
R7e-2: the spike result, written down before the reader is built

The terminal side of steering rests on four things I could not assert from
reading code: a callable message plus invalidate() changing the prompt mid-turn,
patch_stdout() keeping the streamed output and the prompt apart, cancelling the
reader leaving the terminal usable, and a typed line actually reaching the queue.
The answers (pass or fail, with the raw evidence) are recorded here, and a failure
stops the terminal work instead of shipping a reader nobody can trust.
MSG
git add docs/r7e-plan.md
git commit -F /tmp/c10.txt
```

---

### Task 11: 转向队列与账本计数

**Files:**
- Create: `ai_coding/core/steering.py`
- Modify: `ai_coding/core/turn_books.py`（`steering_messages` + `note_steering`）
- Test: `tests/test_steering_queue.py`（新）+ `tests/test_turn_books.py`（**只新增**）

**Interfaces:**
- Produces: `SteeringQueue(*, max_messages=5, max_chars=2000)`，方法 `put(text) -> bool`、
  `pending -> int`、`drain() -> list[str]`；
  `TurnBooks.steering_messages: int`、`TurnBooks.note_steering(count=1) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_steering_queue.py
"""R7e-2: where a line typed while the turn is running waits."""

from __future__ import annotations

from ai_coding.core.steering import SteeringQueue


def test_put_then_drain_returns_the_lines_in_order():
    queue = SteeringQueue()
    queue.put("第一句")
    queue.put("第二句")
    assert queue.drain() == ["第一句", "第二句"]


def test_drain_is_idempotent():
    queue = SteeringQueue()
    queue.put("一次")
    assert queue.drain() == ["一次"]
    assert queue.drain() == []


def test_pending_counts_without_draining():
    queue = SteeringQueue()
    queue.put("a")
    queue.put("b")
    assert queue.pending == 2
    assert queue.pending == 2  # 看一眼不该消费


def test_blank_lines_are_dropped():
    queue = SteeringQueue()
    queue.put("   ")
    queue.put("")
    assert queue.pending == 0


def test_a_long_line_is_truncated_with_a_marker():
    queue = SteeringQueue(max_chars=10)
    queue.put("x" * 50)
    (line,) = queue.drain()
    assert line.endswith("…")
    assert len(line) == 11


def test_at_most_max_messages_come_out_and_the_rest_stay_queued():
    queue = SteeringQueue(max_messages=2)
    for index in range(5):
        queue.put(f"第 {index} 句")
    assert queue.drain() == ["第 0 句", "第 1 句"]
    assert queue.pending == 3  # 不许悄悄丢掉用户的输入


def test_whitespace_is_trimmed_but_inner_text_is_kept():
    queue = SteeringQueue()
    queue.put("  改用 uv， 不要 pip  ")
    assert queue.drain() == ["改用 uv， 不要 pip"]


def test_a_slash_line_is_not_steering():
    """Q6：命令不是指令。`put` 拒绝它，调用方（reader）留到回合结束再交回 REPL。"""
    queue = SteeringQueue()
    assert queue.put("/status") is False
    assert queue.pending == 0
```

```python
# tests/test_turn_books.py —— 追加
def test_note_steering_counts_and_resets_the_failure_streak():
    books = TurnBooks(failed_streak=2, attempted_calls=3, successful_calls=0)
    books.note_steering(2)
    assert books.steering_messages == 2
    assert books.failed_streak == 0  # Q5: 新指令 = 重新开始
    assert books.attempted_calls == 3  # 但工具确实没成功过，计数不动
    assert books.successful_calls == 0


def test_note_steering_defaults_to_one():
    books = TurnBooks()
    books.note_steering()
    assert books.steering_messages == 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_steering_queue.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: ai_coding.core.steering`

- [ ] **Step 3: Implement**

```python
# ai_coding/core/steering.py
"""Where a line typed while a turn is running waits (R7e-2).

Measured before it was designed (docs/r7e-analysis.md 1.1): a background reader can
hand a line over mid-turn, but only as an async task - a thread reader delivers the
line and then refuses to stop.  This queue is the receiving end of that reader, and
it is deliberately dumb: a bounded FIFO with no idea who filled it, so a `turn.start`
hook or a scripted source can use it too.

Two rules matter and both have tests: draining is capped per call (the rest stays,
because silently dropping what the user typed is the one unforgivable bug here), and
blank lines are not instructions.
"""

from __future__ import annotations

from collections import deque

__all__ = ["SteeringQueue"]

_TRUNCATION = "…"


class SteeringQueue:
    """A bounded FIFO of lines the user typed while the turn was running."""

    def __init__(self, *, max_messages: int = 5, max_chars: int = 2_000) -> None:
        self._lines: deque[str] = deque()
        self._max_messages = max(1, int(max_messages))
        self._max_chars = max(1, int(max_chars))

    def put(self, text: str) -> bool:
        """Queue one line; return False when it is a command, not an instruction.

        Safe from any thread: ``deque.append`` is atomic.  A line starting with "/"
        belongs to the REPL (Claude Code holds commands until the turn ends, docs
        §2.1b), so the queue refuses it and the caller keeps it.
        """
        body = str(text or "").strip()
        if not body or body.startswith("/"):
            return False
        self._lines.append(body)
        return True

    @property
    def pending(self) -> int:
        """How many lines are waiting (looking does not consume them)."""
        return len(self._lines)

    def drain(self) -> list[str]:
        """Take up to ``max_messages`` lines, oldest first, each trimmed to ``max_chars``."""
        taken: list[str] = []
        while self._lines and len(taken) < self._max_messages:
            body = self._lines.popleft()
            if len(body) > self._max_chars:
                body = body[: self._max_chars] + _TRUNCATION
            taken.append(body)
        return taken
```

```python
# ai_coding/core/turn_books.py —— 字段区，memory_announced 之后
    #: Lines the user typed while this turn was running and the loop absorbed.
    steering_messages: int = 0

# 方法区，note_nudge 之后
    def note_steering(self, count: int = 1) -> None:
        """The user steered the turn: new instructions, so the streak restarts (Q5).

        The call counters are deliberately untouched: nothing succeeded, and the
        stall ladder is about tool progress, not about how many times we asked.
        """
        self.steering_messages += max(0, int(count))
        self.failed_streak = 0
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_steering_queue.py tests/test_turn_books.py -o addopts="" -q`
Expected: 10 passed（8 + 2）；随后全量 995 passed

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c11.txt <<'MSG'
R7e-2a: a bounded queue, and what steering does to the books

The queue is a plain FIFO with two rules that have tests: draining is capped (the
rest stays queued - dropping what the user typed is the failure mode this whole
slice exists to avoid), blank lines are not instructions, and a line starting with
"/" is a command, not an instruction - it is refused here and the caller keeps it
for the REPL, which is what Claude Code documents for commands.  The books count
steering messages and restart the failure streak, because a fresh instruction is a
fresh attempt - while leaving attempted/successful alone, because those describe
what the tools actually did.
MSG
git add ai_coding/core/steering.py ai_coding/core/turn_books.py tests/test_steering_queue.py tests/test_turn_books.py
git commit -F /tmp/c11.txt
```

---

### Task 12: 循环在迭代边界把队列抽干

**Files:**
- Modify: `ai_coding/core/events.py`（`InputSteered`）
- Modify: `ai_coding/core/agent_loop.py`（`steering=` 参数 + `_take_steering`）
- Test: `tests/test_loop_steering.py`（新）

**Interfaces:**
- Consumes: `SteeringQueue`（Task 11）、`TurnBooks.note_steering`（Task 11）
- Produces: `AgentLoop(..., steering: Any | None = None)`；
  `InputSteered(text_chars: int, iteration: int, total: int)`；
  `AgentLoop._take_steering(session, books, index) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_loop_steering.py
"""R7e-2: a line typed mid-turn becomes an instruction the model sees next step."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.events import InputSteered, RecordingObserver
from ai_coding.core.steering import SteeringQueue
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.security.approval import AutoApproveCallback
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Ask for one tool on the first step, then speak."""

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            return StepResult(
                text=None,
                tool_calls=[ToolCallRef(id="c1", name="poke", arguments="{}")],
                finish_reason="tool_calls",
            )
        return StepResult(text="好", tool_calls=[], finish_reason="stop")


class _Poke(BaseTool):
    parallel_safe = True

    @property
    def name(self) -> str:
        return "poke"

    @property
    def description(self) -> str:
        return "poke"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="poked")


class _Sessions:
    def save(self, session: SessionData) -> None: ...


def _loop(
    queue: SteeringQueue | None,
    engine: _Engine,
    *,
    observer: Any | None = None,
    **overrides,
):
    registry = ToolRegistry()
    registry.register(_Poke())
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(max_tool_iterations=3, stop_policy="natural", **overrides),
        observer=observer,  # None 时 AgentLoop 自己用 NullObserver
        steering=queue,
    )


def _loop_with_recorder(queue: SteeringQueue | None, engine: _Engine):
    observer = RecordingObserver()
    return _loop(queue, engine, observer=observer), observer


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_a_steering_line_becomes_a_persisted_user_message():
    queue = SteeringQueue()
    queue.put("改用 uv，不要 pip")
    engine = _Engine()
    loop = _loop(queue, engine)
    session = _session()
    await loop.process_input(session, "改构建")
    contents = [m.content or "" for m in session.messages if m.role == "user"]
    assert any("[steering] 改用 uv，不要 pip" in c for c in contents)


@pytest.mark.asyncio
async def test_the_model_sees_the_steering_line_in_the_same_iteration():
    queue = SteeringQueue()
    queue.put("改用 uv")
    engine = _Engine()
    await _loop(queue, engine).process_input(_session(), "改构建")
    second_request = [m.content or "" for m in engine.requests[1]]
    assert any("[steering] 改用 uv" in c for c in second_request)


@pytest.mark.asyncio
async def test_steering_is_reported_once_per_batch():
    queue = SteeringQueue()
    queue.put("一")
    queue.put("二")
    loop, observer = _loop_with_recorder(queue)
    await loop.process_input(_session(), "改构建")
    events = observer.of_type(InputSteered)
    assert len(events) == 1
    assert events[0].total == 2
    assert events[0].iteration == 2


@pytest.mark.asyncio
async def test_steering_does_not_count_as_an_iteration_or_a_nudge():
    queue = SteeringQueue()
    queue.put("早一点说")
    loop, _observer = _loop_with_recorder(queue)
    await loop.process_input(_session(), "改构建")
    assert loop.last_outcome.iterations == 2  # 只有两次真正的迭代


@pytest.mark.asyncio
async def test_no_source_means_no_behaviour_change():
    engine = _Engine()
    with_source = _Engine()
    queue = SteeringQueue()  # 空队列
    await _loop(None, engine).process_input(_session(), "改构建")
    await _loop(queue, with_source).process_input(_session(), "改构建")
    assert [m.content for m in engine.requests[1]] == [
        m.content for m in with_source.requests[1]
    ]


@pytest.mark.asyncio
async def test_leftovers_stay_queued_for_the_next_turn():
    queue = SteeringQueue(max_messages=1)
    queue.put("第一条")
    queue.put("第二条")
    loop, _observer = _loop_with_recorder(queue)
    await loop.process_input(_session(), "改构建")
    assert queue.pending == 1
```

（文件里只有这两个工厂：`_loop` 不带 observer，`_loop_with_recorder` 返回 `(loop, observer)`。）

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_loop_steering.py -o addopts="" -q`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'steering'`

- [ ] **Step 3: Implement**

```python
# ai_coding/core/events.py —— ToolSurfaceWarning 之后
@dataclass(frozen=True)
class InputSteered:
    """The user typed while the turn was running, and it became an instruction (R7e-2).

    The line is persisted as a ``[steering] …`` user message (Q4) for the same
    reason the task gate is: it changed what the model did, so replaying the
    transcript without it would not reproduce the turn.
    """

    text_chars: int
    iteration: int = 0
    total: int = 1

# __all__ 与 LoopEvent 联合各加一个 InputSteered
```

```python
# ai_coding/core/agent_loop.py —— __init__ 的只读参数区，hooks 之后
        steering: Any | None = None,
# 赋值区，self.hooks 附近
        # R7e-2: where lines typed mid-turn wait.  None = today's behaviour, and
        # the loop then never looks at a queue at all.
        self.steering = steering

# process_input 内：books.note_iteration(index) 之后、_emit(IterationStarted) 之前
                self._take_steering(session, books, index)

# 新方法，放在 _begin_turn 之后
    def _take_steering(self, session: SessionData, books: TurnBooks, index: int) -> None:
        """Absorb whatever the user typed, right before this request is assembled.

        Delivery point: the iteration boundary, after the previous batch finished -
        which is what Claude Code documents ("as soon as those tool calls finish,
        within the same turn", docs/r7e-analysis.md 2.1b).  The lines are persisted
        (Q4) and they do not consume an iteration or a nudge.
        """
        if self.steering is None:
            return
        lines = self.steering.drain()
        if not lines:
            return
        for line in lines:
            session.add_message(user_message(f"[steering] {line}", now_utc()))
        books.note_steering(len(lines))
        self.closer.checkpoint(session, index)
        self._emit(
            InputSteered(
                text_chars=sum(len(line) for line in lines),
                iteration=index,
                total=books.steering_messages,
            )
        )
```

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_loop_steering.py -o addopts="" -q`
Expected: 6 passed；随后全量 1001 passed

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c12.txt <<'MSG'
R7e-2b: a line typed mid-turn becomes an instruction

The loop drains a queue at the iteration boundary - after the previous batch, before
this request is assembled - which is both the only place it can work and what Claude
Code documents for queued messages.  Each line is persisted as a [steering] user
message: it changed the model's behaviour, so the transcript has to carry it.

The loop never consumes an iteration or a nudge for steering, and passing no source
leaves the request byte-identical - the switch is the object's presence.
MSG
git add ai_coding/core/events.py ai_coding/core/agent_loop.py tests/test_loop_steering.py
git commit -F /tmp/c12.txt
```

---

### Task 13: 唯一读取器与 REPL 接线

**Files:**
- Create: `ai_coding/ui/reader.py`
- Modify: `ai_coding/ui/repl.py`（`run_repl(..., reader=None)`）
- Modify: `ai_coding/cli.py`（按 `ui.steering` 建 reader 并传进去）
- Test: `tests/test_ui_reader.py`（新）+ `tests/test_repl.py`（**只新增**）

**Interfaces:**
- Consumes: `SteeringQueue`（Task 11）、`AIConfig.steering_max_*`、`UIConfig.steering`（Task 1）
- Produces: `InteractiveReader(*, prompt=None, invalidate=None, steering=None, enabled=True)`，
  方法 `start()` / `aclose()` / `input() -> str` / `set_busy(bool)` /
  `ask(question, options=()) -> UserAnswer`；
  `InteractiveReader.for_terminal(*, steering=None, enabled=True)`；
  `run_repl(loop, sessions, console, *, user_prompt=None, reader=None)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ui_reader.py
"""R7e-2: one reader for one terminal, and who gets each line."""

from __future__ import annotations

import asyncio

import pytest

from ai_coding.core.steering import SteeringQueue
from ai_coding.ui.reader import InteractiveReader

pytestmark = pytest.mark.asyncio


def _reader(lines: list[str], *, steering: SteeringQueue | None = None):
    scripted = list(lines)
    prompts: list[str] = []

    async def prompt(message):
        prompts.append(message() if callable(message) else str(message))
        if not scripted:
            raise EOFError
        await asyncio.sleep(0)  # 让调度器有机会切到消费者
        return scripted.pop(0)

    reader = InteractiveReader(prompt=prompt, invalidate=lambda: None, steering=steering)
    return reader, prompts


async def test_a_line_goes_to_the_repl_when_nothing_is_running():
    reader, _prompts = _reader(["你好"])
    reader.start()
    try:
        assert await reader.input() == "你好"
    finally:
        await reader.aclose()


async def test_a_line_becomes_steering_while_the_turn_is_running():
    queue = SteeringQueue()
    reader, _prompts = _reader(["改用 uv"], steering=queue)
    reader.start()
    reader.set_busy(True)
    try:
        for _ in range(20):
            if queue.pending:
                break
            await asyncio.sleep(0.01)
        assert queue.drain() == ["改用 uv"]
    finally:
        await reader.aclose()


async def test_a_slash_line_is_held_for_the_repl_even_while_busy():
    queue = SteeringQueue()
    reader, _prompts = _reader(["/status"], steering=queue)
    reader.start()
    reader.set_busy(True)
    try:
        assert await asyncio.wait_for(reader.input(), timeout=1) == "/status"
        assert queue.pending == 0
    finally:
        await reader.aclose()


async def test_a_pending_question_takes_the_next_line():
    reader, prompts = _reader(["改 A", "下一个问题"])
    reader.start()
    try:
        answer = await asyncio.wait_for(reader.ask("改 A 还是 B？", ["A", "B"]), timeout=1)
        assert answer.answered is True and answer.text == "改 A"
        assert any("改 A 还是 B？" in text for text in prompts)
    finally:
        await reader.aclose()


async def test_eof_ends_the_input_stream():
    reader, _prompts = _reader([])
    reader.start()
    try:
        with pytest.raises(EOFError):
            await asyncio.wait_for(reader.input(), timeout=1)
    finally:
        await reader.aclose()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_ui_reader.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: ai_coding.ui.reader`

- [ ] **Step 3: Implement**

```python
# ai_coding/ui/reader.py
"""One reader for one terminal (R7e-2).

R7c established the rule (a second reader on the same tty fights the first); R7e
needs the opposite property from the same terminal - a reader that stays alive
*while a turn is running*, so a line typed mid-turn can be delivered.  Measured
before it was built (docs/r7e-analysis.md 1.1): that has to be an async task
(``prompt_async``), because a thread reader delivers the line and then refuses to
be stopped.

So this class owns the session and arbitrates: a pending question takes the next
line, a line typed while a turn is running goes to the steering queue, a command
(``/…``) is held for the REPL, and everything else is the REPL's next input.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ai_coding.core.steering import SteeringQueue
from ai_coding.security.user_prompt import UserAnswer

__all__ = ["InteractiveReader"]

_INPUT_PROMPT = "you> "
_STEER_PROMPT = "steer> "
_QUESTION_PROMPT = "问题> "


class InteractiveReader:
    """The single owner of the terminal's input, and the arbiter of each line."""

    def __init__(
        self,
        *,
        prompt: Callable[[Any], Awaitable[str]] | None = None,
        invalidate: Callable[[], None] | None = None,
        steering: SteeringQueue | None = None,
        enabled: bool = True,
    ) -> None:
        self._prompt = prompt
        self._invalidate = invalidate or (lambda: None)
        self._steering = steering
        self._enabled = bool(enabled)
        self._inputs: deque[str] = deque()
        self._waiting: asyncio.Future[str] | None = None
        self._question: str | None = None
        self._answer: asyncio.Future[str] | None = None
        self._busy = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    # ---- construction ------------------------------------------------------

    @classmethod
    def for_terminal(
        cls, *, steering: SteeringQueue | None = None, enabled: bool = True
    ) -> InteractiveReader:
        """Build the real thing (lazily imports prompt_toolkit)."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout

        session = PromptSession()

        async def prompt(message: Any) -> str:
            with patch_stdout():
                return await session.prompt_async(message)

        return cls(
            prompt=prompt,
            invalidate=session.app.invalidate,
            steering=steering,
            enabled=enabled,
        )

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is None and self._prompt is not None:
            self._task = asyncio.create_task(self._read_loop(), name="ai-coding-reader")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        self._closed = True
        if task is not None and not task.done():
            task.cancel()

    # ---- what the REPL asks for -------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """A turn is (not) running: pick where an unclaimed line goes."""
        self._busy = bool(busy)
        self._invalidate()

    async def input(self) -> str:
        """The next line meant for the REPL; raises ``EOFError`` when input ends."""
        if self._inputs:
            return self._inputs.popleft()
        if self._closed:
            raise EOFError
        self._waiting = asyncio.get_running_loop().create_future()
        try:
            return await self._waiting
        finally:
            self._waiting = None

    # ---- what the model asks for (R7c's channel, through this reader) ------

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        """Put ``question`` on the prompt line and take the next line as its answer."""
        if self._closed:
            return UserAnswer(answered=False, reason="eof")
        self._question = _render_question(question, options)
        self._answer = asyncio.get_running_loop().create_future()
        self._invalidate()
        try:
            text = (await self._answer).strip()
        finally:
            self._question = None
            self._answer = None
            self._invalidate()
        if not text:
            return UserAnswer(answered=False, reason="empty")
        return UserAnswer(answered=True, text=text)

    # ---- the one loop ------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._prompt is not None
        while not self._closed:
            try:
                line = await self._prompt(self._message)
            except (EOFError, OSError):
                self._close_stream()
                return
            except asyncio.CancelledError:
                raise
            self._dispatch(line)

    def _message(self) -> str:
        """The prompt text (a callable, so it can change mid-turn)."""
        if self._question:
            return self._question
        if self._busy and self._enabled:
            return _STEER_PROMPT
        return _INPUT_PROMPT

    def _dispatch(self, line: str) -> None:
        """Give one line to exactly one consumer: question, steering, or the REPL."""
        body = str(line or "")
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(body)
            return
        if self._busy and self._enabled and self._steering is not None:
            if self._steering.put(body):
                return
        self._inputs.append(body)
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_result(self._inputs.popleft())

    def _close_stream(self) -> None:
        self._closed = True
        if self._answer is not None and not self._answer.done():
            self._answer.set_exception(EOFError())
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_exception(EOFError())


def _render_question(question: str, options: Sequence[str]) -> str:
    if not options:
        return f"{question} "
    rendered = " / ".join(str(option) for option in options)
    return f"{question}（{rendered}） "
```

```python
# ai_coding/ui/repl.py —— run_repl 换成一个循环 + 可替换的行来源
async def run_repl(
    loop: Any,
    sessions: Any,
    console: DialogueConsole,
    *,
    user_prompt: Any | None = None,
    reader: Any | None = None,
) -> None:
    """Interactive loop. Lazily imports prompt_toolkit.

    R7c: `user_prompt` (the `ask_user` channel) gets the REPL's own reader, because
    two readers on one terminal fight over stdin.  R7e-2: when a `reader` is given
    it is that reader - it stays alive during a turn, so a line typed mid-turn can
    be delivered instead of being eaten by the next prompt.
    """
    session = sessions.create()
    if reader is not None:
        reader.start()
        if user_prompt is not None and hasattr(user_prompt, "set_interactive"):
            user_prompt.set_interactive(reader)
        read_line = reader.input
    else:
        from prompt_toolkit import PromptSession

        prompt: Any = PromptSession()
        if user_prompt is not None and hasattr(user_prompt, "set_reader"):
            user_prompt.set_reader(prompt.prompt)
        read_line = lambda: asyncio.to_thread(prompt.prompt, "you> ")  # noqa: E731

    console.rule("ai-coding REPL — /help for commands")
    try:
        while True:
            try:
                text = await read_line()
            except (KeyboardInterrupt, EOFError):
                break
            stripped = text.strip()
            if not stripped:
                continue
            if reader is not None:
                reader.set_busy(True)
            try:
                action, session = await handle_input(
                    loop, stripped, session, sessions, console
                )
            finally:
                if reader is not None:
                    reader.set_busy(False)
            if action is REPLAction.QUIT:
                break
    finally:
        if reader is not None:
            await reader.aclose()
    console.info("bye")
```

```python
# ai_coding/cli.py —— repl() 内，建 registry 之后
        from ai_coding.core.steering import SteeringQueue
        from ai_coding.ui.reader import InteractiveReader

        steering = SteeringQueue(
            max_messages=int(getattr(app_cfg.ai, "steering_max_messages", 5)),
            max_chars=int(getattr(app_cfg.ai, "steering_max_chars", 2000)),
        )
        reader = InteractiveReader.for_terminal(
            steering=steering, enabled=bool(getattr(app_cfg.ui, "steering", True))
        )
        # ... 建 loop 时把 steering 传进去（见下）

        await run_repl(
            loop, sessions, console, user_prompt=user_prompt, reader=reader
        )

# _build_loop 加一个只读参数并转交给 AgentLoop
    steering: Any | None = None,
        ...
        steering=steering,
```

（`_build_loop` 的 `AgentLoop(...)` 调用里加 `steering=steering`；
`repl()` 里 `_build_loop(..., steering=steering)`。`ask` 路径不建 reader，行为不变。）

- [ ] **Step 4: Run it, the gated pty test, and the whole suite**

Run: `.venv/bin/python -m pytest tests/test_ui_reader.py tests/test_repl.py -o addopts="" -q`
Expected: 新增 5 passed；既有 `test_repl.py` 零改动通过

Run: `AI_CODING_PTY=1 .venv/bin/python -m pytest tests/test_repl_ask_user_pty.py -o addopts="" -q`
Expected: 1 passed（R7c 的通道换成 reader 之后仍然通）

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c13.txt <<'MSG'
R7e-2c: one reader arbitrates the terminal

The reader owns the prompt session and decides where each line goes: a pending
question takes it, a line typed during a turn becomes steering, a "/" line is held
for the REPL, and everything else is the REPL's next input.  run_repl keeps one
loop and swaps only the line source, so the reader=None path is byte-for-byte the
old behaviour and the pty test from R7c still passes.

It has to be an async task: measured earlier, a thread reader delivers the line and
then refuses to stop.
MSG
git add ai_coding/ui/reader.py ai_coding/ui/repl.py ai_coding/cli.py tests/test_ui_reader.py
git commit -F /tmp/c13.txt
```

---

### Task 14: 提问通道改走 reader（保留旧路径）

**Files:**
- Modify: `ai_coding/security/user_prompt.py`（`set_interactive` + `ask` 分支）
- Test: `tests/test_user_prompt.py`（**只新增**）

**Interfaces:**
- Consumes: `InteractiveReader.ask`（Task 13）
- Produces: `CLIUserPromptCallback.set_interactive(reader) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_user_prompt.py —— 追加
@pytest.mark.asyncio
async def test_a_reader_backed_callback_asks_through_the_reader():
    from ai_coding.security.user_prompt import UserAnswer

    class _Reader:
        def __init__(self) -> None:
            self.asked: list[tuple[str, tuple[str, ...]]] = []

        async def ask(self, question, options=()):
            self.asked.append((question, tuple(options)))
            return UserAnswer(answered=True, text="走 reader")

    reader = _Reader()
    callback = CLIUserPromptCallback()
    callback.set_interactive(reader)
    answer = await callback.ask("改 A 还是 B？", ["A", "B"])
    assert answer.text == "走 reader"
    assert reader.asked == [("改 A 还是 B？", ("A", "B"))]


@pytest.mark.asyncio
async def test_a_broken_reader_degrades_to_no_answer():
    class _Reader:
        async def ask(self, question, options=()):
            raise RuntimeError("terminal gone")

    callback = CLIUserPromptCallback()
    callback.set_interactive(_Reader())
    answer = await callback.ask("在吗？")
    assert answer.answered is False and answer.reason == "no_user"


@pytest.mark.asyncio
async def test_without_a_reader_the_old_input_path_still_works():
    callback = CLIUserPromptCallback(input_fn=lambda _prompt: "老路径")
    answer = await callback.ask("在吗？")
    assert answer.text == "老路径"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/bin/python -m pytest tests/test_user_prompt.py -o addopts="" -q`
Expected: FAIL —— `AttributeError: 'CLIUserPromptCallback' object has no attribute 'set_interactive'`

- [ ] **Step 3: Implement**

```python
# ai_coding/security/user_prompt.py —— __init__ 与 ask 的开头
    def __init__(self, input_fn: Callable[[str], str] | None = None) -> None:
        self._read: Callable[[str], str] = input_fn or (lambda prompt: input(prompt))
        self._interactive: Any | None = None

    def set_interactive(self, reader: Any) -> None:
        """Answer questions through the REPL's reader (R7e-2).

        With a reader present this callback no longer touches stdin itself, which
        is what keeps "one terminal, one reader" true while a question is on screen.
        """
        self._interactive = reader

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        if self._interactive is not None:
            try:
                return await self._interactive.ask(question, options)
            except Exception:  # noqa: BLE001 - the channel must never break a turn
                return UserAnswer(answered=False, reason="no_user")
        prompt = _format_prompt(question, options)
        ...（其余不变）
```

（`Any` 若未导入，从 `collections.abc`/`typing` 补；`UserAnswer`/`Sequence` 已在文件里。）

- [ ] **Step 4: Run it and the whole suite**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: 1009 passed（1001 + 3 + 5 视前面步数），2 skipped

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c14.txt <<'MSG'
R7e-2d: a question goes through the reader that owns the terminal

With a reader present the callback no longer reads stdin itself, so a question and a
steering line cannot race for the same keystroke - the reader decides which one the
next line is for.  The old paths (sync input_fn, set_reader) stay exactly as they
were, because `ask`, the test child and every existing test still use them.
MSG
git add ai_coding/security/user_prompt.py tests/test_user_prompt.py
git commit -F /tmp/c14.txt
```

---

### Task 15: 验收脚本与六份文档

**Files:**
- Create: `scripts/r7e_cases.py`
- Modify: `docs/features.md` / `data-contracts.md` / `architecture.md` / `how-to-run.md` /
  `integration_testing.md` / `r7-backlog.md`
- Modify: `docs/r7e-plan.md`（执行状态，含 Task 10 的 spike 结论）

**Interfaces:**
- Consumes: 前面全部任务
- Produces: `scripts/r7e_cases.py`（离线 + `--real`）

- [ ] **Step 1: Write the offline cases**

```python
# scripts/r7e_cases.py（骨架照 scripts/r7_cases.py：SEP / CaseFn / --only / --real / SUMMARY）
OFFLINE = {
    "memory_write_adds_and_dedupes": ...,      # service + 工具各一遍
    "memory_write_is_not_visible_to_a_subagent": ...,
    "tools_chars_warning_fires_once": ...,
    "tools_chars_warning_keeps_the_request_identical": ...,
    "steering_line_becomes_an_instruction": ...,
    "steering_leaves_the_rest_queued": ...,
    "steering_resets_the_streak_not_the_counts": ...,
    "subagent_task_only_has_a_clean_window": ...,
    "subagent_full_still_inherits_the_parent": ...,
}
LIVE = {
    "pty_steering": ...,       # 真 pty：回合中途写一行 → 历史里出现 [steering]
    "pty_ask_user_through_reader": ...,   # 真 pty：问题与转向共用一条提示行
    "turn_still_works": ...,   # 复用 r7_cases.py 的那一条（真模型一个回合）
}
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python scripts/r7e_cases.py`
Expected: `SUMMARY 9/9 passed`

Run: `.venv/bin/python scripts/r7e_cases.py --real --config config.local.yaml`
Expected: 真实三条**只报告**（pty 两条 + 真模型一条）

- [ ] **Step 3: Sync the six docs**

| 文档 | 改什么 |
|---|---|
| `features.md` | 工具清单加 `memory_write`；「记忆」一节补「模型主动写」；「子智能体」一节补两种模式；MCP 三条代价末尾补「超过阈值会有一次 `[tools]` 提示」 |
| `data-contracts.md` | 5 个新键、`SubagentResult.context_mode`、`BaseTool.origin`、`ToolContext.turn`、两个新事件 |
| `architecture.md` | 包结构补 `core/steering.py`、`ui/reader.py`；「一次回合的数据流」补 steering 抽干点与工具面告警 |
| `how-to-run.md` | 「R7e 新增配置与行为」小节：steering 怎么用（回合中直接打字）、`task_only` 的取舍、阈值怎么调 |
| `integration_testing.md` | `scripts/r7e_cases.py` 怎么跑；两条门控终端用例 |
| `r7-backlog.md` | 候选池划掉这四条并指向 `docs/r7e-analysis.md` |

- [ ] **Step 4: Run everything**

Run:
`.venv/bin/python -m pytest tests/ -o addopts="" -q && .venv/bin/python -m ruff check . &&
.venv/bin/python -m mypy ai_coding && .venv/bin/python scripts/r7e_cases.py &&
.venv/bin/python scripts/r7_cases.py && .venv/bin/python scripts/r6_cases.py`
Expected: 全绿；`r7e_cases` 9/9；`r7_cases` 11/11；`r6_cases` 9/9

- [ ] **Step 5: Commit**

```bash
cat > /tmp/c15.txt <<'MSG'
R7e: the acceptance script, and the docs the round changed

Nine offline cases (one per claim) plus three live ones: a real pty where a line
typed mid-turn lands in the transcript, the same pty proving a question and a
steering line share one prompt, and one real model turn to show the composition
root still works end to end.  Six docs updated for what actually changed.
MSG
git add scripts/r7e_cases.py docs
git commit -F /tmp/c15.txt
```

---

## Self-Review（写完计划后自查，已逐条核过）

**1. Spec coverage**（对照 `docs/r7e-analysis.md`）：

| 规格 | 任务 |
|---|---|
| §3.1 `memory_write`（service 包装 + 工具 + 门卫 + 父级注册） | Task 7, 8, 9 |
| §3.2 steering 循环侧（队列 + 抽干 + 持久化 + `/` 行 + 上限 + 事件） | Task 11, 12 |
| §3.2 steering 终端侧（reader + REPL + 提问共用 + UI 开关） | Task 13, 14（先 Task 10 spike） |
| §3.3 工具面告警（origin + 阈值 + 一次性 + 渲染） | Task 4, 5, 6 |
| §3.4 `context_mode`（配置 + runner + 包字段 + 描述 + 组合根） | Task 1, 2, 3 |
| §5 数据契约（5 键 + 2 事件 + 3 字段） | Task 1（键）、5/12（事件）、2/4/8（字段） |
| §6 不变量（观测只写不改 / 一个读取器 / 缓存前缀） | Task 5（逐字节）、13（一个读取器）、2/12（默认不变） |
| §8 用例清单 | 各任务的 Step 1（总数 48+，与 §8 的分布一致） |
| §13 文档同步 | Task 15 |
| §14 明确不做 | 计划里没有任何一条触碰它们（无 SQLite、无 checkpoint、无停止语义、无自动压缩） |

**2. Placeholder scan**：`grep -n "TBD\|TODO\|待填\|类似 Task"` —— 只有 Task 10 Step 3 的
spike 结果表用「待填」，那是**记录实测结果的位置**（spike 的产物就是那张表），不是省掉的代码。
所有代码步骤都带完整代码。

**3. Type consistency**（跨任务的名字与签名）：

| 名字 | 定义处 | 使用处 | 一致 |
|---|---|---|---|
| `AIConfig.subagent_context_mode` / `tools_chars_warn` / `steering_max_messages` / `steering_max_chars` | Task 1 | 2, 5, 13 | ✓ |
| `UIConfig.steering` / `AppConfig.ui` | Task 1 | 13 | ✓ |
| `SubAgentRunner(..., base_instructions=...)` / `.context_mode` | Task 2 | 3 | ✓ |
| `SubagentResult.context_mode` | Task 2 | 15 | ✓ |
| `BASE_INSTRUCTIONS` | Task 3 | 3（cli + runner 透传） | ✓ |
| `BaseTool.origin` / `MCPToolAdapter.origin` | Task 4 | 5（`_tools_by_origin`） | ✓ |
| `ToolSurfaceWarning(tools_chars, threshold, origins, hint)` | Task 5 | 6（console） | ✓ |
| `MemoryService.write_note(text, *, kind, importance, supersedes, turn, session_id)` | Task 7 | 8（工具调用） | ✓ |
| `ToolContext.turn` / `tool_context(..., turn=0)` | Task 8 | 8（`_run_tools(turn=...)`） | ✓ |
| `MemoryWriteTool(memory)` / `MAX_TEXT` | Task 8 | 9（注册） | ✓ |
| `SteeringQueue(*, max_messages, max_chars)` / `put -> bool` / `drain` / `pending` | Task 11 | 12（loop）、13（reader） | ✓ |
| `TurnBooks.note_steering(count=1)` / `steering_messages` | Task 11 | 12 | ✓ |
| `InputSteered(text_chars, iteration, total)` | Task 12 | 15（console 走既有渲染；事件在 trace 里走默认分支） | ✓ |
| `InteractiveReader(..., prompt=, invalidate=, steering=, enabled=)` / `start/aclose/input/set_busy/ask` / `for_terminal` | Task 13 | 13（repl）、14（user_prompt） | ✓ |
| `CLIUserPromptCallback.set_interactive(reader)` | Task 14 | 13（run_repl 调用它） | ✓ |

**4. 己方两处偏差（写下来，免得被当成笔误）**：

1. `docs/r7e-analysis.md` §8.4 把「`/` 行不进历史」列在**循环**用例里；本计划把它放在
   **队列**（`SteeringQueue.put` 返回 `False`，Task 11），因为「队列里的东西只能是消息」是队列自己的不变量。
   分析文档的那条意图不变，位置更靠前。
2. `docs/r7e-analysis.md` §8.1 列了「runner 从配置读模式」一条；本计划用
   `SubAgentRunner.context_mode` 这个属性 + Task 3 的组合根透传覆盖同一件事，
   测试写在 `tests/test_subagent_context_mode.py` 与 `tests/test_tool_subagent.py`。

**5. 风险与顺序**：Task 10 是唯一的「可能推翻后续任务」的关卡，所以它**排在 Task 13 之前**；
Task 1–9 与它完全无关，任何情况下都不会白做。

**6. 关于各 Step 4 里的「全量 NNN passed」**：那些是**估算的累计值**（基线 942），
用来给你一个「是不是只动了该动的」的直觉。真正的判据是 Step 4 的前半句
（本任务的用例数）与「既有用例零改动仍通过」；执行时以实际数字为准，并在执行状态里如实回填。

---

## Execution Handoff

计划已保存到 `docs/r7e-plan.md`（本项目约定：`docs/rN-plan.md`，而不是技能默认的
`docs/superpowers/plans/YYYY-MM-DD-*.md`）。两种执行方式：

**1. Subagent-Driven（推荐）** —— 每个 Task 派一个子智能体实现，任务之间我来审（两层审查：
先看测试是否真的先红后绿，再看实现是否只做了该做的事）。

**2. Inline Execution** —— 在当前会话里按 executing-plans 分批执行，到检查点停下来给你看。

> 本项目的既有约定是**不派子任务、由主模型自己按 TDD 实现**（你在早前明确过一次），
> 所以我默认走 **Inline Execution**；如果你想换成子智能体并行，说一声即可。
