# R6 实施计划（回合摘要环 / 失败升级 / ask_user / 记忆证据 / 索引不变量）

> **For agentic workers:** 本项目**在本会话内执行**（用户长期指令：不派子任务、用主模型实现）。
> 每个 Task 是一个提交边界：先写失败测试 → 跑到失败 → 最小实现 → 跑到通过 → 提交。
> 步骤用 `- [ ]` 打勾跟踪。**计划路径按用户约定为 `docs/r6-plan.md`**（覆盖技能默认的
> `docs/superpowers/plans/`）。

**Goal:** 让「压缩后的中段记忆」和「预算耗尽的回合」不再丢失——用 harness 自己派生的一行摘要与一份收尾交接，加上一次换策略提示、一个可被提问的人在环工具、一个记忆证据字段。

**Architecture:** 新增 `domain/turn_digest.py`（持久值对象 + 渲染，仿 `TaskState` 自带 `render()`）；`TurnOutcome` 增 `handoff`（harness 写的收尾报告，**不改 `text` 语义**）；`SessionData` 增 `turnCount` / `turnDigests` 有界环；易失尾部新增第四块 `digest`，**只在本轮历史被真正改写时注入**；`TurnPolicy` 增「连续失败 → 换策略」与「零成功 → 提前收尾」两档；新增 `security/user_prompt.py` + `tools/ask_user_tool.py`（阻塞式回调，答案即 `ToolResult`）；`MemoryNote` 增 `evidence` 元数据（只观测，不过滤）。

**Tech Stack:** Python 3.13（`.venv`，mypy 按 3.10 下限检查）、pydantic、pytest + pytest-asyncio、ruff、mypy --strict；无新依赖。

**Spec:** `docs/r6-analysis.md`（设计论证、实测数据、契约表、决策点）。执行者必须两份都读：本文给步骤，spec 给理由与边界。

## Global Constraints

- **TDD 强制**：每个 Task 先写测试、跑红、再实现、再跑绿。**除非测试自身写错，不得修改既有测试断言**（本计划明确标注的唯一例外见 Task 11 的 `source_turn` 说明——它不含既有断言的修改）。
- **质量门禁（每个 Task 结束都要过）**：`.venv/bin/python -m pytest tests/ -q` 全绿；`.venv/bin/python -m ruff check .` 0 错误；`.venv/bin/python -m mypy ai_coding` 0 错误。
- **分层**：`domain/` 不依赖 `core/`；`core/` 不反向依赖 `agent_loop`；工具不接触循环。
- **永不阻断回合**：摘要、记忆、轨迹、钩子的任何失败都只降级并可观测（`Degraded`）。
- **缓存纪律**：系统提示逐字节静态；记忆与摘要**只进易失尾部**；历史只在水位线以上被改写。
- **命名/风格**：模块 docstring 说明「为什么」并引用 `docs/r6-analysis.md` 小节；中文注释与既有代码一致；所有公开符号进 `__all__`。
- **锁定的决策值**（用户已按建议采纳 D-R6-1..14）：派生摘要、仅压缩回合注入、环 12 行 / 1200 字符、`TurnOutcome.handoff` 新字段、不新增 `TurnStatus`、`failed_streak≥2` / 换策略 1 次 / `stall_iterations=3`、`ask_user` 阻塞式且不给子智能体、`evidence` 只观测、旧会话 `turnCount` 不回溯、不给模型会话文件路径、本次不改 L2 占位、`ask_user` 默认 300 秒。
- **提交**：先文档后代码；提交信息含反引号时必须 `git commit -F <文件>`（shell 会吃掉 `-m` 里的反引号）。

---

### Task 1: 回合摘要的值对象与渲染（纯单元，不接线）

**Files:**
- Create: `ai_coding/domain/turn_digest.py`
- Test: `tests/test_turn_digest.py`

**Interfaces:**
- Consumes: `domain.loop.TurnOutcome`、`domain.message.ToolArtifact`、`domain.task.TaskState`
- Produces:
  - `TurnDigest(turn: int, request: str, outcome: str, status: str = "", files: tuple[str, ...] = (), open_items: int = 0)`，方法 `render() -> str`、`to_dict() -> dict`、静态 `from_dict(Any) -> TurnDigest | None`
  - `one_line(text: str, limit: int) -> str`
  - `build_digest(*, turn, user_input, outcome, artifacts=(), task=None, request_chars=60, outcome_chars=90) -> TurnDigest`
  - `append_digest(session: Any, digest: TurnDigest, *, keep: int) -> None`
  - `render_working_memory(digests, *, current_turn: int, max_chars: int = 1200, compacted: bool = True, note: str = WORKING_MEMORY_NOTE) -> str`
  - `render_handoff(*, outcome, task=None, failures=(), artifacts=(), trace_path: str = "", max_items: int = 5) -> str`
  - 常量 `WORKING_MEMORY_NOTE`、`DEFAULT_REQUEST_CHARS=60`、`DEFAULT_OUTCOME_CHARS=90`

- [ ] **Step 1: 写失败测试** `tests/test_turn_digest.py`

```python
"""Turn digests: one harness-derived line per turn, and the un-finished handoff (R6a)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolArtifact
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import (
    TurnDigest,
    append_digest,
    build_digest,
    one_line,
    render_handoff,
    render_working_memory,
)


def _outcome(status=TurnStatus.COMPLETED, text="改好了", reason="model finished", iterations=2):
    return TurnOutcome(status=status, text=text, iterations=iterations, reason=reason)


def _artifact(path: str) -> ToolArtifact:
    return ToolArtifact(path=path, kind="write", bytes=10, description="")


def _task() -> TaskState:
    task = TaskState(goal="让 make test 通过")
    task.add("创建 Makefile")
    task.add("修复 calc.py")
    task.update("1", status="completed")
    return task


def test_one_line_collapses_whitespace_and_cuts():
    out = one_line("第一行\n第二行   第三行" + "尾" * 200, 20)
    assert "\n" not in out
    assert len(out) <= 20
    assert out.endswith("…")


def test_digest_request_is_one_short_line():
    digest = build_digest(
        turn=3,
        user_input="请" + "很长" * 500,
        outcome=_outcome(),
        request_chars=60,
    )
    assert "\n" not in digest.request
    assert len(digest.request) <= 60


def test_digest_records_status_files_and_turn():
    digest = build_digest(
        turn=7,
        user_input="让 make test 通过",
        outcome=_outcome(),
        artifacts=[_artifact("Makefile"), _artifact("Makefile"), _artifact("src/calc.py")],
        task=_task(),
    )
    assert digest.turn == 7
    assert digest.status == "completed"
    assert digest.files == ("Makefile", "src/calc.py")  # 去重、保序
    assert digest.open_items == 1
    assert "Makefile" in digest.render()


def test_digest_falls_back_to_the_status_when_the_model_said_nothing():
    digest = build_digest(
        turn=5,
        user_input="继续",
        outcome=_outcome(
            status=TurnStatus.MAX_ITERATIONS, text=None, reason="iteration budget exhausted"
        ),
    )
    assert "迭代预算用尽" in digest.outcome
    assert digest.status == "max_iterations"


def test_append_digest_keeps_a_bounded_ring():
    from ai_coding.domain.session import SessionData

    session = SessionData(session_id="s", title="", created_time="", last_access_time="")
    for turn in range(1, 31):
        append_digest(
            session,
            build_digest(turn=turn, user_input=f"请求{turn}", outcome=_outcome()),
            keep=12,
        )
    assert len(session.turn_digests) == 12
    assert [d.turn for d in session.turn_digests] == list(range(19, 31))


def test_append_digest_survives_a_hostile_session():
    append_digest(object(), TurnDigest(turn=1, request="r", outcome="o"), keep=3)  # 不抛


def test_digest_survives_a_session_round_trip():
    from ai_coding.domain.session import SessionData

    session = SessionData(session_id="s", title="", created_time="", last_access_time="")
    session.turn_count = 4
    append_digest(
        session,
        build_digest(
            turn=4,
            user_input="改 calc",
            outcome=_outcome(),
            artifacts=[_artifact("src/calc.py")],
            task=_task(),
        ),
        keep=12,
    )
    data = session.to_dict()
    assert data["turnCount"] == 4
    assert data["turnDigests"][0]["turn"] == 4
    back = SessionData.from_dict(data)
    assert back.turn_count == 4
    assert back.turn_digests == session.turn_digests


def test_broken_digest_rows_are_ignored():
    from ai_coding.domain.session import SessionData

    data = {
        "sessionId": "s",
        "title": "",
        "createdTime": "",
        "lastAccessTime": "",
        "messages": [],
        "turnDigests": [{"turn": "x"}, {}, None, {"turn": 2, "request": "ok"}],
    }
    session = SessionData.from_dict(data)
    assert [d.turn for d in session.turn_digests] == [2]


def test_working_memory_is_empty_when_not_compacted():
    digests = [TurnDigest(turn=1, request="a", outcome="b")]
    assert render_working_memory(digests, current_turn=2, compacted=False) == ""
    assert render_working_memory([], current_turn=2) == ""


def test_working_memory_names_the_current_turn_and_stays_bounded():
    digests = [
        TurnDigest(turn=n, request=f"第{n}个请求" + "字" * 40, outcome=f"第{n}轮结果")
        for n in range(1, 21)
    ]
    block = render_working_memory(digests, current_turn=21, max_chars=600)
    assert block.startswith("<working-memory>")
    assert block.endswith("</working-memory>")
    assert "第 21 轮" in block
    assert len(block) <= 600
    assert "第20个请求" in block          # 最新的一定在
    assert "第1个请求" not in block       # 最旧的先被丢
    assert "memory_search" in block       # 指路，而不是让对方整读会话文件


def test_handoff_names_goal_open_items_and_failure():
    text = render_handoff(
        outcome=_outcome(
            status=TurnStatus.MAX_ITERATIONS, text=None, reason="iteration budget exhausted", iterations=5
        ),
        task=_task(),
        failures=[("bash", "nonzero_exit", "make: *** No rule to make target 'test'.")],
        artifacts=[_artifact("Makefile")],
        trace_path=".ai-coding/traces/abc/turn-1.jsonl",
    )
    assert "回合未完成" in text
    assert "目标: 让 make test 通过" in text
    assert "已完成: 1. 创建 Makefile" in text
    assert "未完成: 2. 修复 calc.py" in text
    assert "bash → nonzero_exit" in text
    assert "Makefile" in text
    assert "turn-1.jsonl" in text


def test_handoff_is_empty_when_the_model_spoke():
    assert render_handoff(outcome=_outcome()) == ""
    assert render_handoff(outcome=_outcome(text="   ")) == ""  # 空白不算说话
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_turn_digest.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_coding.domain.turn_digest'`

- [ ] **Step 3: 实现** `ai_coding/domain/turn_digest.py`

```python
"""What one turn leaves behind, and what to say when it did not finish (R6a).

Compaction is lossy in the middle (measured, docs/r6-analysis.md 1.1): at 60
turns the message list is untouched, yet all but the last three tool bodies become
``[Previous: used bash]``.  The head goal and the last twenty messages survive;
"how did I get here" does not.  One live run then spent three extra iterations and
grew prompt tokens from 3,363 to 6,363 digging through the 270 KB session file
(docs/r6-analysis.md 1.2).

``TurnDigest`` is the *harness-derived* answer: no extra model call, just the four
things the loop already knows when a turn ends — the user's request, the terminal
outcome, the artifacts it produced, and how many todo items are still open.  The
ring is rendered into a volatile tail block only when the history was actually
rewritten (``core.compaction.Compactor.did_compact``); in an ordinary turn the
recent conversation is still in the history, so the ring would be pure
duplication (docs/r6-analysis.md 2.1, D-R6-2).

``render_handoff`` is the other half: a turn that exhausts its budget with
``outcome.text is None`` used to hand the user an empty string (docs/r6-analysis.md
1.3).  The handoff is *harness-authored*, so it lives in ``TurnOutcome.handoff``
and never in ``text``: ``core.memory_turn`` reads ``text is None`` to decide
whether the turn concluded anything, and ``core.subagent`` reads ``text`` as the
model's own conclusion (D-R6-4).

Values live in ``domain`` — like ``TaskState``, which also owns its ``render()`` —
so nothing here depends on ``core``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolArtifact
from ai_coding.domain.task import TaskState

__all__ = [
    "DEFAULT_OUTCOME_CHARS",
    "DEFAULT_REQUEST_CHARS",
    "WORKING_MEMORY_NOTE",
    "TurnDigest",
    "append_digest",
    "build_digest",
    "one_line",
    "render_handoff",
    "render_working_memory",
]

DEFAULT_REQUEST_CHARS = 60
DEFAULT_OUTCOME_CHARS = 90
_ELLIPSIS = "…"
_MAX_FILES_SHOWN = 4
_WHITESPACE = re.compile(r"\s+")

WORKING_MEMORY_NOTE = "需要更早的细节时用 memory_search 查证，不要整读会话文件。"

_STATUS_LABEL: dict[TurnStatus, str] = {
    TurnStatus.MAX_ITERATIONS: "迭代预算用尽",
    TurnStatus.BUDGET: "token 预算用尽",
    TurnStatus.TIMEOUT: "回合超时",
    TurnStatus.ERROR: "引擎错误",
    TurnStatus.STOPPED: "用户中止",
    TurnStatus.ABORTED: "回合被取消",
}


def one_line(text: str, limit: int) -> str:
    """Collapse whitespace and cut to ``limit`` characters — never multi-line."""
    body = _WHITESPACE.sub(" ", str(text or "")).strip()
    if limit <= 0 or len(body) <= limit:
        return body
    return body[: max(1, limit - 1)].rstrip() + _ELLIPSIS


def _unique_paths(artifacts: Sequence[ToolArtifact]) -> tuple[str, ...]:
    seen: list[str] = []
    for artifact in artifacts:
        path = str(getattr(artifact, "path", "") or "")
        if path and path not in seen:
            seen.append(path)
    return tuple(seen)


@dataclass(frozen=True)
class TurnDigest:
    """One turn, in one line."""

    turn: int
    request: str
    outcome: str
    status: str = ""
    files: tuple[str, ...] = ()
    open_items: int = 0

    def render(self) -> str:
        label = f"t{self.turn}" if self.turn else "t?"
        line = f"- {label} {self.request or '(无请求)'}"
        if self.outcome:
            line += f" → {self.outcome}"
        extras: list[str] = []
        if self.files:
            extras.append("改了 " + ", ".join(self.files[:_MAX_FILES_SHOWN]))
        if self.status and self.status != TurnStatus.COMPLETED.value:
            extras.append(self.status)
        if extras:
            line += f"（{'；'.join(extras)}）"
        return line

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "turn": int(self.turn),
            "request": self.request,
            "outcome": self.outcome,
            "status": self.status,
        }
        if self.files:
            data["files"] = list(self.files)
        if self.open_items:
            data["openItems"] = int(self.open_items)
        return data

    @staticmethod
    def from_dict(data: Any) -> TurnDigest | None:
        """Parse one persisted row; ``None`` for anything unusable (untrusted input)."""
        if not isinstance(data, dict):
            return None
        try:
            turn = int(data.get("turn", 0) or 0)
        except (TypeError, ValueError):
            return None
        request = str(data.get("request", "") or "")
        if turn <= 0 and not request:
            return None
        raw_files = data.get("files")
        files = (
            tuple(str(item) for item in raw_files)
            if isinstance(raw_files, (list, tuple))
            else ()
        )
        try:
            open_items = int(data.get("openItems", 0) or 0)
        except (TypeError, ValueError):
            open_items = 0
        return TurnDigest(
            turn=turn,
            request=request,
            outcome=str(data.get("outcome", "") or ""),
            status=str(data.get("status", "") or ""),
            files=files,
            open_items=open_items,
        )


def build_digest(
    *,
    turn: int,
    user_input: str,
    outcome: TurnOutcome,
    artifacts: Sequence[ToolArtifact] = (),
    task: TaskState | None = None,
    request_chars: int = DEFAULT_REQUEST_CHARS,
    outcome_chars: int = DEFAULT_OUTCOME_CHARS,
) -> TurnDigest:
    """The one line this turn leaves in the session (harness-derived, no model call)."""
    spoken = one_line(outcome.text or "", outcome_chars)
    if not spoken:
        label = _STATUS_LABEL.get(outcome.status, outcome.status.value)
        reason = one_line(outcome.reason, outcome_chars)
        spoken = f"{label}：{reason}" if reason else label
    return TurnDigest(
        turn=int(turn),
        request=one_line(user_input, request_chars),
        outcome=spoken,
        status=outcome.status.value,
        files=_unique_paths(artifacts),
        open_items=len(task.open_items()) if task is not None else 0,
    )


def append_digest(session: Any, digest: TurnDigest, *, keep: int) -> None:
    """Append to the session's bounded ring (oldest first).  Never raises."""
    try:
        ring = list(getattr(session, "turn_digests", None) or [])
        ring.append(digest)
        limit = int(keep)
        session.turn_digests = ring[-limit:] if limit > 0 else ring
    except Exception:  # noqa: BLE001 — a digest must never break a turn
        return


def render_working_memory(
    digests: Sequence[TurnDigest],
    *,
    current_turn: int,
    max_chars: int = 1200,
    compacted: bool = True,
    note: str = WORKING_MEMORY_NOTE,
) -> str:
    """The volatile ``<working-memory>`` block, or ``""`` when it adds nothing.

    ``compacted`` is the loop's answer to "was this request's history rewritten?"
    (``Compactor.did_compact``).  Newest lines win; the newest one is kept even if
    it alone exceeds the budget, because dropping it would hide the most recent
    turn exactly when it matters.
    """
    if not compacted or max_chars <= 0:
        return ""
    lines = [d.render() for d in digests if (d.request or d.outcome)]
    if not lines:
        return ""
    header = f"当前: 第 {current_turn} 轮（更早的消息已被压缩，以下是每轮一行摘要）"
    overhead = len("<working-memory>\n\n</working-memory>") + len(header) + 1
    if note:
        overhead += len(note) + 1
    budget = max_chars - overhead
    chosen: list[str] = []
    used = 0
    for line in reversed(lines):
        if chosen and used + len(line) + 1 > budget:
            break
        chosen.append(line)
        used += len(line) + 1
    if not chosen:
        return ""
    chosen.reverse()
    body = [header, *chosen]
    if note:
        body.append(note)
    return "<working-memory>\n" + "\n".join(body) + "\n</working-memory>"


def render_handoff(
    *,
    outcome: TurnOutcome,
    task: TaskState | None = None,
    failures: Sequence[tuple[str, str, str]] = (),
    artifacts: Sequence[ToolArtifact] = (),
    trace_path: str = "",
    max_items: int = 5,
) -> str:
    """What the harness says when the model ran out of turn without a final word.

    Returns ``""`` when the model did speak: the handoff must never overwrite the
    model's own conclusion (docs/r6-analysis.md 2.1).
    """
    if (outcome.text or "").strip():
        return ""
    label = _STATUS_LABEL.get(outcome.status, outcome.status.value)
    lines = [f"[回合未完成 · {label}（{outcome.iterations} 轮）]"]
    if outcome.reason:
        lines.append(f"原因: {one_line(outcome.reason, 160)}")
    if task is not None and task.goal:
        lines.append(f"目标: {one_line(task.goal, 160)}")
    if task is not None and task.items:
        done = [item for item in task.items if item.is_terminal()]
        if done:
            lines.append(
                "已完成: "
                + "；".join(f"{item.id}. {one_line(item.content, 60)}" for item in done[:max_items])
            )
        open_items = task.open_items()
        if open_items:
            lines.append(
                "未完成: "
                + "；".join(
                    f"{item.id}. {one_line(item.content, 60)}" for item in open_items[:max_items]
                )
            )
    if failures:
        tool, reason, preview = failures[-1]
        tail = f"（{one_line(preview, 80)}）" if preview else ""
        lines.append(f"最近一次失败: {tool} → {reason}{tail}")
    paths = _unique_paths(artifacts)
    if paths:
        lines.append("产物: " + ", ".join(paths[:_MAX_FILES_SHOWN]))
    if trace_path:
        lines.append(f"轨迹: {trace_path}")
    return "\n".join(lines)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_turn_digest.py -q`
Expected: PASS（12 passed）

- [ ] **Step 5: 门禁 + 提交**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
git add ai_coding/domain/turn_digest.py tests/test_turn_digest.py
git commit -m "R6a-1: a turn's one line, derived by the harness not the model"
```

---

### Task 2: 把摘要环与会话一起持久化

**Files:**
- Modify: `ai_coding/domain/session.py`

**Interfaces:**
- Produces: `SessionData.turn_count: int`（JSON `turnCount`）、`SessionData.turn_digests: list[TurnDigest]`（JSON `turnDigests`，空则不写，沿用 `task` 的「不写噪声」惯例）
- Consumes: `domain.turn_digest.TurnDigest`

- [ ] **Step 1–4: 测试已在 Task 1 覆盖**

`test_digest_survives_a_session_round_trip` 与 `test_broken_digest_rows_are_ignored`
在 Task 1 里已经写好并**当前是红的**（`SessionData` 还没有这两个字段）。本 Task 只做实现。

Run: `.venv/bin/python -m pytest tests/test_turn_digest.py -q`
Expected: FAIL — `AttributeError: 'SessionData' object has no attribute 'turn_count'`

- [ ] **Step 5: 实现**：`session.py` 的改动

```python
from ai_coding.domain.turn_digest import TurnDigest

@dataclass
class SessionData:
    """A persisted conversation session."""

    session_id: str
    title: str
    created_time: str
    last_access_time: str
    messages: list[ChatMessage] = field(default_factory=list)
    task: TaskState | None = None
    # R3b: how many messages have already been harvested into memory. ...
    harvested_messages: int = 0
    # R6a: the session-level turn counter (1-based) and the bounded ring of
    # one-line digests the volatile <working-memory> block is rendered from.  The
    # counter exists so `source_turn` and the digest header can say "turn N"
    # instead of "message count" (docs/r6-analysis.md 2.4).
    turn_count: int = 0
    turn_digests: list[TurnDigest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {...}          # 现有四个键不动
        # ... 现有 task / harvestedMessages 逻辑不动
        if self.turn_count:
            data["turnCount"] = int(self.turn_count)
        if self.turn_digests:
            data["turnDigests"] = [digest.to_dict() for digest in self.turn_digests]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionData:
        raw_digests = data.get("turnDigests")
        rows = raw_digests if isinstance(raw_digests, list) else []
        digests = [digest for digest in (TurnDigest.from_dict(row) for row in rows) if digest]
        return cls(
            ...,
            turn_count=int(data.get("turnCount", 0) or 0),
            turn_digests=digests,
        )
```

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_turn_digest.py tests/test_domain_session.py -q`
Expected: PASS（既有 5 条 session 测试**一条都不改**且仍然绿）

- [ ] **Step 7: 提交**

```bash
git add ai_coding/domain/session.py
git commit -m "R6a-2: the digest ring rides along with the session file"
```

---

### Task 3: 循环接线——压缩回合才注入摘要环，空回合改成交接

**Files:**
- Modify: `ai_coding/domain/loop.py`（`TurnOutcome.handoff`）
- Modify: `ai_coding/core/compaction.py`（`Compactor.did_compact`）
- Modify: `ai_coding/core/agent_loop.py`
- Modify: `ai_coding/config/models.py`（`working_memory_keep` / `working_memory_chars`）
- Modify: `ai_coding/core/subagent.py`（`text or handoff`）
- Test: `tests/test_loop_digest.py`

**Interfaces:**
- Produces: `TurnOutcome.handoff: str = ""`；`Compactor.did_compact(source, result) -> bool`；
  `AgentLoop.process_input` 返回 `outcome.text or outcome.handoff`；尾部块名 `"digest"`
- Consumes: Task 1 的 `build_digest` / `append_digest` / `render_working_memory` / `render_handoff`

- [ ] **Step 1: 写失败测试** `tests/test_loop_digest.py`

```python
"""Loop wiring for R6a: the digest ring, the tail block, and the handoff."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ChatMessage, ToolCallRef
from ai_coding.domain.session import SessionData
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry
from ai_coding.domain.message import ToolResult


class _Engine(ChatEngine):
    """Always asks for the same tool, so the turn can only end on budget."""

    def __init__(self, tool_name: str | None = "poke") -> None:
        self.tool_name = tool_name
        self.requests: list[list[ChatMessage]] = []
        self.instructions: list[str | None] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        self.instructions.append(instructions)
        if self.tool_name is None:
            return StepResult(text="做完了", tool_calls=[], finish_reason="stop")
        return StepResult(
            text=None,
            tool_calls=[ToolCallRef(id=f"c{len(self.requests)}", name=self.tool_name, arguments="{}")],
            finish_reason="tool_calls",
        )


class _Poke(BaseTool):
    name = "poke"

    @property
    def description(self) -> str:
        return "poke"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="poked")


class _Sessions:
    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)


class _Compacting:
    """A stand-in context manager: rewrites (or does not) on demand."""

    def __init__(self, rewrite: bool) -> None:
        self.rewrite = rewrite

    async def compress_if_needed(self, messages, *, force=False, on_summary=None):
        if not self.rewrite:
            return messages
        return [*messages, ChatMessage(role="user", content="[compacted]", timestamp="")]


def _loop(engine: ChatEngine, context: Any = None, **overrides: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Poke())
    settings = AIConfig(**overrides)
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        settings=settings,
        context=context,
        workspace=".",
    )


def _session() -> SessionData:
    return SessionData(session_id="s1", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_each_turn_appends_one_digest():
    loop = _loop(_Engine(tool_name=None))
    session = _session()
    await loop.process_input(session, "第一件事")
    await loop.process_input(session, "第二件事")
    assert [d.turn for d in session.turn_digests] == [1, 2]
    assert session.turn_count == 2
    assert "第一件事" in session.turn_digests[0].request


@pytest.mark.asyncio
async def test_digests_are_persisted_with_the_session():
    sessions = _Sessions()
    registry = ToolRegistry()
    registry.register(_Poke())
    loop = AgentLoop(_Engine(tool_name=None), sessions, registry, settings=AIConfig())
    await loop.process_input(_session(), "写点什么")
    assert sessions.saved[-1].turn_digests, "the ring must reach the session file"


@pytest.mark.asyncio
async def test_budget_exhaustion_returns_the_handoff_not_an_empty_string():
    loop = _loop(_Engine(), max_tool_iterations=2)
    session = _session()
    answer = await loop.process_input(session, "让 make test 通过")
    assert answer, "the user must never get an empty string"
    assert "[回合未完成" in answer
    assert "让 make test 通过" in answer
    assert loop.last_outcome is not None
    assert loop.last_outcome.text is None      # 模型确实没说话
    assert loop.last_outcome.handoff == answer  # 交接在 outcome 上，不在 text 上
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_working_memory_is_injected_only_when_history_was_compacted():
    plain_engine = _Engine(tool_name=None)
    plain = _loop(plain_engine, _Compacting(rewrite=False))
    plain_session = _session()
    await plain.process_input(plain_session, "一")
    await plain.process_input(plain_session, "二")

    compact_engine = _Engine(tool_name=None)
    compacting = _loop(compact_engine, _Compacting(rewrite=True))
    compact_session = _session()
    await compacting.process_input(compact_session, "一")
    await compacting.process_input(compact_session, "二")

    def last_request_text(engine: _Engine) -> str:
        return "\n".join(message.content or "" for message in engine.requests[-1])

    assert "<working-memory>" not in last_request_text(plain_engine)
    assert "<working-memory>" in last_request_text(compact_engine)


@pytest.mark.asyncio
async def test_working_memory_is_a_tail_block_never_the_system_prompt():
    engine = _Engine(tool_name=None)
    loop = _loop(engine, _Compacting(rewrite=True))
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    request = engine.requests[-1]
    assert "<working-memory>" in (request[-1].content or "")
    assert all("<working-memory>" not in (item or "") for item in engine.instructions)
    assert "<working-memory>" not in (engine.instructions[-1] or "")


@pytest.mark.asyncio
async def test_a_completed_turn_keeps_text_and_leaves_handoff_empty():
    loop = _loop(_Engine(tool_name=None))
    answer = await loop.process_input(_session(), "打个招呼")
    assert answer == "做完了"
    assert loop.last_outcome is not None
    assert loop.last_outcome.text == "做完了"
    assert loop.last_outcome.handoff == ""
```

- [ ] **Step 2: 跑到失败**

Run: `.venv/bin/python -m pytest tests/test_loop_digest.py -q`
Expected: FAIL — `TurnOutcome` 没有 `handoff`

- [ ] **Step 3: 实现**

`domain/loop.py`：

```python
@dataclass(frozen=True)
class TurnOutcome:
    status: TurnStatus
    text: str | None = None
    iterations: int = 0
    reason: str = ""
    usage: dict[str, int] | None = None
    # R6a: the harness's own report for a turn that ended without a final word
    # (budget/clock/engine).  Deliberately *not* folded into ``text``: memory reads
    # ``text is None`` as "this turn concluded nothing", and a sub-agent's
    # conclusion is the model's own words (docs/r6-analysis.md 2.1, D-R6-4).
    handoff: str = ""
```

`core/compaction.py`：

```python
from collections.abc import Callable, Sequence

    @staticmethod
    def did_compact(source: Sequence[ChatMessage], result: Sequence[ChatMessage]) -> bool:
        """True when ``compress`` actually rewrote the history.

        ``compress`` hands back the caller's own list when it does nothing (no
        context, below the water mark, degraded fallback) and a fresh list when it
        ran.  That identity is the loop's signal for "is the working-memory ring
        worth its tokens?" (docs/r6-analysis.md 2.1, D-R6-2).
        """
        return result is not source
```

`config/models.py`（`AIConfig` 末尾，紧跟 `turn_token_budget`）：

```python
    # R6a: the volatile <working-memory> ring — 12 one-line turn digests, rendered
    # into 1200 characters (measured ≈230 estimated tokens, 0.5% of the budget).
    # Injected only when this request's history was actually rewritten.
    working_memory_keep: int = 12
    working_memory_chars: int = 1200
```

`core/agent_loop.py`（改动点逐条）：

```python
from dataclasses import replace
from ai_coding.domain.turn_digest import (
    append_digest,
    build_digest,
    render_handoff,
    render_working_memory,
)

    async def process_input(self, session, user_input, on_delta=None, observer=None) -> str:
        ...
        session.add_message(user_message(user_input, now_utc()))
        session.turn_count = int(getattr(session, "turn_count", 0) or 0) + 1
        turn_number = session.turn_count
        ...
                compacted = Compactor.did_compact(session.messages, history)
                digest_block = render_working_memory(
                    session.turn_digests,
                    current_turn=turn_number,
                    max_chars=int(getattr(self.settings, "working_memory_chars", 1200)),
                    compacted=compacted,
                )
                tail, tail_blocks = build_tail(
                    [
                        _volatile("memory", memory_block),
                        _volatile("task", _content_of(injection.message)),
                        _volatile("hook", hook_context),
                        _volatile("digest", digest_block),
                    ]
                )
        ...                                        # 循环结束后、_finalize 之前：
        if not outcome.text:
            handoff = render_handoff(
                outcome=outcome,
                task=task,
                artifacts=self.last_artifacts,
                trace_path=self._trace_path(session),
            )
            if handoff:
                outcome = replace(outcome, handoff=handoff)
        append_digest(
            session,
            build_digest(
                turn=turn_number,
                user_input=user_input,
                outcome=outcome,
                artifacts=self.last_artifacts,
                task=task,
            ),
            keep=int(getattr(self.settings, "working_memory_keep", 12)),
        )
        self._finalize(session, outcome)
        await self.turn_memory.harvest(...)
        await self._fire_hooks(HookPoint.TURN_END, session)
        return outcome.text or outcome.handoff

    def _trace_path(self, session: SessionData) -> str:
        """Where this turn's trace lives, for the handoff (best effort)."""
        path = getattr(self.trace, "current_path", None)
        return str(path) if path else ""
```

`core/subagent.py`：

```python
        conclusion = _truncate(
            (outcome.text or outcome.handoff) if outcome else None, limit
        )
```

- [ ] **Step 4: 跑到通过**

Run: `.venv/bin/python -m pytest tests/test_loop_digest.py tests/test_agent_loop_react.py tests/test_agent_loop_memory.py tests/test_agent_loop_r4_wiring.py -q`
Expected: PASS（既有循环测试**一条都不改**）

- [ ] **Step 5: 门禁 + 提交**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
git add -A
git commit -F - <<'MSG'
R6a-3: an exhausted turn reports, and a compacted one carries its own digest
MSG
```

---

### Task 4: 真实案例 R1——「预算耗尽」不再返回空字符串

**Files:**
- Create: `scripts/r6_cases.py`（先放 R1 一条真实案例与 8 条离线案例的骨架）

**Interfaces:**
- Consumes: `AgentLoop.process_input`、`_build_tool_registry`（`ai_coding.cli`）
- Produces: `scripts/r6_cases.py --real --config config.local.yaml`（可重复运行；失败只报告不阻断）

- [ ] **Step 1: 写脚本骨架与 R1**

```python
#!/usr/bin/env python3
"""R6 acceptance cases — offline by default, `--real` adds the live ones.

The offline cases replay docs/r6-analysis.md section 1 with fakes: the digest ring,
the compaction-triggered injection, the handoff, the escalation ladder, ask_user
with and without a human, the evidence metadata and the index invariants.

The live cases are the same experiments re-run against a real model; they report
instead of asserting, except where the evidence is structural (an empty answer, a
bash call that reads the session file).
"""
```

- [ ] **Step 2–5**: 见 Task 13 的完整脚本（本 Task 只加 `--real` 的 R1 分支并跑一次，
  确认它输出 `PASS  R1 ...`：`make test` 那种工作区里 `max_tool_iterations=2`，
  断言返回值非空且含「未完成」）。

```bash
git add scripts/r6_cases.py
git commit -m "R6a-4: the live case that used to print nothing now prints a handoff"
```

---

### Task 5: 失败升级的纯决策（`TurnPolicy`）

**Files:**
- Modify: `ai_coding/core/turn_policy.py`
- Modify: `ai_coding/config/models.py`
- Test: `tests/test_turn_policy_escalation.py`

**Interfaces:**
- Produces: `TurnSnapshot.failed_streak/attempted_calls/successful_calls/strategy_nudges`（带默认值）；
  `TurnPolicyConfig.failed_streak_threshold=2` / `max_strategy_nudges=1` / `stall_iterations=3`；
  `TurnPolicy.strategy_message(failed_streak) -> str`；常量 `STRATEGY_GATE_LEAD = "[strategy-gate]"`

- [ ] **Step 1: 写失败测试** `tests/test_turn_policy_escalation.py`

```python
"""Escalation in the pure policy (R6b): change strategy, then stop and report."""

from __future__ import annotations

from ai_coding.core.turn_policy import (
    TurnAction,
    TurnPolicy,
    TurnPolicyConfig,
    TurnSnapshot,
)


def _policy(**overrides):
    return TurnPolicy(TurnPolicyConfig(max_iterations=10, **overrides))


def test_repeated_failures_ask_for_a_strategy_change():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=3, failed_streak=2, successful_calls=3)
    )
    assert decision.action is TurnAction.NUDGE
    assert decision.reason == "repeated tool failures"
    assert "[strategy-gate]" in (decision.inject or "")
    assert "ask_user" in (decision.inject or "")


def test_the_strategy_nudge_is_given_once_per_turn():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=4, failed_streak=5, successful_calls=1, strategy_nudges=1)
    )
    assert decision.action is TurnAction.ITERATE


def test_a_turn_with_no_success_stops_before_the_iteration_budget():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(
            iteration=4, failed_streak=3, attempted_calls=3, successful_calls=0,
            strategy_nudges=1,
        )
    )
    assert decision.action is TurnAction.FINISH
    assert "no progress" in decision.reason


def test_a_successful_call_disarms_the_stall_stop():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(
            iteration=4, failed_streak=1, attempted_calls=3, successful_calls=2,
            strategy_nudges=1,
        )
    )
    assert decision.action is TurnAction.ITERATE


def test_a_turn_that_never_calls_a_tool_is_not_a_stall():
    # 只说话不调工具的回合归停止策略管（strict 会一直提醒），不是「零进展」
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=5, attempted_calls=0, successful_calls=0)
    )
    assert decision.action is TurnAction.ITERATE


def test_budget_still_wins_over_escalation():
    decision = _policy().decide_next_iteration(
        TurnSnapshot(iteration=4, failed_streak=9, attempted_calls=9, successful_calls=0),
        budget_expired=True,
    )
    assert decision.action is TurnAction.FINISH
    assert decision.reason == "turn deadline exceeded"


def test_escalation_is_off_when_the_thresholds_are_zero():
    policy = _policy(failed_streak_threshold=0, stall_iterations=0)
    for iteration in (2, 5, 9):
        decision = policy.decide_next_iteration(
            TurnSnapshot(
                iteration=iteration, failed_streak=9, attempted_calls=9,
                successful_calls=0,
            )
        )
        assert decision.action is TurnAction.ITERATE


def test_from_ai_config_reads_the_new_keys():
    settings = type(
        "S",
        (),
        {
            "max_tool_iterations": 4,
            "failed_streak_threshold": 3,
            "max_strategy_nudges": 2,
            "stall_iterations": 5,
        },
    )()
    config = TurnPolicyConfig.from_ai_config(settings)
    assert (config.failed_streak_threshold, config.max_strategy_nudges, config.stall_iterations) == (3, 2, 5)
```

- [ ] **Step 2: 跑到失败** → `FAIL：unexpected keyword argument 'failed_streak'`
- [ ] **Step 3: 实现**（见 spec §2.2 的代码块：四个快照字段、三个配置字段、`from_ai_config` 读取、`decide_next_iteration` 的判定优先级「预算 → 轮次 → stall → 换策略 → ITERATE」、`strategy_message`）。**stall 的判据必须是 `attempted_calls > 0 and successful_calls == 0`**：只看 `successful_calls == 0` 会被 `test_strict_policy_ignores_the_nudge_cap` 抓住——只说话不调工具的回合不是「零进展」。
- [ ] **Step 4: 跑到通过**

Run: `.venv/bin/python -m pytest tests/test_turn_policy_escalation.py tests/test_turn_policy.py -q`
Expected: PASS（既有 19 条表测一条不改）

- [ ] **Step 5: 提交**

```bash
git add ai_coding/core/turn_policy.py ai_coding/config/models.py tests/test_turn_policy_escalation.py
git commit -m "R6b-1: change strategy on repeated failures, stop on zero progress"
```

---

### Task 6: 循环接线——失败计数与换策略提示

**Files:**
- Modify: `ai_coding/core/agent_loop.py`
- Test: `tests/test_loop_escalation.py`

**Interfaces:**
- Consumes: Task 5 的 `TurnSnapshot` 字段与 `TurnAction.NUDGE`
- Produces: 每回合至多一条 `[strategy-gate]` 用户消息（**持久化**，与 `[task-gate]` 同款，理由见 `docs/r1a-4-analysis.md` D1）

- [ ] **Step 1: 写失败测试** `tests/test_loop_escalation.py`

```python
"""The loop's half of R6b: count failures, nudge once, stop when nothing works."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult, TurnStatus
from ai_coding.domain.message import ToolCallRef, ToolResult
from ai_coding.domain.session import SessionData
from ai_coding.tools.base import BaseTool, ToolContext
from ai_coding.tools.registry import ToolRegistry


class _Engine(ChatEngine):
    """Always asks for the same tool, so only the policy can end the turn."""

    def __init__(self, ask_for_tools: bool = True) -> None:
        self.ask_for_tools = ask_for_tools
        self.calls = 0

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.calls += 1
        if not self.ask_for_tools:
            return StepResult(text="我直接回答", tool_calls=[], finish_reason="stop")
        return StepResult(
            text=None,
            tool_calls=[ToolCallRef(id=f"c{self.calls}", name="flaky", arguments="{}")],
            finish_reason="tool_calls",
        )


class _Flaky(BaseTool):
    """Fails the first ``failures`` calls, then succeeds."""

    name = "flaky"
    parallel_safe = True

    def __init__(self, failures: int = 99) -> None:
        self.failures = failures
        self.seen = 0

    @property
    def description(self) -> str:
        return "a tool that fails"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        self.seen += 1
        if self.seen <= self.failures:
            return ToolResult(success=False, output="boom", error="nonzero_exit")
        return ToolResult(success=True, output="ok")


class _Sessions:
    def save(self, session: SessionData) -> None:
        return None


def _loop(tool: BaseTool, **overrides: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(tool)
    return AgentLoop(
        _Engine(), _Sessions(), registry, settings=AIConfig(**overrides), workspace="."
    )


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


def _strategy_gates(session: SessionData) -> list[str]:
    return [
        message.content or ""
        for message in session.messages
        if message.role == "user" and "[strategy-gate]" in (message.content or "")
    ]


@pytest.mark.asyncio
async def test_failed_iterations_produce_one_strategy_nudge_in_the_session():
    loop = _loop(
        _Flaky(), max_tool_iterations=6, stall_iterations=0, failed_streak_threshold=2
    )
    session = _session()
    await loop.process_input(session, "修好它")
    assert len(_strategy_gates(session)) == 1
    assert loop.last_outcome is not None
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_a_stalled_turn_finishes_with_a_handoff():
    loop = _loop(_Flaky(), max_tool_iterations=8, stall_iterations=3)
    session = _session()
    answer = await loop.process_input(session, "修好它")
    assert loop.last_outcome is not None
    assert "no progress" in loop.last_outcome.reason
    assert loop.last_outcome.iterations < 8
    assert "[回合未完成" in answer
    assert "最近一次失败: flaky → nonzero_exit" in answer


@pytest.mark.asyncio
async def test_successful_tool_calls_count_towards_progress():
    loop = _loop(_Flaky(failures=1), max_tool_iterations=8, stall_iterations=3)
    session = _session()
    await loop.process_input(session, "修好它")
    assert loop.last_outcome is not None
    assert "no progress" not in loop.last_outcome.reason
    assert loop.last_outcome.status is TurnStatus.MAX_ITERATIONS


@pytest.mark.asyncio
async def test_escalation_never_fires_without_tool_calls():
    registry = ToolRegistry()
    registry.register(_Flaky())
    loop = AgentLoop(
        _Engine(ask_for_tools=False),
        _Sessions(),
        registry,
        settings=AIConfig(max_tool_iterations=4),
        workspace=".",
    )
    session = _session()
    answer = await loop.process_input(session, "打个招呼")
    assert answer == "我直接回答"
    assert _strategy_gates(session) == []
    assert loop.last_outcome is not None and loop.last_outcome.handoff == ""
```

- [ ] **Step 2–4: 实现与验证**

```python
        # process_input 的回合状态
        failures: list[tuple[str, str, str]] = []
        attempted_calls = 0
        successful_calls = 0
        failed_streak = 0
        strategy_nudges = 0

        # _iteration_decision(...) 的新参数，写进 TurnSnapshot
        # 判定为 NUDGE 时（换策略），与 todo 门禁同款：
        if decision.action is TurnAction.NUDGE:
            strategy_nudges += 1
            session.add_message(user_message(decision.inject or "", now_utc()))
            self._checkpoint(session, index)
            continue

        # _run_tools 改为返回 outcomes，调用方计数（无测试直接调用私有方法，已核验）
        outcomes = await self._run_tools(...)
        successes = [item for item in outcomes if item.result.success]
        attempted_calls += len(outcomes)
        successful_calls += len(successes)
        failed_streak = 0 if successes else (failed_streak + 1 if outcomes else failed_streak)
        failures.extend(
            (item.call.name, str(item.result.error or "failed"), (item.result.output or "")[:160])
            for item in outcomes
            if not item.result.success
        )
        if any(item.halted for item in outcomes):
            outcome = _stopped_outcome(step.text, index)
            break
```

Run: `.venv/bin/python -m pytest tests/test_loop_escalation.py -q` → PASS

- [ ] **Step 5: 门禁 + 提交**

```bash
git add -A && git commit -m "R6b-2: the loop counts failures and injects the strategy gate once"
```

---

### Task 7: 真实案例 R2——压缩后的中段问题不再靠「整读会话文件」

**Files:**
- Modify: `scripts/r6_cases.py`（加 R2 分支）

**Interfaces:**
- Consumes: Task 1–6 的全部产物、`config.local.yaml`
- Produces: 一条真实案例：合成 291 条已压缩会话（同 spec §1.2），询问中段决策；判据是**结构化事实**
  ——（a）回答含 `uv`；（b）本回合 trace 里**没有**读取 `sessions/*.json` 的 bash 调用；（c）首步
  `prompt_tokens < 5000`。

```bash
git add scripts/r6_cases.py
git commit -m "R6b-3: the live case that used to dig through the session file"
```

---

### Task 8: 提问通道（`security/user_prompt.py`）

**Files:**
- Create: `ai_coding/security/user_prompt.py`
- Test: `tests/test_user_prompt.py`

**Interfaces:**
- Produces: `UserAnswer(answered: bool, text: str = "", reason: str = "")`；
  `UserPromptCallback`（`async ask(question, options=()) -> UserAnswer`）；
  `CLIUserPromptCallback(input_fn=None)`（`asyncio.to_thread` 里读一行，EOF/OSError → `reason="eof"`）；
  `UnavailableUserPromptCallback()`（`reason="no_user"`，永不阻塞）

- [ ] **Step 1: 测试**（3 条）

```python
@pytest.mark.asyncio
async def test_cli_callback_returns_the_typed_answer():
    prompts: list[str] = []
    callback = CLIUserPromptCallback(input_fn=lambda prompt: prompts.append(prompt) or "改 A")
    answer = await callback.ask("改 A 还是 B？", options=["A", "B"])
    assert answer.answered is True
    assert answer.text == "改 A"
    assert "改 A 还是 B？" in prompts[0] and "A" in prompts[0]

@pytest.mark.asyncio
async def test_eof_is_unavailable_not_a_crash():
    def boom(_prompt: str) -> str:
        raise EOFError
    answer = await CLIUserPromptCallback(input_fn=boom).ask("在吗？")
    assert answer.answered is False and answer.reason == "eof"

@pytest.mark.asyncio
async def test_unavailable_callback_never_blocks():
    answer = await UnavailableUserPromptCallback().ask("在吗？")
    assert answer.answered is False and answer.reason == "no_user"
```

- [ ] **Step 2–5**: 实现（模块 docstring 说明「与 `CLIApprovalCallback` 的区别：主动提问、
  且等待走线程池所以能被回合上限打断」，见 spec §2.3）→ 跑绿 → 提交。

```bash
git add ai_coding/security/user_prompt.py tests/test_user_prompt.py
git commit -m "R6c-1: a question channel that a turn clock can interrupt"
```

---

### Task 9: `ask_user` 工具

**Files:**
- Create: `ai_coding/tools/ask_user_tool.py`
- Test: `tests/test_tool_ask_user.py`

**Interfaces:**
- Consumes: Task 8 的 `UserPromptCallback` / `UserAnswer`
- Produces: `AskUserTool(prompt, timeout_seconds=300.0)`，`name="ask_user"`，`parallel_safe=False`，
  `max_duration_seconds == timeout + 15`；结果三态：
  `answered=True` → `success=True, data={"answered": True, "answer": ...}`；
  `reason="no_user"` → `success=False, error="no_user"`，output 教模型「写清假设后收尾」；
  `reason="eof"/"timeout"` → `success=False, error="user_timeout"`

- [ ] **Step 1: 写失败测试** `tests/test_tool_ask_user.py`

```python
"""ask_user: a blocking question whose absence of a human is an answer (R6c)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ai_coding.security.user_prompt import (
    UnavailableUserPromptCallback,
    UserAnswer,
    UserPromptCallback,
)
from ai_coding.tools.ask_user_tool import AskUserTool


class _FixedPrompt:
    """A scripted human that records what it was asked."""

    def __init__(self, answer: UserAnswer) -> None:
        self.answer = answer
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        self.seen.append((question, tuple(options)))
        return self.answer


class _Boom:
    """A prompt channel that blows up (no tty, closed stdin, …)."""

    async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
        raise RuntimeError("no tty")


def _tool(prompt: UserPromptCallback, **kwargs: Any) -> AskUserTool:
    return AskUserTool(prompt, **kwargs)


@pytest.mark.asyncio
async def test_answered_question_is_structured_data():
    prompt = _FixedPrompt(UserAnswer(answered=True, text="改 A"))
    result = await _tool(prompt).execute(
        {"question": "改 A 还是 B？", "options": ["A", "B"]}
    )
    assert result.success is True
    assert result.data["answered"] is True
    assert result.data["answer"] == "改 A"
    assert "改 A" in result.output
    assert prompt.seen == [("改 A 还是 B？", ("A", "B"))]  # options 传到了通道


@pytest.mark.asyncio
async def test_no_human_tells_the_model_to_decide():
    result = await _tool(UnavailableUserPromptCallback()).execute({"question": "改哪个？"})
    assert result.success is False
    assert result.error == "no_user"
    assert "假设" in result.output  # 教模型写清假设后自己收尾


@pytest.mark.asyncio
async def test_callback_failure_is_a_tool_failure():
    result = await _tool(_Boom()).execute({"question": "在吗？"})
    assert result.success is False
    assert result.error == "user_prompt_error"


@pytest.mark.asyncio
async def test_missing_question_is_rejected():
    prompt = _FixedPrompt(UserAnswer(answered=True, text="x"))
    result = await _tool(prompt).execute({"question": "   "})
    assert result.success is False
    assert result.error == "missing_question"
    assert prompt.seen == []  # 没问就不该打扰人


def test_the_tool_is_not_parallel_safe():
    assert _tool(UnavailableUserPromptCallback()).parallel_safe is False


def test_the_tool_declares_its_wait_bound():
    tool = _tool(UnavailableUserPromptCallback(), timeout_seconds=30.0)
    assert tool.max_duration_seconds == 45.0  # timeout + 15s 收割余量，与 bash/subAgent 同款
```

> 顶部还需要 `from typing import Any`。

- [ ] **Step 2–5**: 实现（`max_duration_seconds` 用 `timeout + 15`，与 `bash`/`subAgent` 同款
  「自报上限」，分发器取 `min(自报, 回合剩余)`）→ 跑绿 → 提交。

```bash
git add ai_coding/tools/ask_user_tool.py tests/test_tool_ask_user.py
git commit -m "R6c-2: ask_user, a tool whose absence of a human is a first-class answer"
```

---

### Task 10: 装配与 CLI 接线（有人的地方才注册）

**Files:**
- Modify: `ai_coding/tools/builder.py`
- Modify: `ai_coding/config/models.py`（`ToolsConfig.ask_user`）
- Modify: `ai_coding/cli.py`（`repl` 传 `CLIUserPromptCallback()`；`ask` 不传）
- Test: `tests/test_builder_ask_user.py`

**Interfaces:**
- Produces: `build_tool_registry(app_cfg, workspace, skills_path=None, *, include_subagent=True, user_prompt=None)`；
  `ToolsConfig.ask_user: ToolConfig`（默认 `timeout_seconds=300`）
- 规则：`include_subagent and tools.ask_user.enabled and user_prompt is not None` 三者同时成立才注册；
  `build_subagent_runner` 的 `build_tools` 传 `user_prompt=None`

- [ ] **Step 1: 测试**（3 条）

```python
def test_ask_user_is_registered_when_a_human_is_available():
    registry = build_tool_registry(_cfg(), ".", None, user_prompt=UnavailableUserPromptCallback())
    assert registry.get("ask_user") is not None

def test_ask_user_is_absent_without_a_human():
    registry = build_tool_registry(_cfg(), ".", None)
    assert registry.get("ask_user") is None

def test_subagent_stacks_never_get_ask_user():
    registry = build_tool_registry(
        _cfg(), ".", None, include_subagent=False, user_prompt=UnavailableUserPromptCallback()
    )
    assert registry.get("ask_user") is None
```

- [ ] **Step 2–5**: 实现 + 真 REPL 手工验收（spec §10.6 承认这是证据最弱的一环：
  在 `repl` 里让模型提问一次，确认问题打印、能输入、答案被用上）→ 提交。

```bash
git add -A && git commit -m "R6c-3: the CLI hands ask_user a human, the sub-agents get none"
```

---

### Task 11: 记忆证据元数据 + `source_turn` 修正

**Files:**
- Modify: `ai_coding/memory/note.py`、`ai_coding/memory/service.py`、`ai_coding/core/memory_turn.py`、`ai_coding/core/events.py`、`ai_coding/ui/repl.py`、`ai_coding/core/agent_loop.py`
- Test: `tests/test_memory_evidence.py`、`tests/test_memory_turn_evidence.py`

**Interfaces:**
- Produces: `MemoryNote.evidence: str = ""`（`"tool" | "model" | ""`）；`new_note(..., evidence="")`；
  `MemoryService.harvest(..., evidence="")`；`MemoryService.status()["by_evidence"]`；
  `MemoryHarvested.evidence: str = ""`；
  `TurnMemory.harvest(..., turn: int | None = None, evidence: str = "")`
  （`turn=None` 保留旧行为 `len(session.messages)`，既有测试因此不改）

- [ ] **Step 1: 写失败测试** `tests/test_memory_evidence.py`

```python
"""Memories carry where they came from (R6d) — metadata, not a filter."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.config.models import MemoryConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.memory.note import MemoryNote, new_note
from ai_coding.memory.schema import HarvestCandidate
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


class _Extractor:
    """Answers with fixed candidates and records the batches it saw."""

    def __init__(self, *texts: str) -> None:
        self.texts = texts
        self.seen: list[list[ChatMessage]] = []

    async def __call__(self, messages, conclusion=None):
        self.seen.append(list(messages))
        return [HarvestCandidate(text=text) for text in self.texts]


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text, timestamp="t")


def _service(tmp_path: Path, *texts: str) -> tuple[MemoryService, MemoryStore]:
    store = MemoryStore(tmp_path / ".memory")
    service = MemoryService(store, extractor=_Extractor(*texts), config=MemoryConfig())
    return service, store


@pytest.mark.asyncio
async def test_notes_from_a_tool_backed_turn_are_marked_tool(tmp_path):
    service, store = _service(tmp_path, "项目用 uv 管理依赖")
    await service.harvest([_user("我们改用 uv 了")], turn_index=3, evidence="tool")
    assert [note.evidence for note in store.active()] == ["tool"]


@pytest.mark.asyncio
async def test_notes_from_a_turn_without_tools_are_marked_model(tmp_path):
    service, store = _service(tmp_path, "项目用 uv 管理依赖")
    await service.harvest([_user("我们改用 uv 了")], turn_index=3, evidence="model")
    assert [note.evidence for note in store.active()] == ["model"]


def test_evidence_survives_the_store_round_trip(tmp_path):
    store = MemoryStore(tmp_path / ".memory")
    store.add_note(new_note("有工具证据的", evidence="tool"))
    reread = MemoryStore(tmp_path / ".memory")   # 重新打开：真的走了磁盘
    assert [note.evidence for note in reread.all()] == ["tool"]
    # 旧行没有这个字段：读回来是空串（未知），不是异常
    legacy = MemoryNote.from_dict({"id": "x", "text": "老数据"})
    assert legacy.evidence == ""


@pytest.mark.asyncio
async def test_status_counts_by_evidence(tmp_path):
    service, store = _service(tmp_path, "a", "b")
    await service.harvest([_user("x")], evidence="tool")
    store.add_note(new_note("c", evidence="model"))
    assert service.status()["by_evidence"] == {"tool": 2, "model": 1}
```

`tests/test_memory_turn_evidence.py`（工具面：事件与溯源）

```python
"""TurnMemory passes the turn's evidence through, and the event carries it (R6d)."""

from __future__ import annotations

from typing import Any

import pytest

from ai_coding.core.events import MemoryHarvested, RecordingObserver
from ai_coding.core.memory_turn import TurnMemory
from ai_coding.domain.loop import TurnOutcome, TurnStatus


class _Report:
    def __init__(self, added=("a",)) -> None:
        self.added = added
        self.updated: tuple[str, ...] = ()
        self.superseded: tuple[str, ...] = ()
        self.skipped: tuple[str, ...] = ()


class _Memory:
    """Records the kwargs the turn handed to the service."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def harvest(self, messages, **kwargs: Any) -> _Report:
        self.kwargs = kwargs
        return _Report()

    async def consolidate(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.session_id = "s"
        self.messages = ["m1", "m2"]
        self.harvested_messages = 0


def _outcome() -> TurnOutcome:
    return TurnOutcome(status=TurnStatus.COMPLETED, text="done", iterations=1)


@pytest.mark.asyncio
async def test_harvest_event_carries_the_evidence_and_the_turn_number():
    memory = _Memory()
    observer = RecordingObserver()
    turn = TurnMemory(memory, emit=observer.on_event, save=lambda session: None)
    await turn.harvest(
        _Session(), deadline=None, summaries=[], outcome=_outcome(), turn=3, evidence="tool"
    )
    assert memory.kwargs["turn_index"] == 3      # 是回合号，不是消息条数
    assert memory.kwargs["evidence"] == "tool"
    assert observer.of_type(MemoryHarvested)[0].evidence == "tool"


@pytest.mark.asyncio
async def test_without_a_turn_number_the_old_cursor_semantics_survive():
    """既有调用点（以及既有测试）不传 turn，就必须保持原样。"""
    memory = _Memory()
    turn = TurnMemory(memory, emit=RecordingObserver().on_event, save=lambda session: None)
    session = _Session()
    await turn.harvest(session, deadline=None, summaries=[], outcome=_outcome())
    assert memory.kwargs["turn_index"] == len(session.messages)
    assert memory.kwargs["evidence"] == ""
```

- [ ] **Step 2–4**: 实现；循环侧用「本回合有没有成功工具调用」算 evidence：

```python
        evidence = "tool" if successful_calls else "model"
        await self.turn_memory.harvest(
            session,
            deadline=deadline,
            summaries=summaries,
            outcome=outcome,
            turn=turn_number,
            evidence=evidence,
        )
```

- [ ] **Step 5: 门禁 + 提交**

```bash
git add -A && git commit -m "R6d: memories carry where they came from, and turn means turn"
```

---

### Task 12: 索引不变量回归

**Files:**
- Test: `tests/test_memory_index_invariants.py`

**Interfaces:**
- Consumes: `MemoryService.index_block()` / `index_notes()` / `MemoryStore`
- Produces: 三条被锁死的不变量（若断言失败，只允许把实现改得更严格）

- [ ] **Step 1: 写失败测试** `tests/test_memory_index_invariants.py`

```python
"""The index is a pointer list with a hard budget (R6e — closes D-R4-15)."""

from __future__ import annotations

import re
from pathlib import Path

from ai_coding.config.models import MemoryConfig
from ai_coding.memory.note import NoteKind, new_note
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore

# 一行 = 一个指针：「- [kind#id8] 一句话」，正文不进索引
POINTER = re.compile(r"^- \[(fact|decision|preference|open_thread)#[0-9a-f]{8}\] \S")


def _service(tmp_path: Path, **overrides) -> tuple[MemoryService, MemoryStore]:
    store = MemoryStore(tmp_path / ".memory")
    return MemoryService(store, config=MemoryConfig(**overrides)), store


def test_index_lines_are_pointers_only(tmp_path):
    service, store = _service(tmp_path)
    for index in range(5):
        store.add_note(new_note(f"决策 {index}：用 uv 管理依赖", kind=NoteKind.DECISION))
    block = service.index_block()
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert lines, "决定类 note 必须出现在常驻块里"
    assert all(POINTER.match(line) for line in lines)
    assert all("\n" not in line for line in lines)


def test_index_never_exceeds_its_budget(tmp_path):
    service, store = _service(tmp_path, index_max_chars=600)
    for index in range(200):
        store.add_note(
            new_note(f"决策 {index}：这条写得像真的会出现在项目里的一句话", kind=NoteKind.DECISION)
        )
    block = service.index_block()
    assert block.startswith("<memory-constraints>")
    assert len(block) <= 600


def test_index_does_not_grow_when_the_store_grows(tmp_path):
    service, store = _service(tmp_path, index_max_chars=600, max_index_entries=200)
    for index in range(5):
        store.add_note(new_note(f"初始决策 {index}：项目约定", kind=NoteKind.DECISION))
    before = service.index_block()
    for index in range(100):
        store.add_note(new_note(f"后续事实 {index}：读到的细节", kind=NoteKind.FACT))
    assert service.index_block() == before      # L2/L3 增长不推高索引
    assert len(service.index_block()) <= 600
```

- [ ] **Step 2–4**: 跑到绿（必要时只做「更严格」的最小修正）→ 提交

```bash
git add tests/test_memory_index_invariants.py
git commit -m "R6e: the index is pointers, bounded, and does not grow with the store"
```

---

### Task 13: 验收脚本、文档同步与收尾核验

**Files:**
- Create/complete: `scripts/r6_cases.py`
- Modify: `docs/data-contracts.md`、`docs/architecture.md`、`docs/features.md`、`docs/how-to-run.md`、`docs/integration_testing.md`、`docs/README.md`
- Modify: `docs/r6-analysis.md`（把已锁定的决策点标注为「已采纳」）

**Interfaces:**
- Produces: `scripts/r6_cases.py`（离线 8 条退出码 0；`--real` 3 条只报告）

- [ ] **Step 1: 写脚本**（结构照 `scripts/memory_cases.py`：`CaseFn` 列表 + `--only` + 退出码）

```python
#!/usr/bin/env python3
"""R6 acceptance cases — offline by default, ``--real`` adds the live ones.

Offline cases replay docs/r6-analysis.md section 1 with fakes: the digest ring,
the compaction-triggered injection, the handoff, the escalation ladder, ask_user
with and without a human, the evidence metadata and the index invariants.

Live cases re-run the same experiments against a real model.  They report rather
than asserting, except where the evidence is structural (an empty answer, a bash
call that reads the session file, a prompt-token ceiling).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Callable, Coroutine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.config.models import AIConfig, MemoryConfig            # noqa: E402
from ai_coding.core.agent_loop import AgentLoop                      # noqa: E402
from ai_coding.domain.loop import StepResult, TurnStatus             # noqa: E402
from ai_coding.domain.message import ChatMessage, ToolCallRef, ToolResult  # noqa: E402
from ai_coding.domain.session import SessionData                     # noqa: E402
from ai_coding.domain.task import TaskState                          # noqa: E402
from ai_coding.domain.turn_digest import (                           # noqa: E402
    TurnDigest,
    append_digest,
    build_digest,
    render_working_memory,
)
from ai_coding.memory.note import NoteKind, new_note                 # noqa: E402
from ai_coding.memory.schema import HarvestCandidate                 # noqa: E402
from ai_coding.memory.service import MemoryService                   # noqa: E402
from ai_coding.memory.store import MemoryStore                       # noqa: E402
from ai_coding.security.user_prompt import UnavailableUserPromptCallback, UserAnswer  # noqa: E402
from ai_coding.tools.ask_user_tool import AskUserTool                # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext               # noqa: E402
from ai_coding.tools.registry import ToolRegistry                    # noqa: E402
from ai_coding.ai.engine import ChatEngine                           # noqa: E402

SEP = "-" * 64
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


# ---- shared fakes (same shapes as the unit tests) ----------------------

class _Engine(ChatEngine):
    def __init__(self, tool_name: str | None = None, reply: str = "做完了") -> None:
        self.tool_name, self.reply, self.calls = tool_name, reply, 0
        self.requests: list[list[ChatMessage]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        self.calls += 1
        if self.tool_name is None:
            return StepResult(text=self.reply, tool_calls=[], finish_reason="stop")
        return StepResult(
            text=None,
            tool_calls=[ToolCallRef(id=f"c{self.calls}", name=self.tool_name, arguments="{}")],
            finish_reason="tool_calls",
        )


class _Tool(BaseTool):
    parallel_safe = True

    def __init__(self, name: str, success: bool = True) -> None:
        self._name, self.success = name, success

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=self.success, output="ok" if self.success else "boom",
                          error=None if self.success else "nonzero_exit")


class _Sessions:
    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)


class _Context:
    def __init__(self, rewrite: bool) -> None:
        self.rewrite = rewrite

    async def compress_if_needed(self, messages, *, force=False, on_summary=None):
        if not self.rewrite:
            return messages
        return [*messages, ChatMessage(role="user", content="[compacted]", timestamp="")]


class _Extractor:
    def __init__(self, *texts: str) -> None:
        self.texts = texts

    async def __call__(self, messages, conclusion=None):
        return [HarvestCandidate(text=text) for text in self.texts]


def _loop(engine: ChatEngine, context: Any = None, **overrides: Any) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_Tool("poke"))
    registry.register(_Tool("flaky", success=False))
    return AgentLoop(engine, _Sessions(), registry, settings=AIConfig(**overrides),
                     context=context, workspace=".")


def _session() -> SessionData:
    return SessionData(session_id="case", title="", created_time="t", last_access_time="t")


def _joined(engine: _Engine) -> str:
    return "\n".join(m.content or "" for m in engine.requests[-1])


# ---- offline cases ----------------------------------------------------

async def case_digest_ring_is_bounded(root: Path) -> tuple[bool, str]:
    session = _session()
    for turn in range(1, 31):
        append_digest(session, TurnDigest(turn=turn, request=f"r{turn}", outcome="ok"), keep=12)
    ok = len(session.turn_digests) == 12 and session.turn_digests[0].turn == 19
    return ok, f"ring={len(session.turn_digests)} oldest=t{session.turn_digests[0].turn}"


async def case_ring_is_not_injected_without_compaction(root: Path) -> tuple[bool, str]:
    engine = _Engine()
    loop = _loop(engine, _Context(rewrite=False))
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    ok = "<working-memory>" not in _joined(engine)
    return ok, "no digest block in an ordinary turn"


async def case_ring_is_injected_after_compaction(root: Path) -> tuple[bool, str]:
    engine = _Engine()
    loop = _loop(engine, _Context(rewrite=True))
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    block = _joined(engine)
    size = len(block.split("<working-memory>")[1].split("</working-memory>")[0]) if "<working-memory>" in block else 0
    ok = "<working-memory>" in block and size <= 1200
    return ok, f"digest block injected, {size} chars"


async def case_exhausted_turn_reports_instead_of_printing_nothing(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool_name="poke")
    loop = _loop(engine, max_tool_iterations=2)
    session = _session()
    answer = await loop.process_input(session, "让 make test 通过")
    outcome = loop.last_outcome
    ok = bool(answer) and "[回合未完成" in answer and outcome is not None and outcome.text is None
    return ok, f"{len(answer)} chars of handoff, outcome.text={None if outcome is None else outcome.text!r}"


async def case_repeated_failures_get_one_strategy_nudge(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool_name="flaky")
    loop = _loop(engine, max_tool_iterations=6, stall_iterations=0, failed_streak_threshold=2)
    session = _session()
    await loop.process_input(session, "修好它")
    gates = [m for m in session.messages if m.role == "user" and "[strategy-gate]" in (m.content or "")]
    ok = len(gates) == 1
    return ok, f"{len(gates)} strategy gate(s)"


async def case_stalled_turn_stops_early_with_a_handoff(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool_name="flaky")
    loop = _loop(engine, max_tool_iterations=8, stall_iterations=3)
    session = _session()
    answer = await loop.process_input(session, "修好它")
    outcome = loop.last_outcome
    ok = outcome is not None and "no progress" in outcome.reason and outcome.iterations < 8 and "[回合未完成" in answer
    return ok, f"{None if outcome is None else outcome.iterations} iterations, reason={None if outcome is None else outcome.reason!r}"


async def case_ask_user_without_a_human(root: Path) -> tuple[bool, str]:
    tool = AskUserTool(UnavailableUserPromptCallback())
    result = await tool.execute({"question": "改哪个？"})
    ok = result.success is False and result.error == "no_user" and "假设" in result.output
    return ok, f"error={result.error}"


async def case_ask_user_with_a_scripted_human(root: Path) -> tuple[bool, str]:
    class _Human:
        async def ask(self, question, options=()):
            return UserAnswer(answered=True, text="改 A")

    result = await AskUserTool(_Human()).execute({"question": "改哪个？"})
    ok = result.success is True and result.data["answer"] == "改 A"
    return ok, f"answer={result.data.get('answer')!r}"


async def case_evidence_and_index_invariants(root: Path) -> tuple[bool, str]:
    store = MemoryStore(root / ".memory")
    service = MemoryService(store, extractor=_Extractor("项目用 uv 管理依赖"), config=MemoryConfig())
    await service.harvest([ChatMessage(role="user", content="改用 uv", timestamp="t")],
                          turn_index=3, evidence="tool")
    store.add_note(new_note("用 uv 管理依赖", kind=NoteKind.DECISION))
    notes = store.active()
    status = service.status()
    block = service.index_block()
    ok = (all(note.evidence == "tool" for note in notes)
          and status["by_evidence"] == {"tool": len(notes)}
          and len(block) <= 600
          and all(line.startswith("- [") for line in block.splitlines() if line.startswith("- ")))
    return ok, f"{len(notes)} notes, index={len(block)} chars"


OFFLINE: list[tuple[str, CaseFn]] = [
    ("digest_ring_is_bounded", case_digest_ring_is_bounded),
    ("ring_absent_without_compaction", case_ring_is_not_injected_without_compaction),
    ("ring_present_after_compaction", case_ring_is_injected_after_compaction),
    ("exhausted_turn_reports", case_exhausted_turn_reports_instead_of_printing_nothing),
    ("one_strategy_nudge", case_repeated_failures_get_one_strategy_nudge),
    ("stalled_turn_stops_early", case_stalled_turn_stops_early_with_a_handoff),
    ("ask_user_without_a_human", case_ask_user_without_a_human),
    ("ask_user_with_a_human", case_ask_user_with_a_scripted_human),
    ("evidence_and_index", case_evidence_and_index_invariants),
]


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated case names")
    parser.add_argument("--real", action="store_true", help="also run the live LLM cases")
    parser.add_argument("--config", default="config.local.yaml", help="config for --real")
    args = parser.parse_args(argv)

    wanted = {name for name in args.only.split(",") if name}
    import tempfile

    failures = 0
    print(SEP)
    for name, case in OFFLINE:
        if wanted and name not in wanted:
            continue
        with tempfile.TemporaryDirectory() as raw:
            ok, detail = await case(Path(raw))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
        failures += 0 if ok else 1
    if args.real:
        for name, case in REAL:
            ok, detail = await case(Path(tempfile.mkdtemp(prefix="r6-real-")))
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
            failures += 0 if ok else 1
    print(SEP)
    print(f"{'all offline cases passed' if not failures else f'{failures} case(s) failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2: 三个真实验收（同一脚本，`--real` 才跑；`config_path` 从 `--config` 传）**

```python
# ---- live cases: the three experiments from docs/r6-analysis.md section 1 ----
# 除 R1 外只报告：LLM 输出有随机性，但 R1/R2 的判据是结构化事实。

def _real_loop(config_path: str, workspace: Path, **overrides: Any) -> AgentLoop:
    """The same composition the CLI uses: real engine, real tools, real memory."""
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.cli import _build_loop, _build_tool_registry
    from ai_coding.config.loader import ConfigManager

    manager = ConfigManager()
    manager.initialize(config_path)
    app_cfg = manager.app
    for key, value in overrides.items():
        setattr(app_cfg.ai, key, value)
    engine = build_engine_from_config(app_cfg)
    registry = _build_tool_registry(app_cfg, str(workspace), None)
    return _build_loop(engine, _Sessions(), registry, app_cfg, str(workspace), None)


async def real_r1_budget_exhaustion_reports(root: Path) -> tuple[bool, str]:
    """1.3: a turn that runs out of budget must report, not return ""."""
    workspace = root / "ws"
    (workspace / "tests").mkdir(parents=True)
    loop = _real_loop(_CONFIG, workspace, max_tool_iterations=2)
    prompt = "让这个仓库的验收命令 `make test` 通过（必须真实执行看到成功输出）"
    answer = await loop.process_input(_session(), prompt)
    await loop.aclose()
    ok = bool(answer) and "[回合未完成" in answer
    return ok, f"{len(answer)} chars, head={answer[:60]!r}"


async def real_r2_compacted_recall(root: Path) -> tuple[bool, str]:
    """1.2: the middle-of-history question is answered from the ring, not by digging."""
    workspace = root / "ws"
    (workspace / "sessions").mkdir(parents=True)
    session = _synth_compacted_session()                 # spec §1.2 的 291 条
    (workspace / "sessions" / "probe1.json").write_text(
        json.dumps(session.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    loop = _real_loop(_CONFIG, workspace)
    answer = await loop.process_input(
        session, "回顾一下：这个任务里我们最终决定用哪个依赖管理工具？为什么？"
    )
    trace_events = _read_trace(loop, session.session_id)  # 从 .ai-coding/traces 读本回合
    await loop.aclose()
    dug = any(
        "sessions/" in json.dumps(event) and "bash" in json.dumps(event)
        for event in trace_events
    )
    answer_mentions_uv = "uv" in answer
    return (answer_mentions_uv and not dug), f"uv={answer_mentions_uv} session_file_read={dug}"


async def real_r3_ask_user_with_a_human(root: Path) -> tuple[bool, str]:
    """2.3: a scripted human answers, and the answer becomes a tool result."""
    class _Human:
        async def ask(self, question, options=()):
            return UserAnswer(answered=True, text="用 uv")

    result = await AskUserTool(_Human()).execute({"question": "依赖工具用哪个？"})
    return result.data.get("answer") == "用 uv", f"{result.data!r}"


REAL: list[tuple[str, CaseFn]] = [
    ("R1_exhausted_turn_reports", real_r1_budget_exhaustion_reports),
    ("R2_compacted_recall", real_r2_compacted_recall),
    ("R3_ask_user", real_r3_ask_user_with_a_human),
]
```

`_CONFIG` 是 `--config` 的模块级副本（在 `main` 里赋值），`_synth_compacted_session()` 复用
spec §1.2 的构造脚本（70 轮填充 + 第 3 轮写进 `uv` 决策），`_read_trace()` 读
`<workspace>/.ai-coding/traces/<session>/turn-1.jsonl` 并返回事件字典列表。
- [ ] **Step 2: 跑离线**：`.venv/bin/python scripts/r6_cases.py` → 8/8，退出码 0
- [ ] **Step 3: 跑真实**：`.venv/bin/python scripts/r6_cases.py --real --config config.local.yaml`
  → R1/R2/R3 各打印 PASS/FAIL（结构性判据）
- [ ] **Step 4: 文档同步**（按 spec §13 的表逐条补齐）
- [ ] **Step 5: 收尾核验 + 提交**

```bash
.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding
.venv/bin/python scripts/memory_cases.py && .venv/bin/python scripts/trace_cases.py
git add -A && git commit -m "R6: docs, acceptance script and the final gate"
```

---

## Self-Review（写完计划后自查，已逐条核过）

**1. Spec 覆盖**：spec §2.1→Task 1/2/3/4；§2.2→Task 5/6/7；§2.3→Task 8/9/10；
§2.4→Task 11；§2.5→Task 12；§13 文档同步→Task 13。**无缺口**。
spec §5 契约表逐行对应：`TurnOutcome.handoff`(T3)、`SessionData` 两字段(T2)、
`MemoryNote.evidence`(T11)、`MemoryHarvested.evidence`(T11)、`TurnPolicyConfig/TurnSnapshot`(T5)、
`AIConfig` 五键(T3/T5)、`ToolsConfig.ask_user`(T10)、`ToolResult.error` 两码(T9)、
`Compactor.did_compact`(T3)、`process_input` 返回值(T3)。

**2. 占位扫描**：无 `TBD/TODO/待补`，无用 `...` 省略的测试函数体。Task 1/3/5/6/9/11/12 的
测试都给了完整函数体；Task 13 的脚本给了完整的离线 9 条 + 真实 3 条（其中
`_synth_compacted_session` / `_read_trace` / `_CONFIG` 三个辅助在 Step 2 的文字里说清来源，
它们是把 spec §1.2 的实验代码搬进脚本，不是待定设计）。

**3. 类型一致性**：`build_digest`/`append_digest`/`render_working_memory`/`render_handoff`
的名称与签名在 Task 1、3、4、7 中一致；`did_compact(source, result)` 在 Task 3 定义、
Task 3 使用；`TurnSnapshot` 三个新字段名在 Task 5（定义）与 Task 6（使用）一致；
`TurnMemory.harvest(turn=, evidence=)` 在 Task 11 定义与使用一致；
`AskUserTool(prompt, timeout_seconds=)` 在 Task 9（定义）与 Task 10（装配）一致。

**4. 已知的执行期风险**（执行时逐条确认）：
- `session.py` 与 `turn_digest.py` 的导入方向：`turn_digest` **不得** import `session`
  （`append_digest` 收 `Any`），否则成环。
- `_run_tools` 的返回值从 `bool` 改成 `list[DispatchOutcome]`：已核验**没有测试**
  直接调用它；改动后调用方自己判 `halted`。
- 换策略的 NUDGE 会消耗一个迭代号（与既有 todo 门禁一致），因此
  `max_tool_iterations=2` 的极紧配置下它可能挤掉一次模型调用——测试里用 ≥4 的预算。
