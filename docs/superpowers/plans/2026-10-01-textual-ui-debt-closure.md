# TUI 欠账清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉三笔真实欠账（Esc 打断接线、ask_user 选项渲染、长会话 widget 增长），并让台账与文档重新可信。

**Architecture:** 三个独立切面，各自可单独交付、单独拒绝。T1 只接线既有机制（零新增核心抽象）；T2 只改 provider 签名一个参数；T3 先测量再决定是否启用混合容器——测量是硬前置，不测不实现。

**Tech Stack:** Textual 8.2.8、asyncio、pytest + ruff + mypy(strict)

**Spec:** `docs/superpowers/specs/2026-09-28-textual-ui-design.md`（§7 风险 3/风险 5、§4.2、§9）+ `.superpowers/sdd/2026-09-28-textual-ui/progress.md`（ledger）

## Global Constraints

- 分支 `textual-ui`；`run_app` 签名不变（`textual_app.py:275`）。
- `DeltaSink = Callable[[str], None]` 保持同步（`agent_loop.py:114`）——UI 侧只能「同步攒 + 定时 flush」。
- 循环必须跑在 `@work(thread=False)`；`thread=True` 会让工具内 `push_screen_wait` 抛 `NoActiveWorker`（spec §7 风险 1）。
- 模态必须 `try/finally` + `if self.screen is s: self.pop_screen()`（`push_screen_wait` 内部 shield，取消不关屏）。
- 三门槛：`.venv/bin/python -m pytest -p no:cacheprovider` / `.venv/bin/python -m ruff check .` / `.venv/bin/python -m mypy ai_coding`。mypy 必须**归零**，不是「不新增」。
- 台账 `.superpowers/sdd/` 已 gitignore；每个 Task 结束要把结论**同时**写进 `progress.md` 的「已修」和「不修」两处——本次审计证明只写一处会让下一个人读到两个矛盾的真相。
- 绝不改既有测试来迁就实现（除该测试测的是已被本任务消灭的 bug，须在 commit message 写明删因）。

---

### Task 1: 文档与台账复位（零代码）

**Files:**
- Modify: `docs/features.md:34`（删「命令面板尚未接线」从句）、`:365`（行号核对）、`:46`（`/plan` 补「主题不回显」）
- Modify: `docs/README.md:6`、`:124-125`
- Modify: `docs/how-to-run.md:51`、`:68`（「两条已知退化」→「一条」）
- Modify: `docs/superpowers/specs/2026-09-28-textual-ui-design.md:247`（删命令面板非目标行）
- Modify: `ai_coding/ui/modals.py:93`（docstring 说谎）
- Modify: `.superpowers/sdd/2026-09-28-textual-ui/progress.md:174-180`（F5/F10 已修却列「不修」）
- Delete: `tests/test_repl_cancel_pty.py`（孤儿：引用的 `tests/_pty_cancel_child.py` 与 `tests/test_ui_reader.py` 都不存在）
- Create: `tests/_pty_cancel_child.py` ← **不**，见 Task 3；本任务只删孤儿

**Interfaces:**
- Consumes: 无
- Produces: 无代码契约

- [ ] **Step 1: 确认孤儿测试确实无引用**

```bash
cd /home/linsir365/projects/ai-coding
ls tests/_pty_cancel_child.py tests/test_ui_reader.py 2>&1   # 期望: 两个 No such file
grep -rn "test_repl_cancel_pty" --include=*.py --include=*.yml --include=*.md . | grep -v "^./tests/test_repl_cancel_pty.py"
```

- [ ] **Step 2: 记录当前文档矛盾作为 RED 证据**

```bash
grep -n "命令面板尚未接线" docs/features.md docs/README.md docs/how-to-run.md
grep -n "两条.*已知退化" docs/README.md docs/how-to-run.md
```

期望输出（这就是要修的清单，共 6 处 + spec 1 处）：
`docs/features.md:34`、`docs/README.md:6`、`docs/how-to-run.md:51` 各 1 处「命令面板尚未接线」；`docs/README.md:124-125` 与 `docs/how-to-run.md:68` 各 1 处「两条」。

- [ ] **Step 3: 修 `features.md:34`**

把该行改为：

```
输入 `/` 会唤起命令面板（见 R12 节），所以 `/` 本身不是普通文本
```

- [ ] **Step 4: 修 `features.md:46`（`/plan` 行）**

在行尾追加：``；主题也不回显（`_plan_turn` 不调 `append_user`），transcript 里看不到你规划的是什么``

- [ ] **Step 5: 修 README 与 how-to-run 的 4 处**

`docs/README.md:6` 与 `docs/how-to-run.md:51`：删掉 `（命令面板尚未接线）` 整个括注从句。
`docs/README.md:124-125` 与 `docs/how-to-run.md:68`：把「两条已知退化（命令面板未接；…）」改为「一条已知退化（模型驱动的 slash 命令不渲染工具事件块；`/plan` 不在此列）」；并从「明确不做」清单里删掉「命令面板」。

- [ ] **Step 6: 删 spec §9 的命令面板行**

删 `2026-09-28-textual-ui-design.md:247` 整行。该 spec `:29` 的 §3 表已把命令面板列为要实现，与 §9 自相矛盾。

- [ ] **Step 7: 修 `modals.py:93` 的 docstring**

改为：`"""Ask one question.  ``options`` is rendered when a caller supplies it."""`

- [ ] **Step 8: 删孤儿测试**

```bash
git rm tests/test_repl_cancel_pty.py
```

- [ ] **Step 9: 修台账矛盾（这是本任务真正的价值）**

`progress.md:174-180` 的「记 ledger 不修」区块里：
- F5 拆两半：三态配色**已修**（`app.tcss:85-91` + `tests/test_transcript.py:282`），从「不修」移出；残留「`.block-tool`/`.block-notice`/`.block-task` 无 CSS 规则」保留在「不修」。
- F10 整条从「不修」移出，标 ✅ 已修（`transcript.py:30-35`）。
- F9 改描述：`transcript.py:124,165` 已清 `_tools`/`_turn`，所以迟到事件挂的是**无字形**的 `Static`，不是「标题停在 `▸`」。结论「不可达/优雅降级」不变。
- 新增一条审计发现（本次发现，台账无记录）：`notice`/`task`/`turn`/`turn-end` 四类块永远落在 Turn 之外（`transcript.py:170-184` 只把 `tool`/`tool-chunk` 放进 Turn），导致 `IterationStarted` 的 `[turn i/n]`（`blocks.py:72`）渲染在它所描述的回合**下方**。归入「不修」，注明「B2 turn 分组对这四类未生效，零覆盖」。
- 在「已修」侧补：台账 `:137` Task 8 的 14 行清单**已失效 3 行**（`ctrl+c` 打印 `bye` 永不成立、`scripts/tui_shot.py:37` 的 `tool-end` 不是流水线 kind、滚轮与工具事件两行被 F6/F8 命中）。

- [ ] **Step 10: 跑门禁并提交**

```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
.venv/bin/python -m pytest -p no:cacheprovider 2>&1 | tail -1
git add -A && git commit -m "docs: stop the ledger and the docs from lying about what exists

The command palette shipped at feee611, but six places still said it had not
been wired -- features.md:34 even contradicted features.md:365 in the same
file. The ledger is worse: F5 and F10 were listed as 'deferred' after
fix wave 2 had already fixed them, so the same document held two
contradictory truths.

Also removes tests/test_repl_cancel_pty.py, whose child script and
referenced test file no longer exist; it was skipped unless
AI_CODING_PTY=1, so CI never noticed."
```

---

### Task 2: Esc 接上既有的 cancel 开关

**Files:**
- Modify: `ai_coding/cli.py:85`、`:149`（`cancel` 参数现在恒为 `None`）
- Modify: `ai_coding/ui/textual_app.py:29-32`（BINDINGS）、`:320`（传给 `_build_loop`）
- Test: `tests/test_textual_app.py`

**Interfaces:**
- Consumes: 既有 `ToolDispatcher.cancel: asyncio.Event | None`（`dispatch.py:171`）、`AgentLoop.cancel`（`agent_loop.py:154,182`）、`run_with_deadline(cancel=...)`（`deadline.py:132`）
- Produces: `AICodingApp.action_cancel_turn() -> None`；`AICodingApp._cancel_event: asyncio.Event`

**为什么不是新功能：** 全链已存在——`dispatch.py:171` 注释 `# R8c: the user's "drop this batch" switch (Esc)`、`deadline.py:132` 的 `cancel` 参数、`dispatch.py:446-450` 捕获 `RunCancelled` 后 `kill.set()` 杀子进程组、`turn_close.py:87` 记 `TurnStatus.ABORTED`。缺的只有：`asyncio.Event()` 实例化 + `.set()` 调用点 + Esc 绑定。

**关键语义（已核实，勿改）：** 一次 Esc 丢**当前这批**工具，不是整个回合。`agent_loop.py:566-568` 在每轮 dispatch 前 `self.cancel.clear()`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_textual_app.py`：

```python
async def test_escape_sets_the_cancel_event_the_dispatcher_waits_on():
    """Esc must reach the switch ToolDispatcher already watches.

    The whole chain exists (dispatch.py:171, deadline.py:132); only the
    wiring was missing, which is why there was no way to interrupt a long
    tool batch.
    """
    loop = _FakeLoop()
    app = _app(loop=loop)
    async with app.run_test() as pilot:
        await pilot.press("escape")
        assert app._cancel_event.is_set()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py::test_escape_sets_the_cancel_event_the_dispatcher_waits_on -q`

Expected: FAIL —— `AttributeError: 'AICodingApp' object has no attribute '_cancel_event'`

- [ ] **Step 3: 加事件与绑定**

在 `textual_app.py` 的类属性声明区（`:38-46`）加：

```python
    _cancel_event: asyncio.Event
```

在 `__init__` 末尾（`:63` 后）加：

```python
        self._cancel_event = asyncio.Event()
```

`__init__` 需要 `import asyncio`（`:6` 附近加 `import asyncio`）。

BINDINGS（`:29-32`）改为：

```python
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("escape", "cancel_turn", "Cancel tool batch", show=False),
    ]
```

加 action（放在 `run_turn` 之后，`:233` 附近）：

```python
    def action_cancel_turn(self) -> None:
        """Drop the tool batch that is running (R8c's switch, finally wired).

        Semantics are the existing ones: ``agent_loop.py:566-568`` clears the
        event before each dispatch, so one Escape drops the current batch and
        the loop keeps going with a fresh one.
        """
        self._cancel_event.set()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py -q`
Expected: PASS

- [ ] **Step 5: 接到真 loop**

`textual_app.py:313-321` 的 `_build_loop(...)` 加 `cancel=self._cancel_event,`：

```python
    loop = _build_loop(
        engine, sessions, registry, app_cfg, workspace, skills,
        approval=approval,
        observer=None,
        trace=recorder,
        guards=_make_guards(app_cfg),
        hooks=_make_hooks(app_cfg, workspace),
        steering=steering,
        cancel=self._cancel_event,
    )
```

`cli.py:85` 的签名已有 `cancel: Any | None = None`、`:149` 已在 `_build_loop` 内部透传 —— **不改 `cli.py`**，只需从 `textual_app.py` 传进去。先核实 `cli.py:140-152` 确实把它传给 dispatcher/loop。

- [ ] **Step 6: 跑门禁**

```bash
.venv/bin/python -m pytest -p no:cacheprovider 2>&1 | tail -1
.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
```

Expected: `12xx passed, 3 skipped`；ruff 绿；mypy 绿。

- [ ] **Step 7: 提交**

```bash
git add -A && git commit -m "feat(ui): Esc drops the running tool batch

ToolDispatcher already had the switch (dispatch.py:171), run_with_deadline
already abandoned on it (deadline.py:132) and dispatch already killed the
subprocess group when it fired (dispatch.py:446-450). Nothing ever set it,
so a long bash call could only be escaped by quitting the app.

Now the app owns one asyncio.Event, binds Escape to it, and passes it into
_build_loop. One press drops the current batch, not the turn:
agent_loop.py:566-568 clears the event before each dispatch."
```

---

### Task 3: ask_user 的选项真的渲染出来

**Files:**
- Modify: `ai_coding/security/user_prompt.py:52-57`（`_format_prompt`）、`:65`（`set_provider` 签名）、`:74`（调用点）
- Modify: `ai_coding/ui/textual_app.py:261-262`（`ask_provider` 签名与 `AskScreen` 调用）
- Modify: `ai_coding/ui/modals.py:97-106`（`AskScreen.__init__` 已支持 options，确认无需改）
- Modify: `ai_coding/ui/app.tcss`（加 `#ask-options`）
- Test: `tests/test_user_prompt.py`、`tests/test_textual_app.py`、`tests/test_modals.py`

**Interfaces:**
- Consumes: `AskScreen(question: str, options: tuple[str, ...] = ())`（`modals.py:97` 已存在）
- Produces: `CLIUserPromptCallback.set_provider(Callable[..., Any])`；`AICodingApp.ask_provider(question: str, options: Sequence[str] = ()) -> str`

**为什么这是「接线」不是「新增」：** `modals.py:97-106` 的 `options` 参数与 `#ask-options` 渲染**已经写好了**（`compose()` 里 `if self._options:` 才渲染那一行），只是两个调用点（`textual_app.py:186`、`:262`）都不传，所以是死代码。审批通道已经走过这条路（`approval.py:74` 三参签名）。

**前置约束：** 非 UI 路径（CLI/stdin）的输出格式**必须逐字节不变**——`user_prompt.py:81` 的 stdin 兜底仍走 `_format_prompt(question, options)`，既有测试锁住。改的只是 provider 那一路（`:74`）。

- [ ] **Step 1: 写失败测试（provider 拿到结构化数据）**

追加到 `tests/test_user_prompt.py`：

```python
def test_the_provider_receives_the_question_and_the_options_apart():
    """The modal can only list options if it is handed them, not a blob.

    ``_format_prompt`` flattens question+options into one string, so
    AskScreen's options parameter could never be filled.
    """
    seen: list[tuple[object, ...]] = []

    async def provider(*args: object) -> str:
        seen.append(args)
        return "answer"

    cb = CLIUserPromptCallback()
    cb.set_provider(provider)  # type: ignore[arg-type]
    assert cb.ask("Which file?", options=["a.py", "b.py"]) == "answer"
    assert seen == [("Which file?", ["a.py", "b.py"])]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_user_prompt.py::test_the_provider_receives_the_question_and_the_options_apart -q`

Expected: FAIL —— `TypeError` 或 provider 收到的是被拍平的字符串

- [ ] **Step 3: 改 provider 调用点与签名（签名已核实）**

`user_prompt.py:67` 的 `_provider: Callable[[str], Any]` 与 `:70` 的 `set_provider(provider: Callable[[str], Any])` 改为 `Callable[..., Any]`，与 `approval.py` 一致。

`:74` 的调用点改为：

```python
                raw = await self._provider(question, list(options))
```

**必须保持不变的**（既有测试锁住，改了就 FAIL 即回退）：
- `:78` 的 stdin 兜底仍走 `_format_prompt(question, options)` —— 非 UI 路径逐字节不变
- `:75-83` 的异常/空串语义不动（空答案 → `UserAnswer(answered=False, reason="empty")`）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_user_prompt.py -q`
Expected: PASS（含既有 stdin 格式断言——若它们 FAIL，说明改坏了非 UI 路径，**回退重做**）

- [ ] **Step 5: 写失败测试（UI 侧渲染）**

追加到 `tests/test_textual_app.py`：

```python
async def test_ask_screen_receives_the_options_the_model_offered():
    """The modal's options parameter must not stay dead code."""
    app = _app(loop=_FakeLoop())
    captured: dict[str, object] = {}

    async def fake_ask(question: str, options: object = ()) -> str:
        captured["q"] = question
        captured["o"] = options
        return ""

    async with app.run_test() as pilot:
        app._ask_provider = fake_ask
        worker = pilot.app.run_worker(app.ask_provider("Which file?", ["a.py", "b.py"]), exclusive=False)
        await worker.wait()
    assert captured["o"] == ["a.py", "b.py"]
```

- [ ] **Step 6: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_textual_app.py::test_ask_screen_receives_the_options_the_model_offered -q`

Expected: FAIL —— `ask_provider` 只接受一个参数

- [ ] **Step 7: 改 `ask_provider`**

`textual_app.py:261-262`：

```python
    async def ask_provider(self, question: str, options: object = ()) -> str:
        screen = AskScreen(question, tuple(options or ()))
```

- [ ] **Step 8: 加 CSS**

`app.tcss` 的模态区（`#ask-options` 附近）加：

```css
#ask-options {
    color: $text-muted;
    padding: 0 0 0 2;
}
```

- [ ] **Step 9: 跑测试与门禁**

```bash
.venv/bin/python -m pytest tests/test_textual_app.py tests/test_user_prompt.py tests/test_modals.py -q
.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
```

- [ ] **Step 10: 提交**

```bash
git add -A && git commit -m "feat(ui): ask_user lists the options the model offered

AskScreen has taken an options tuple and rendered it under #ask-options
since it was written, but nothing ever passed one: the provider signature
was Callable[[str], Any] and _format_prompt flattened question+options
into a single string. The rendering was dead code and modals.py:93 even
claimed 'options are shown'.

Now the provider gets the question and the options separately -- the same
two-step the approval channel already took. The CLI/stdin path still goes
through _format_prompt and is byte-for-byte unchanged."
```

---

### Task 4: 测量长会话的 widget 增长（**测量先行，实现后置**）

**Files:**
- Create: `tests/test_transcript_widget_growth.py`
- Test: 同上
- Modify: `.superpowers/sdd/2026-09-28-textual-ui/progress.md`（把 spec §7 风险 3 承诺的「先测量」兑现成数字）

**Interfaces:**
- Consumes: `Transcript.begin_assistant/stream/end_assistant`
- Produces: 无生产代码改动。产出一张 N×widget 数的表 + 一个常驻测试。

**为什么先测：** spec §7 风险 3 承诺「v1 先测量再启用」混合容器，但**这步测量从来没做**，也没有留下任何数字。我没有实测帧耗时，不能断言已经卡。这个 Task 的产出是数据，不是优化。

- [ ] **Step 1: 写测量测试**

Create `tests/test_transcript_widget_growth.py`。`Transcript` 不是 App，不能直接 `run_test()`，所以借 `tests/test_textual_app.py` 已有的 `_app()` / `_FakeLoop()` 工厂起一个真 app：

```python
"""Spec §7 risk 3 promised "measure before enabling" the mixed container.

Nothing ever measured it, so there was no number to decide with. This pins
the curve: one Turn widget survives per finished turn, and its Markdown goes
with it. It passes today — that is the point. It is a characterisation test,
not a bug report, and the number it records is the reason to or not to build
the hybrid container later.
"""

import pytest

from ai_coding.ui.transcript import Transcript, Turn
from tests.test_textual_app import _FakeLoop, _app


@pytest.mark.parametrize("turns", [1, 10, 25, 50])
async def test_one_turn_widget_survives_per_finished_turn(turns):
    app = _app(loop=_FakeLoop())
    async with app.run_test() as pilot:
        transcript = app.query_one(Transcript)
        for i in range(turns):
            transcript.begin_assistant("12:00")
            transcript.stream(f"answer {i}")
            await transcript.end_assistant()
        await pilot.pause()
        # end_assistant() drops the reference (transcript.py:164-167) but never
        # removes the widget, so the tree grows one Turn per turn.
        assert len(transcript.query(Turn)) == turns
```

- [ ] **Step 2: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_transcript_widget_growth.py -q`

Expected: PASS（**4 个参数化用例全 PASS**——增长是既成事实，测试只是把它钉住。若 FAIL，说明 Turn 有别的回收路径，需回头核 `transcript.py:163-168`）

- [ ] **Step 3: 记录数字到台账**

在 `progress.md` 新增一节，填入实测的 `Turn` / `Markdown` / `RichLog` 数量（用 `transcript.query(...)` 数，或直接数 `transcript.children`），并明确写一句裁决：

> 决定：**本轮不实现混合容器**。理由与数字写在此处；若将来 N>100 出现卡顿，混合容器是那时的答案。

- [ ] **Step 4: 跑门禁并提交**

```bash
.venv/bin/python -m pytest -p no:cacheprovider 2>&1 | tail -1
.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
git add -A && git commit -m "test: measure the transcript's widget growth

Spec section 7 risk 3 said 'measure before enabling' the hybrid container
and then never measured, so there was no number to decide with. This pins
the curve: one Turn widget survives per finished turn, and its Markdown
goes with it. The decision not to implement the container yet, and the
number behind it, are recorded in the ledger."
```

---

## 明确不做（记录理由，避免下次重新讨论）

| 项 | 决定 | 理由 |
|---|---|---|
| 混合容器（回合转 RichLog） | 本轮不做 | Task 4 测量后决定 |
| F6 RichLog 不换行（窄终端裁切） | 不做 | 需换 `RichLog(wrap=True, min_width=...)` + CSS max-height，属 Task 级改动；窄终端场景罕见 |
| F8 嵌套滚动 | 不做 | 需真实终端滚轮才能验证，headless 测不出 |
| F9 post-end 迟到事件 | 不做 | 已确认不可达（`dispatch.py:199` gather 保证顺序） |
| F7 `/plan` 精确匹配 | 不做 | 已文档化的已知行为 |
| 主题切换 UI | 不做 | spec 误写「默认亮色」——实测 `DEFAULT_THEME == "textual-dark"`，实际是暗色 |
| 侧栏四屏 | 不做 | 后端无数据模型 |
| 欢迎页 | 可选 | 纯装饰，优先级低于上面四项 |

## 架构：改动后的数据流

```
用户在 turn 进行中按 Esc
    │
    ├─→ App.action_cancel_turn()                    [新增, textual_app.py]
    │       self._cancel_event.set()
    │              │
    │              ↓
    │       ToolDispatcher.cancel                   [既有, dispatch.py:171]
    │              │
    │              ↓
    │       run_with_deadline(cancel=...)           [既有, deadline.py:132]
    │              │  watcher = cancel.wait()
    │              │  → asyncio.wait(FIRST_COMPLETED)
    │              │  → watcher done → _abandon(task) → raise RunCancelled
    │              ↓
    │       dispatch._execute except RunCancelled    [既有, dispatch.py:446]
    │              kill.set()   ← 子进程组被杀，不是只停止等待
    │              return self._cancelled(call)      [既有, :609]
    │              ↓
    │       TurnStatus.ABORTED, reason="cancelled"  [既有, turn_close.py:87]
    │              ↓
    │   agent_loop 继续下一轮 iteration
    │   (每轮 dispatch 前 agent_loop.py:566-568 cancel.clear())
    └─→ 回合 NOT 终止 ← 一次 Esc 只丢当前批


模型调 ask_user(question, options)
    │
    └─→ CLIUserPromptCallback.ask                  [改, user_prompt.py:74]
            self._provider(question, list(options))    ← 不再拍平
                  │
                  ↓
            App.ask_provider(question, options)     [改, textual_app.py:261]
                  │
                  ↓
            AskScreen(question, options)            [既有 UI, 首次拿到 options]
                  │  #ask-options 渲染选项           [新增 CSS]
                  ↓
            push_screen_wait → 用户回答/空串
```

## 影响范围汇总

| 文件 | T1 文档 | T2 Esc | T3 选项 | T4 测量 |
|---|---|---|---|---|
| `docs/features.md` | ✓ | | | |
| `docs/README.md` | ✓ | | | |
| `docs/how-to-run.md` | ✓ | | | |
| `specs/2026-09-28-textual-ui-design.md` | ✓ | | | |
| `progress.md` | ✓ | ✓ | ✓ | ✓ |
| `ai_coding/ui/modals.py` | docstring | | 确认无需改 | |
| `ai_coding/ui/textual_app.py` | | ✓ | ✓ | |
| `ai_coding/ui/app.tcss` | | | ✓ | |
| `ai_coding/security/user_prompt.py` | | | ✓ | |
| `ai_coding/cli.py` | | 核实（不改） | | |
| `tests/test_repl_cancel_pty.py` | 删除 | | | |
| `tests/test_textual_app.py` | | ✓ | ✓ | |
| `tests/test_user_prompt.py` | | | ✓ | |
| `tests/test_transcript_widget_growth.py` | | | | 新建 |
| **核心逻辑文件** | **0** | **0** | **0** | **0** |

**零核心逻辑改动**——三个切面全部是接线与测量，`agent_loop.py`/`dispatch.py`/`deadline.py`/`steering.py` 一行不动。这是本计划最重要的性质：风险全在 UI 层，核心路径零回归面。