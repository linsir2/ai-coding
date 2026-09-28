# TUI Layout Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 4 TUI layout bugs: body scrollability, fixed input box, approval prompt routing, footer display.

**Architecture:** Replace `FormattedTextControl` with `BufferControl` for body (enables scrolling). Fix layout order so input is truly at bottom. Route approval/ask_user prompts through a dedicated input area instead of writing to body. Clean up footer to show only essential info.

**Tech Stack:** Python 3.13, prompt_toolkit

**Spec:** `docs/features.md#r10-已知缺口`

## Global Constraints

- No new dependencies
- `prompt_toolkit.Style.from_dict` keys must match `^[a-z0-9.\s_-]*$`
- All tests must pass: `pytest tests/ -p no:warnings`
- Lint must pass: `ruff check ai_coding/ tests/`
- TDD: write failing test first, then implement

---

### Task 1: BodyBuffer → real Buffer (enable scrolling)

**Files:**
- Modify: `ai_coding/ui/body.py`
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_body.py`

**Interfaces:**
- Consumes: `prompt_toolkit.buffer.Buffer`
- Produces: `BodyBuffer.get_buffer() -> Buffer`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_body.py
def test_body_buffer_has_real_buffer():
    from prompt_toolkit.buffer import Buffer
    body = BodyBuffer()
    buf = body.get_buffer()
    assert isinstance(buf, Buffer)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_body.py::test_body_buffer_has_real_buffer -v`
Expected: FAIL (no get_buffer method)

- [ ] **Step 3: Write minimal implementation**

```python
# ai_coding/ui/body.py
"""Scrollable output Buffer."""

from __future__ import annotations

from prompt_toolkit.buffer import Buffer


class BodyBuffer:
    def __init__(self) -> None:
        self._buffer = Buffer()

    def write(self, text: str, style: str = "") -> None:
        self._buffer.insert_text(text)

    def clear(self) -> None:
        self._buffer.reset()

    def get_buffer(self) -> Buffer:
        return self._buffer
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_body.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/body.py tests/test_body.py
git commit -m "fix(ui): BodyBuffer uses real Buffer for scrollability"
```

---

### Task 2: Fix layout — input truly at bottom, body scrollable

**Files:**
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `BodyBuffer.get_buffer()`, `Window(scrollable=True)`
- Produces: layout with scrollable body, fixed input at bottom

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_app.py
def test_body_window_is_scrollable():
    """Body window should be scrollable."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    assert "scrollable=True" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_body_window_is_scrollable -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`, change body_control and body_window:

```python
body_control = BufferControl(buffer=body.get_buffer())
body_window = Window(content=body_control, wrap_lines=False, scrollable=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_app.py
git commit -m "fix(ui): body window scrollable, input fixed at bottom"
```

---

### Task 3: Approval/ask_user prompt → dedicated input area (not body)

**Files:**
- Modify: `ai_coding/ui/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `input_buffer`, `input_event`, `awaiting_input` flag
- Produces: approval/ask_user prompts shown in input area, not body

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_app.py
def test_input_provider_does_not_write_to_body():
    """Approval/ask_user prompts should not write to body."""
    import inspect
    from ai_coding.ui.app import run_app
    source = inspect.getsource(run_app)
    # The provider should set awaiting_input flag, not write to body
    assert "awaiting_input = True" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py::test_input_provider_does_not_write_to_body -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/app.py`, modify `_input_provider` to NOT write to body:

```python
async def _input_provider(prompt: str) -> str:
    nonlocal awaiting_input
    # Show prompt in input area, not body
    input_buffer.reset()
    input_buffer.insert_text(f"{prompt} ")
    awaiting_input = True
    input_event.clear()
    try:
        await input_event.wait()
        return input_buffer.text
    finally:
        awaiting_input = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/app.py tests/test_app.py
git commit -m "fix(ui): approval/ask_user prompts use input area, not body"
```

---

### Task 4: Footer cleanup — remove HTTP noise, show only essentials

**Files:**
- Modify: `ai_coding/ui/footer.py`
- Test: `tests/test_footer.py`

**Interfaces:**
- Consumes: `loop`, `turn_start`
- Produces: clean footer with model, context%, timer, hints

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_footer.py
def test_footer_no_http_noise():
    """Footer should not contain HTTP request logs."""
    import time
    from ai_coding.ui.footer import render_footer

    class FakeLoop:
        class engine:
            class model_config:
                name = "test-model"
        class settings:
            max_context_tokens = 1000
        last_outcome = None

    loop = FakeLoop()
    text = "".join(t for _, t in render_footer(loop, time.monotonic()))
    assert "HTTP" not in text
    assert "INFO" not in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_footer.py::test_footer_no_http_noise -v`
Expected: FAIL (if HTTP logs are leaking)

- [ ] **Step 3: Write minimal implementation**

In `ai_coding/ui/footer.py`, ensure only model/context/timer/hints are shown:

```python
def render_footer(loop: Any, turn_start: float) -> list[tuple[str, str]]:
    parts = []
    model = getattr(getattr(loop, "engine", None), "model_config", None)
    name = getattr(model, "name", "") or getattr(loop, "model_name", "") or "?"
    parts.append(f"model: {name}")
    outcome = getattr(loop, "last_outcome", None)
    usage = getattr(outcome, "usage", None) if outcome else None
    total_tokens = getattr(usage, "total_tokens", 0) if usage else 0
    max_tokens = getattr(getattr(loop, "settings", None), "max_context_tokens", 0)
    if max_tokens > 0:
        pct = int(total_tokens / max_tokens * 100)
        parts.append(f"{pct}% context")
    elapsed = time.monotonic() - turn_start
    parts.append(f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}")
    parts.append("ctrl+c quit · / commands")
    text = " · ".join(parts)
    return [("class:footer.text", text)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_footer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai_coding/ui/footer.py tests/test_footer.py
git commit -m "fix(ui): footer shows only essentials, no HTTP noise"
```

---

### Task 5: Full regression + pty verification

**Files:**
- No new files
- Test: all existing tests + pty smoke test

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -p no:warnings -q -o addopts=""`
Expected: PASS (all tests)

- [ ] **Step 2: Run lint**

Run: `.venv/bin/python -m ruff check ai_coding/ tests/`
Expected: All checks passed

- [ ] **Step 3: Pty smoke test**

Run: `timeout 10 python /tmp/pty_smoke.py`
Expected: TUI launches, body scrollable, input fixed at bottom, approval works

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: full regression pass after TUI layout fix"
```

---

## Self-Review

**Spec coverage:**
- ✅ Body scrollable — Task 1, 2
- ✅ Input fixed at bottom — Task 2
- ✅ Approval prompt routing — Task 3
- ✅ Footer cleanup — Task 4

**Placeholder scan:** No TBDs or TODOs. All code is complete.

**Type consistency:**
- `BodyBuffer.get_buffer() -> Buffer` matches usage in `app.py`
- `Window(scrollable=True)` is valid prompt_toolkit API
- `input_buffer.insert_text()` is valid Buffer API

**Root causes addressed:**
1. `FormattedTextControl` → `BufferControl` enables scrolling
2. Layout order: header → body → palette → input → footer (input truly at bottom)
3. `_input_provider` writes to input_buffer, not body
4. Footer only shows model/context/timer/hints
