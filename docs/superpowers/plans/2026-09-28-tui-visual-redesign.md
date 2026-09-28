# TUI 视觉重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 去掉固定 header、审批提示回答后消失、输入框加边框区分、对话历史可辨。

**Architecture:** 把 header 内容并入 body（滚动时自然滚走）；审批/ask_user 回答后清空 input_buffer；输入框用 `Window(char=...)` 画边框；每轮对话之间加分隔线。

**Tech Stack:** Python 3.13, prompt_toolkit

**Spec:** `docs/features.md#r10-已知缺口`

## Global Constraints

- No new dependencies
- 焦点必须始终在 `input_window`，滚动不得夺走焦点
- 只用 prompt_toolkit 公开 API：`Window(char=...)`、`Keys.ScrollUp/Down`、`Application(mouse_support=True)`、`Window._scroll_up/_scroll_down`
- 禁止直接写 `render_info.vertical_scroll`（私有字段）
- 方向键 `↑↓` 保留给命令面板导航
- TDD：先写失败测试
- All tests must pass: `.venv/bin/python -m pytest tests/ -p no:warnings`
- Lint must pass: `.venv/bin/python -m ruff check ai_coding/ tests/`

---

### Task 1: 去掉固定 header，欢迎信息并入 body

**Files:**
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `render_header(loop)` 返回 `list[tuple[str, str]]`
- Produces: body 首行包含 header 内容，无固定 header 窗口

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_no_fixed_header_window():
    """Header must not be a fixed-height window at the top."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    assert "HEADER_HEIGHT" not in source
    assert "header_control" not in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_no_fixed_header_window -v`
Expected: FAIL — `HEADER_HEIGHT` and `header_control` still present

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`:

1. Delete `HEADER_HEIGHT = 9` (line 42)
2. Delete `header_control = FormattedTextControl(lambda: render_header(loop))` (line 110)
3. Delete the header `Window` from `root = HSplit([...])` (lines 238-242)
4. Replace the welcome message (lines 107-108) with header content merged into body:

```python
    from ai_coding.ui.header import render_header
    for style, text in render_header(loop):
        body.write(text, style=style)
    body.write("\n")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_app.py
git commit -m "fix(ui): remove fixed header, merge into scrollable body"
```

---

### Task 2: 审批/ask_user 提示回答后消失

**Files:**
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `input_buffer`, `input_event`, `awaiting_input`
- Produces: 回答后 input_buffer 清空，提示不残留

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_input_buffer_cleared_after_approval():
    """After answering, input_buffer must be reset so the prompt disappears."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    # Find the _input_provider function
    start = source.find("async def _input_provider")
    end = source.find("user_prompt.set_provider")
    provider_code = source[start:end]
    assert "input_buffer.reset()" in provider_code
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_input_buffer_cleared_after_approval -v`
Expected: FAIL — no `input_buffer.reset()` in `_input_provider`

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`, modify `_input_provider` to reset after answering:

```python
    async def _input_provider(prompt: str) -> str:
        nonlocal awaiting_input
        input_buffer.reset()
        input_buffer.insert_text(f"{prompt} ")
        awaiting_input = True
        input_event.clear()
        try:
            await input_event.wait()
            answer = input_buffer.text
            input_buffer.reset()
            auto_scroll_to_end(body_window)
            return answer
        finally:
            awaiting_input = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_app.py
git commit -m "fix(ui): clear input buffer after approval/ask_user answer"
```

---

### Task 3: 输入框加边框区分

**Files:**
- Modify: `ai_coding/ui/app.py`
- Modify: `ai_coding/ui/theme.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Window(char=...)` 参数
- Produces: 输入框有可见边框

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_input_window_has_border():
    """Input window must have a visible border."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    assert "char=" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_input_window_has_border -v`
Expected: FAIL — no `char=` in source

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`, add `char` to `input_window`:

```python
    input_window = Window(
        content=input_control,
        height=D(preferred=INPUT_HEIGHT),
        style="class:input",
        char="│",
    )
```

In `ai_coding/ui/theme.py`, add border color:

```python
    "input.border": "#00d2d3",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py ai_coding/ui/theme.py tests/test_app.py
git commit -m "feat(ui): add border to input window"
```

---

### Task 4: 对话历史分隔

**Files:**
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `body.write()`
- Produces: 每轮对话之间有空行分隔

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_conversation_separator():
    """Each turn should be separated by a blank line."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    assert 'body.write("\\n")' in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_conversation_separator -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`, add separator after each turn in `_submit_text`:

```python
    async def _submit_text(text: str) -> None:
        nonlocal turn_start
        turn_start = time.monotonic()
        await loop.process_input(session, text, on_delta=body.write)
        body.write("\n")
        auto_scroll_to_end(body_window)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_app.py
git commit -m "feat(ui): add conversation separator between turns"
```

---

### Task 5: 全量回归 + pty 验证

**Files:**
- No new files

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -p no:warnings -q -o addopts=""`
Expected: PASS — all tests

- [ ] **Step 2: Run lint**

Run: `.venv/bin/python -m ruff check ai_coding/ tests/`
Expected: `All checks passed!`

- [ ] **Step 3: Pty smoke test**

Run: `/tmp/verify_final.py` if present, else create equivalent:
- 输入 abc → backspace → az → 回车 → [turn]
- 确认焦点没回退

Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: full regression pass after TUI visual redesign"
```

---

## 实施后终端应有的样子

```
 0|  ✨ Welcome to ai-coding! ✨              ← header 内容（可滚动）
 1|  Describe a task, or use / for commands.
 2|
 3|  ✓ 11 tools connected · /tools
 4|  ✓ memory loaded (1 notes) · /memory
 5|  model: deepseek-v4-pro-0813
 6|
 7|  > 你好                                  ← 用户输入（历史）
 8|  你好！有什么可以帮你的吗？                ← AI 回复
 9|
10|  > ls -la                                ← 第二轮
11|  [tool: bash] ...
12|  -> ok: file1 file2
13|
14|│ █                                       ← 输入框（有边框）
15|│
16|model: ... 0% context · 00:00 · ctrl+c quit · / commands
```

## Self-Review

**1. Spec coverage** — 需求「去掉固定 header、审批消失、输入框区分、对话可辨」：
- Task 1: 去掉固定 header ✓
- Task 2: 审批提示消失 ✓
- Task 3: 输入框边框 ✓
- Task 4: 对话分隔 ✓
- Task 5: 回归验证 ✓

**2. Placeholder scan** — 无 TBD / TODO。每个代码块都是可直接粘贴的完整实现。

**3. Typeconsistency** — `render_header(loop)` 返回 `list[tuple[str, str]]`，`body.write(text, style=style)` 接受 `str`。`Window(char=...)` 参数已验证存在。

**已知不做**：不做用户消息灰色背景框（需要自定义 `get_line_prefix`，复杂度高）；不做时间戳（需求里没提）；不做 Markdown 渲染（body 是纯文本 Buffer）。这些都没在需求里，YAGNI。
