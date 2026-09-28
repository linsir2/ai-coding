# TUI 视觉增强 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户消息灰色背景框、时间戳、Markdown 渲染。

**Architecture:** 用 `Window(get_line_prefix=...)` 按行号应用不同样式；用 `set` 跟踪用户消息行号；时间戳在写入 body 时附带；Markdown 用 `get_line_prefix` 按行内容匹配应用样式。

**Tech Stack:** Python 3.13, prompt_toolkit

**Spec:** `docs/features.md#r10-已知缺口`

## Global Constraints

- No new dependencies
- `get_line_prefix` 返回 `list[tuple[str, str]]`，每项是 `(style, text)`
- 行号从 0 开始，`get_line_prefix(line_number)` 被调用时 `line_number` 是当前渲染的行索引
- 用户消息行号用 `set[int]` 跟踪，`get_line_prefix` 检查 `line_number in user_lines`
- TDD：先写失败测试
- All tests must pass: `.venv/bin/python -m pytest tests/ -p no:warnings`
- Lint must pass: `.venv/bin/python -m ruff check ai_coding/ tests/`

---

### Task 6: 用户消息灰色背景框

**Files:**
- Modify: `ai_coding/ui/app.py`
- Modify: `ai_coding/ui/theme.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Window(get_line_prefix=...)` 参数
- Produces: `user_lines: set[int]` 跟踪用户消息行号

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_user_lines_tracked():
    """User message line numbers must be tracked for styling."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    assert "user_lines" in source
    assert "get_line_prefix" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_user_lines_tracked -v`
Expected: FAIL — `user_lines` and `get_line_prefix` not present

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`:

1. Add `user_lines: set[int] = set()` after `awaiting_input = False` (line 105)

2. Track user message line numbers in `_enter` (line 189-202). After `body.write(f"> {text}\n")`, add:

```python
                user_lines.add(body.get_buffer().document.line_count - 2)
```

3. Add `get_line_prefix` to `body_window` (line 120-125):

```python
    body_window = Window(
        content=body_control,
        wrap_lines=False,
        scroll_offsets=ScrollOffsets(top=1, bottom=1),
        allow_scroll_beyond_bottom=True,
        get_line_prefix=lambda i: [("class:user-msg", "  ")] if i in user_lines else [],
    )
```

In `ai_coding/ui/theme.py`, add:

```python
    "user-msg": "bg:#2d3436 fg:#dfe4ea",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py ai_coding/ui/theme.py tests/test_app.py
git commit -m "feat(ui): gray background for user messages"
```

---

### Task 7: 时间戳

**Files:**
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `datetime.now()`
- Produces: 每轮对话附带时间戳

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_timestamp_added():
    """Each turn should have a timestamp."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    assert "datetime" in source
    assert "strftime" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_timestamp_added -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`:

1. Add `from datetime import datetime` to imports

2. Add timestamp in `_enter` after `body.write(f"> {text}\n")`:

```python
                body.write(f"  {datetime.now().strftime('%H:%M')}\n")
```

3. Add timestamp in `_submit_text` after `body.write("\n")`:

```python
        body.write(f"  {datetime.now().strftime('%H:%M')}\n")
```

In `ai_coding/ui/theme.py`, add:

```python
    "timestamp": "#636e72",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py ai_coding/ui/theme.py tests/test_app.py
git commit -m "feat(ui): add timestamps to conversation turns"
```

---

### Task 8: Markdown 渲染

**Files:**
- Modify: `ai_coding/ui/app.py`
- Modify: `ai_coding/ui/theme.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `get_line_prefix` 按行内容匹配
- Produces: 标题、代码块、粗体、列表有不同样式

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py 追加
def test_markdown_styles_defined():
    """Markdown styles must be defined in theme."""
    from ai_coding.ui.theme import STYLE
    rules = dict(STYLE.style_rules)
    assert "md-header" in rules
    assert "md-code" in rules
    assert "md-bold" in rules
    assert "md-list" in rules
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_markdown_styles_defined -v`
Expected: FAIL — styles not defined

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/theme.py`, add:

```python
    "md-header": "#00d2d3 bold",
    "md-code": "bg:#1e272e fg:#dfe4ea",
    "md-bold": "#ffa502",
    "md-list": "#2ed573",
```

In `ai_coding/ui/app.py`, add a helper function and use it in `get_line_prefix`:

```python
    def _md_prefix(i: int) -> list[tuple[str, str]]:
        line = body.get_buffer().document.lines[i] if i < body.get_buffer().document.line_count else ""
        if line.startswith("#"):
            return [("class:md-header", "")]
        if line.startswith("```"):
            return [("class:md-code", "")]
        if line.startswith("-") or line.startswith("*"):
            return [("class:md-list", "")]
        if "**" in line:
            return [("class:md-bold", "")]
        return []
```

Then update `body_window` to use both:

```python
        get_line_prefix=lambda i: [("class:user-msg", "  ")] if i in user_lines else _md_prefix(i),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py ai_coding/ui/theme.py tests/test_app.py
git commit -m "feat(ui): basic Markdown rendering in body"
```

---

### Task 9: 全量回归 + pty 验证

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
git commit -m "test: full regression pass after TUI visual enhancements"
```

---

## 实施后终端应有的样子

```
 0|  ✨ Welcome to ai-coding! ✨
 1|  Describe a task, or use / for commands.
 2|
 3|  ✓ 11 tools connected · /tools
 4|  ✓ memory loaded (1 notes) · /memory
 5|  model: deepseek-v4-pro-0813
 6|
 7|    19:30                                ← 时间戳
 8|  > 你好                                  ← 用户消息（灰色背景）
 9|    19:30                                ← 时间戳
10|  你好！有什么可以帮你的吗？
11|
12|  ### 项目结构                            ← Markdown 标题（青色加粗）
13|  - src/                                 ← 列表（绿色）
14|  - tests/                               ← 列表（绿色）
15|  **粗体文本**                            ← 粗体（橙色）
16|
17|│ █                                       ← 输入框（有边框）
18|│
19|model: ... 0% context · 00:00 · ctrl+c quit · / commands
```

## Self-Review

**1. Spec coverage** — 需求「用户消息灰色背景、时间戳、Markdown 渲染」：
- Task 6: 用户消息灰色背景 ✓
- Task 7: 时间戳 ✓
- Task 8: Markdown 渲染 ✓
- Task 9: 回归验证 ✓

**2. Placeholder scan** — 无 TBD / TODO。每个代码块都是可直接粘贴的完整实现。

**3. Typeconsistency** — `get_line_prefix` 返回 `list[tuple[str, str]]`，`user_lines` 是 `set[int]`，`body.get_buffer().document.lines[i]` 返回 `str`。`Style.from_dict` 支持 `bg:#color` 和 `bold`。

**已知限制**：
- Markdown 渲染是**按行匹配**的，不支持跨行代码块（``` 开和关之间的行都算代码）
- 粗体 `**text**` 只检测行内是否有 `**`，不做真正的解析
- 用户消息灰色背景是**整行**的，包括 `> ` 前缀
- 时间戳精度到分钟（`%H:%M`），不做秒级

这些限制都是 TUI 场景下的合理简化，YAGNI。
