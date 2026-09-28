# R8 Implementation Plan（checkpoint / evidence / 工具流式与取消）

> **For agentic workers:** 本项目用 **Inline Execution**（用户明确要求「不准派子任务，自己实现」）。
> 逐步执行本计划时用 `- [ ]` 勾选跟踪；每个 Task 结束跑一次质量门禁并单独提交。

**Goal:** 让压缩后的回合摘要多一行「模型自己写的状态」；让 `evidence` 能被筛、能在同分时择优；让长命令
不再黑屏、让卡住的单次调用能被真取消。

**Architecture:** 三个切片互不依赖。R8b 只动 `memory/relevance.py` 的排序键与 `memory_search` 的参数；
R8a 在既有的 `TurnDigest` / `TurnBooks` / `ToolContext` 三条通道上接一个新工具；R8c 把 `run_process`
从 `communicate()` 改成「两个读线程 + 主线程等待」，再让取消沿着
`Reader → AgentLoop → ToolDispatcher → run_with_deadline → run_process(killpg)` 一路传下去。

**Tech Stack:** Python 3.13（mypy 按 3.10 下限）、asyncio、subprocess + threads、prompt_toolkit、
rich、pytest（`-o addopts=""`）、ruff、mypy。

**Spec:** `docs/r8-analysis.md`（提交 `7a272f2`）。判据、行号与实测数字都在那份文档里；本计划不重复论证，
只写「怎么做、怎么测」。

## Global Constraints

动手前已拍板的值（用户「就按照你推荐的来」），实现时不要改主意：

- **D-1**：`checkpoint_write` 的 note **替换** digest 行里的「结果那句话」；上限 `ai.working_memory_note_chars = 120`；harness 的 `（改了 …）` 与非 completed 的 status 照旧。
- **D-2**：checkpoint **不**写 `.memory`。
- **D-3 / D-13**：`evidence` 只参与**按需召回**（`rank_notes` 里顶替 `note.id`）；**不动** `_better_of`，只加一条回归钉子。
- **D-4**：`memory_search` 加 `evidence` 过滤（`tool` / `model` / 空=不限）；未知值 → 结构化错误 `bad_evidence`，不静默放行。
- **D-5 / D-8 / D-9**：流式改 `run_process`（边跑边说）；`ui.tool_chunks` 默认 `true`；控制台节流 **0.15s/行**。
- **D-6 / D-7**：**Esc** 触发（外加程序化 API）；取消之后**回合继续**，模型看到 `error="cancelled_by_user"`。
- **D-10**：取消必须**真的杀掉进程组**（`os.killpg`），不是只放弃等待。
- **D-11**：被取消的调用**不计入** `failed_streak`、**不进** `failures`。
- **D-12**：`checkpoint_write` 是 `parallel_safe = True`；同一批里多次调用**按调用顺序取最后一条**。
- **D-14**：`checkpoint_write` **不算**「工具证据」（`evidence` 判据排除它）。

工程质量与流程约束（与前几轮一致）：

- 质量门禁：`pytest tests/ -o addopts="" -q`（当前 **1010 passed / 2 skipped**）、`ruff check .`、`mypy ai_coding`。
- **既有测试断言一条都不改**；只有确认测试本身写错才动，并在提交信息与回复里写明理由。
- TDD：先写红测试 → 跑红 → 最小实现 → 跑绿 → 提交。一个 Task 一个提交。
- 提交信息不含反引号；多行信息用 `git commit -F <file>`。
- 不改：压缩管线、收割提取语义、steering 队列与投递点、审批路径、会话文件格式（除 `turnDigests` 的两个可选字段）。
- 新增公开接口（后续 Task 的名字/签名以此为准）：
  `memory.relevance.evidence_rank(note) -> int`；
  `TurnDigest.note/.source`、`build_digest(..., model_note="", note_chars=120)`、`DEFAULT_NOTE_CHARS = 120`；
  `TurnBooks.checkpoint`、`TurnBooks.note_checkpoint(text, *, limit)`、`TurnBooks.evidence_calls`；
  `ToolContext.note_checkpoint`、`ToolContext.kill_event`；
  `ToolChunk(call_id, tool_name, stream, text, seq)`；
  `run_process(..., on_chunk=None, cancel=None)`、`run_process_async(..., on_chunk=None, cancel=None)`；
  `ProcessOutcome.cancelled`；`RunCancelled`；`domain.message.CANCELLED_BY_USER`。

## File Structure

| 文件 | 责任 | Task |
|---|---|---|
| `ai_coding/memory/relevance.py` | 召回排序（`evidence_rank` 作同分 tie-break） | 1 |
| `ai_coding/tools/memory_search_tool.py` | 按需召回的参数面（`evidence` 过滤） | 3 |
| `ai_coding/domain/turn_digest.py` | digest 行的值对象（`note` / `source`） | 4 |
| `ai_coding/core/turn_books.py` | 一个回合的计数与状态（checkpoint、证据计数、取消不计失败） | 5, 12 |
| `ai_coding/tools/base.py`、`core/request_builder.py`、`core/agent_loop.py`、`core/turn_close.py` | 把工具写下的东西送回 `build_digest` | 6, 11 |
| `ai_coding/tools/checkpoint_tool.py`（新） | `checkpoint_write` 工具本体 | 7 |
| `ai_coding/tools/builder.py`、`cli.py`、`config/models.py` | 注册与配置 | 7 |
| `ai_coding/core/events.py` | `ToolChunk` 事件 | 9 |
| `ai_coding/infra/subprocess_runner.py` | 读线程 + 排空 + 取消杀进程组 | 9, 12 |
| `ai_coding/tools/bash_tool.py`、`ui/console.py`、`core/mcp.py` | 把 chunk 送到控制台 | 10 |
| `ai_coding/infra/deadline.py`、`core/dispatch.py` | `cancel=` 与 `cancelled_by_user` | 11 |
| `ai_coding/ui/reader.py`、`cli.py` | Esc 键与装配 | 13 |
| `scripts/mcp_progress_server.py`（新）、`scripts/r8_cases.py`（新） | 真 stdio 用例 + 验收脚本 | 13, 14 |

---

### Task 1: `evidence` 在召回里只破平局

**Files:**
- Modify: `ai_coding/memory/relevance.py`
- Test: `tests/test_memory_relevance.py`（追加）

**Interfaces:**
- Consumes: `MemoryNote.evidence`（`"tool"` / `"model"` / `""`）
- Produces: `evidence_rank(note: MemoryNote) -> int`（0 = tool，1 = 其它）

- [ ] **Step 1: 写红测试**

```python
def _ev_note(note_id: str, evidence: str) -> MemoryNote:
    return _note(note_id, "构建用 uv", evidence=evidence)


def test_r8_evidence_breaks_a_tie_between_identical_notes():
    notes = [_ev_note("a", "model"), _ev_note("b", "tool")]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams(), now=NOW)
    assert [r.note.id for r in ranked] == ["b", "a"]


def test_r8_evidence_never_beats_a_higher_score():
    notes = [
        _note("strong", "构建用 uv，不要用 poetry", importance=5, evidence="model"),
        _note("weak", "构建用 uv", importance=1, evidence="tool"),
    ]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams(), now=NOW)
    assert ranked[0].note.id == "strong"


def test_r8_blank_evidence_ranks_like_model_not_worse():
    notes = [_ev_note("legacy", ""), _ev_note("stated", "model"), _ev_note("done", "tool")]
    ranked = rank_notes(notes, "构建用什么工具", params=RecallParams(), now=NOW)
    assert ranked[0].note.id == "done"
    assert [r.note.id for r in ranked[1:]] == ["legacy", "stated"]


def test_r8_a_lower_score_is_never_reordered_by_evidence():
    strong = _note("strong", "构建用 uv，不要用 poetry", importance=5, evidence="model")
    weak = _note("weak", "构建用 uv", importance=1, evidence="tool")
    ranked = rank_notes([weak, strong], "构建用什么工具", params=RecallParams(), now=NOW)
    assert ranked[0].score > ranked[-1].score
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_memory_relevance.py -o addopts="" -q`
Expected: `test_r8_evidence_breaks_a_tie_between_identical_notes` FAIL（同分时按 `note.id`，`a` 在前）

- [ ] **Step 3: 最小实现**

```python
#: Where a note came from, as a tie-breaker only (R8b, D-3).  `""` is legacy data
#: (harvested before R6d recorded evidence) and must not be punished.
_EVIDENCE_RANK: Mapping[str, int] = {"tool": 0, "model": 1, "": 1}


def evidence_rank(note: MemoryNote) -> int:
    """0 for a tool-backed note, 1 for everything else (R8b)."""
    return _EVIDENCE_RANK.get(str(getattr(note, "evidence", "") or ""), 1)
```

`rank_notes` 的排序键改成：

```python
    scored.sort(
        key=lambda item: (
            -item.score,
            -item.relevance,
            evidence_rank(item.note),
            item.note.id,
        )
    )
```

并把 `"evidence_rank"` 加进 `__all__`。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_memory_relevance.py tests/test_memory_service.py -o addopts="" -q`
Expected: PASS（既有 case 2/9/10 一条断言都不改）

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/relevance.py tests/test_memory_relevance.py
git commit -m "R8b-1: evidence only breaks a tie when the scores are equal"
```

---

### Task 2: 钉住「合并时不做 evidence 择优」

**Files:**
- Test: `tests/test_memory_service_v2.py`（追加；**不改实现**）

**Interfaces:**
- Consumes: `memory.service._better_of`
- Produces: 无（回归钉子，D-13 的先决条件）

- [ ] **Step 1: 写测试（这是「钉现状」，不是红测试）**

```python
def test_r8_merging_ignores_evidence(tmp_path):
    """`_better_of` keeps deciding on (importance, hits, created_time) (D-13)."""
    from ai_coding.memory.service import _better_of

    older_tool = MemoryNote(
        id="old", text="构建用 uv", evidence="tool",
        created_time="2026-01-01T00:00:00+00:00",
    )
    newer_stated = MemoryNote(
        id="new", text="构建用 uv", evidence="",
        created_time="2026-02-01T00:00:00+00:00",
    )
    keep, drop = _better_of(older_tool, newer_stated)
    assert (keep.id, drop.id) == ("new", "old")  # recency wins, not evidence

    hit_heavy = MemoryNote(
        id="hit", text="构建用 uv", hits=5, evidence="",
        created_time="2026-01-01T00:00:00+00:00",
    )
    keep, drop = _better_of(older_tool, hit_heavy)
    assert (keep.id, drop.id) == ("hit", "old")  # hits outrank evidence
```

- [ ] **Step 2: 跑一次**

Run: `.venv/bin/python -m pytest tests/test_memory_service_v2.py -o addopts="" -q`
Expected: PASS（若 FAIL 说明 `_better_of` 的现状与文档不符 → 先核对，不要改断言）

- [ ] **Step 3: 提交**

```bash
git add tests/test_memory_service_v2.py
git commit -m "R8b-2: pin the merge decision so evidence stays out of it"
```

---

### Task 3: `memory_search` 的 `evidence` 过滤

**Files:**
- Modify: `ai_coding/tools/memory_search_tool.py`
- Test: `tests/test_tool_memory_search.py`（追加）

**Interfaces:**
- Consumes: `MemoryNote.evidence`
- Produces: `memory_search` 参数 `evidence: "tool" | "model" | ""`；错误码 `bad_evidence`

- [ ] **Step 1: 写红测试**

```python
def _evidence_service(tmp_path) -> MemoryService:
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="t1", text="构建用 uv", evidence="tool"))
    store.add_note(MemoryNote(id="m1", text="构建改用 uv，不要 poetry", evidence="model"))
    store.add_note(MemoryNote(id="l1", text="构建用 uv，老数据", evidence=""))
    return MemoryService(store)


@pytest.mark.asyncio
async def test_r8_evidence_filter_keeps_only_tool_backed(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))
    result = await tool.execute({"query": "构建用什么", "evidence": "tool"})
    assert result.data is not None
    assert [n["id"] for n in result.data["notes"]] == ["t1"]
    assert result.data["notes"][0]["evidence"] == "tool"


@pytest.mark.asyncio
async def test_r8_evidence_filter_model_means_not_tool(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))
    result = await tool.execute({"query": "构建用什么", "evidence": "model"})
    assert {n["id"] for n in result.data["notes"]} == {"m1", "l1"}


@pytest.mark.asyncio
async def test_r8_evidence_empty_is_exactly_today(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))
    result = await tool.execute({"query": "构建用什么"})
    assert {n["id"] for n in result.data["notes"]} == {"t1", "m1", "l1"}


@pytest.mark.asyncio
async def test_r8_unknown_evidence_is_an_error_not_a_silent_pass(tmp_path):
    tool = MemorySearchTool(_evidence_service(tmp_path))
    result = await tool.execute({"query": "构建用什么", "evidence": "maybe"})
    assert result.success is False
    assert result.error == "bad_evidence"
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_tool_memory_search.py -o addopts="" -q`
Expected: FAIL（`evidence` 现在被忽略，未知值也当没写）

- [ ] **Step 3: 最小实现**

`params_json_schema` 的 `properties` 加：

```python
                "evidence": {
                    "type": "string",
                    "description": (
                        "Optional filter: 'tool' = notes recorded in a turn where a "
                        "tool call succeeded; 'model' = everything else (including "
                        "notes from before this was recorded)."
                    ),
                },
```

`execute()` 在拿到 `hits` 之后：

```python
        wanted_evidence = str(params.get("evidence", "") or "").strip().casefold()
        if wanted_evidence and wanted_evidence not in ("tool", "model"):
            return ToolResult(
                success=False,
                output="error: 'evidence' must be 'tool', 'model' or empty",
                error="bad_evidence",
                data={"evidence": wanted_evidence},
            )
        if wanted_evidence == "tool":
            hits = [hit for hit in hits if (hit.note.evidence or "") == "tool"]
        elif wanted_evidence == "model":
            hits = [hit for hit in hits if (hit.note.evidence or "") != "tool"]
```

并在每条 note 的 dict 里加 `"evidence": hit.note.evidence`。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_tool_memory_search.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/tools/memory_search_tool.py tests/test_tool_memory_search.py
git commit -m "R8b-3: memory_search can ask for tool-backed notes only"
```

---

### Task 4: digest 行接受「模型写的那句话」

**Files:**
- Modify: `ai_coding/domain/turn_digest.py`
- Test: `tests/test_turn_digest.py`（追加）

**Interfaces:**
- Consumes: `TurnOutcome`
- Produces: `TurnDigest.note` / `.source` / `.display()`；`build_digest(..., model_note="", note_chars=DEFAULT_NOTE_CHARS)`；`DEFAULT_NOTE_CHARS = 120`

- [ ] **Step 1: 写红测试**

```python
def test_r8_note_replaces_the_outcome_sentence():
    digest = build_digest(
        turn=3,
        user_input="修 bash 超时",
        outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="做完了"),
        model_note="下一步：先补 runner 的用例",
    )
    assert digest.note == "下一步：先补 runner 的用例"
    assert digest.outcome == "做完了"  # the harness/model-derived sentence is still kept
    assert digest.render() == "- t3 修 bash 超时 → 下一步：先补 runner 的用例"


def test_r8_harness_facts_still_show_and_open_items_still_do_not():
    digest = build_digest(
        turn=3, user_input="修 bash 超时",
        outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="做完了"),
        artifacts=[ToolArtifact(path="ai_coding/infra/subprocess_runner.py")],
        task=TaskState(goal="g"),
        model_note="note",
    )
    line = digest.render()
    assert "改了 ai_coding/infra/subprocess_runner.py" in line
    assert "open" not in line


def test_r8_source_is_recorded_but_never_rendered():
    model = build_digest(turn=1, user_input="q",
                         outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="a"),
                         model_note="n")
    harness = build_digest(turn=1, user_input="q",
                           outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="a"))
    assert (model.source, harness.source) == ("model", "harness")
    assert "harness" not in model.render() and "model" not in model.render()


def test_r8_note_round_trips_and_old_rows_still_parse():
    digest = build_digest(turn=1, user_input="q",
                          outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="a"),
                          model_note="n")
    assert TurnDigest.from_dict(digest.to_dict()) == digest
    old = {"turn": 1, "request": "r", "outcome": "o", "status": "completed"}
    parsed = TurnDigest.from_dict(old)
    assert parsed is not None and parsed.note == "" and parsed.source == ""


def test_r8_no_note_is_byte_identical_to_today():
    digest = build_digest(turn=1, user_input="q",
                          outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="a"))
    assert digest.render() == "- t1 q → a"


def test_r8_a_note_longer_than_the_cap_is_cut():
    digest = build_digest(turn=1, user_input="q",
                          outcome=TurnOutcome(status=TurnStatus.COMPLETED, text="a"),
                          model_note="字" * 500, note_chars=10)
    assert len(digest.note) == 10
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_turn_digest.py -o addopts="" -q`
Expected: FAIL（`build_digest()` 没有 `model_note` 参数）

- [ ] **Step 3: 最小实现**

```python
DEFAULT_NOTE_CHARS = 120


@dataclass(frozen=True)
class TurnDigest:
    turn: int
    request: str
    outcome: str
    status: str = ""
    files: tuple[str, ...] = ()
    open_items: int = 0
    #: What the model wrote for its future self (R8a).  Empty = the old line.
    note: str = ""
    #: "model" when `note` came from `checkpoint_write`, else "harness".
    source: str = ""

    def display(self) -> str:
        """The sentence the ring shows: the model's note wins when there is one."""
        return self.note or self.outcome
```

`render()` 里把 `if self.outcome:` 与 `f" → {self.outcome}"` 换成 `self.display()`；
`to_dict` 在非空时写 `"note"` / `"source"`；`from_dict` 读它们（缺省 `""`）。`build_digest` 改成：

```python
def build_digest(
    *,
    turn: int,
    user_input: str,
    outcome: TurnOutcome,
    artifacts: Sequence[ToolArtifact] = (),
    task: TaskState | None = None,
    request_chars: int = DEFAULT_REQUEST_CHARS,
    outcome_chars: int = DEFAULT_OUTCOME_CHARS,
    model_note: str = "",
    note_chars: int = DEFAULT_NOTE_CHARS,
) -> TurnDigest:
    note = one_line(model_note, note_chars) if model_note else ""
    outcome_line = one_line(outcome.text or "", outcome_chars)
    if not outcome_line:
        label = _STATUS_LABEL.get(outcome.status, outcome.status.value)
        reason = one_line(outcome.reason, outcome_chars)
        outcome_line = f"{label}：{reason}" if reason else label
    return TurnDigest(
        turn=int(turn),
        request=one_line(user_input, request_chars),
        outcome=outcome_line,
        status=outcome.status.value,
        files=_unique_paths(artifacts),
        open_items=len(task.open_items()) if task is not None else 0,
        note=note,
        source="model" if note else "harness",
    )
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_turn_digest.py tests/test_loop_digest.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ai_coding/domain/turn_digest.py tests/test_turn_digest.py
git commit -m "R8a-1: a digest line can carry the model's own note"
```

---

### Task 5: 回合本子记下这句话

**Files:**
- Modify: `ai_coding/core/turn_books.py`
- Test: `tests/test_turn_books.py`（追加）

**Interfaces:**
- Produces: `TurnBooks.checkpoint: str | None`；`TurnBooks.note_checkpoint(text, *, limit=120) -> None`

- [ ] **Step 1: 写红测试**

```python
def test_r8_checkpoint_collapses_to_one_line_and_caps():
    books = TurnBooks()
    books.note_checkpoint("  第一行\n第二行  ", limit=10)
    assert books.checkpoint == "第一行 第二行"
    books.note_checkpoint("字" * 50, limit=5)
    assert books.checkpoint == "字字字字…"


def test_r8_last_checkpoint_of_a_batch_wins():
    books = TurnBooks()
    books.note_checkpoint("先写的", limit=120)
    books.note_checkpoint("后写的", limit=120)
    assert books.checkpoint == "后写的"


def test_r8_a_blank_checkpoint_never_erases_a_real_one():
    books = TurnBooks()
    books.note_checkpoint("有价值的", limit=120)
    books.note_checkpoint("   \n  ", limit=120)
    assert books.checkpoint == "有价值的"


def test_r8_a_fresh_turn_has_no_checkpoint():
    assert TurnBooks().checkpoint is None
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_turn_books.py -o addopts="" -q`
Expected: FAIL（`AttributeError: 'TurnBooks' object has no attribute 'note_checkpoint'`）

- [ ] **Step 3: 最小实现**

```python
from ai_coding.domain.turn_digest import DEFAULT_NOTE_CHARS, one_line

...
    #: What the model asked to remember about this turn (R8a); last write wins.
    checkpoint: str | None = None

...
    def note_checkpoint(self, text: str, *, limit: int = DEFAULT_NOTE_CHARS) -> None:
        """Record the model's own line for this turn (R8a, D-1/D-12)."""
        body = one_line(text, limit)
        if body:
            self.checkpoint = body
```

- [ ] **Step 4: 跑绿**

Run: `.venv/bin/python -m pytest tests/test_turn_books.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add ai_coding/core/turn_books.py tests/test_turn_books.py
git commit -m "R8a-2: the turn book keeps the model's checkpoint line"
```

---

### Task 6: 把 note 从工具送到 digest

**Files:**
- Modify: `ai_coding/tools/base.py`、`ai_coding/core/request_builder.py`、`ai_coding/core/agent_loop.py`、`ai_coding/core/turn_close.py`
- Test: `tests/test_loop_digest.py`（追加）

**Interfaces:**
- Produces: `ToolContext.note_checkpoint: Callable[[str], None] | None`；`TurnCloser.close(..., model_note="")`
- 注意：`_run_tools` 现在拿不到 `books`（调用点 `core/agent_loop.py:330`），本任务给它加参数

- [ ] **Step 1: 写红测试**

```python
class _CheckpointTool(BaseTool):
    """A stand-in for checkpoint_write: it only writes the turn's note."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "checkpoint_write"

    @property
    def description(self) -> str:
        return "checkpoint"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"note": {"type": "string"}}}

    async def execute(self, params, ctx: ToolContext | None = None) -> ToolResult:
        note = str(params.get("note", ""))
        if ctx is not None and ctx.note_checkpoint is not None:
            ctx.note_checkpoint(note)
        return ToolResult(success=True, output=f"remembered: {note}")


def _checkpoint_loop() -> tuple[AgentLoop, _Engine]:
    engine = _Engine(tool_name="checkpoint_write")
    loop = _loop(engine, _Compacting(rewrite=False), max_tool_iterations=2)
    loop.tools.register(_CheckpointTool())
    return loop, engine


@pytest.mark.asyncio
async def test_r8_the_tool_note_reaches_the_digest():
    loop, _engine_ = _checkpoint_loop()
    session = _session()
    await loop.process_input(session, "先写一行 checkpoint")
    assert session.turn_digests[-1].note == "先补 runner 的用例"
    assert session.turn_digests[-1].source == "model"


@pytest.mark.asyncio
async def test_r8_the_call_is_written_back_into_the_history():
    loop, _engine_ = _checkpoint_loop()
    session = _session()
    await loop.process_input(session, "先写一行 checkpoint")
    tool_messages = [m for m in session.messages if m.role == "tool"]
    assert tool_messages and "remembered" in (tool_messages[-1].content or "")


@pytest.mark.asyncio
async def test_r8_the_note_never_enters_the_system_prompt():
    loop, engine = _checkpoint_loop()
    await loop.process_input(_session(), "先写一行 checkpoint")
    assert all("先补 runner 的用例" not in (item or "") for item in engine.instructions)
```

`_Engine.step` 需要按工具名给出参数：

```python
        args = (
            '{"note": "先补 runner 的用例"}'
            if self.tool_name == "checkpoint_write"
            else "{}"
        )
        return StepResult(
            text=None,
            tool_calls=[
                ToolCallRef(id=f"c{len(self.requests)}", name=self.tool_name, arguments=args)
            ],
            finish_reason="tool_calls",
        )
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_loop_digest.py -o addopts="" -q`
Expected: FAIL（`ToolContext` 没有 `note_checkpoint`）

- [ ] **Step 3: 最小实现**

`tools/base.py`：

```python
    # R8a: how a tool tells the running turn "remember this line" (checkpoint_write).
    note_checkpoint: Callable[[str], None] | None = None
```

`core/request_builder.py::tool_context(..., note_checkpoint=None)` 把它传进 `ToolContext(...)`。

`core/agent_loop.py::_run_tools(..., books: TurnBooks, ...)`：

```python
        limit = int(getattr(self.settings, "working_memory_note_chars", 120) or 120)
        outcomes = await self.dispatcher.dispatch_batch(
            step.tool_calls,
            tool_context(
                ...,
                note_checkpoint=lambda text: books.note_checkpoint(text, limit=limit),
            ),
            ...
        )
```

调用点加 `books=books`；`_close_turn` 传 `model_note=books.checkpoint or ""`；
`core/turn_close.py::close(..., model_note: str = "")` →
`build_digest(..., model_note=model_note, note_chars=int(getattr(self._settings, "working_memory_note_chars", 120) or 120))`。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_loop_digest.py tests/test_request_builder.py tests/test_tool_context.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: PASS / clean

- [ ] **Step 5: 提交**

```bash
git add ai_coding/tools/base.py ai_coding/core/request_builder.py ai_coding/core/agent_loop.py ai_coding/core/turn_close.py tests/test_loop_digest.py
git commit -m "R8a-3: the loop carries the note from the tool to the digest"
```

---

### Task 7: `checkpoint_write` 工具本体

**Files:**
- Create: `ai_coding/tools/checkpoint_tool.py`
- Modify: `ai_coding/core/turn_books.py`（`evidence_calls`）、`ai_coding/core/agent_loop.py`（evidence 判据）、`ai_coding/cli.py`（注册）、`ai_coding/config/models.py`（配置键）
- Test: `tests/test_tool_checkpoint.py`（新建）、`tests/test_turn_books.py`、`tests/test_memory_turn_evidence.py`（追加）

**Interfaces:**
- Produces: `CheckpointTool`（`name = "checkpoint_write"`，ALLOW，`parallel_safe = True`）；`TurnBooks.evidence_calls`

- [ ] **Step 1: 写红测试**

新文件 `tests/test_tool_checkpoint.py`：

```python
import pytest

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.tools.base import ToolContext
from ai_coding.tools.checkpoint_tool import CheckpointTool


@pytest.mark.asyncio
async def test_r8_the_tool_hands_the_note_to_the_turn_and_echoes_it():
    seen: list[str] = []
    result = await CheckpointTool().execute(
        {"note": "下一步：补 runner 用例"}, ToolContext(note_checkpoint=seen.append)
    )
    assert result.success is True
    assert seen == ["下一步：补 runner 用例"]
    assert "下一步：补 runner 用例" in result.output


@pytest.mark.asyncio
async def test_r8_a_missing_note_is_a_structured_error():
    result = await CheckpointTool().execute({}, ToolContext(note_checkpoint=lambda _t: None))
    assert result.success is False
    assert result.error == "missing_note"


@pytest.mark.asyncio
async def test_r8_the_tool_reports_when_the_turn_cannot_be_reached():
    result = await CheckpointTool().execute({"note": "n"}, ToolContext())
    assert result.success is False
    assert result.error == "checkpoint_unavailable"


def test_r8_the_gate_allows_it_and_it_may_run_parallel():
    tool = CheckpointTool()
    assert tool.parallel_safe is True
    assert PermissionGate().check("checkpoint_write", {"note": "n"}).decision is (
        PermissionDecision.ALLOW
    )
```

`tests/test_turn_books.py`：

```python
def test_r8_a_checkpoint_write_is_not_world_evidence():
    books = TurnBooks()
    books.note_outcomes([_outcome("checkpoint_write", success=True)])
    assert books.successful_calls == 1   # the stall ladder still sees progress
    assert books.evidence_calls == 0     # but it is not evidence about the world (D-14)


def test_r8_a_real_tool_call_is_evidence():
    books = TurnBooks()
    books.note_outcomes([_outcome("bash", success=True)])
    assert books.evidence_calls == 1
```

`tests/test_memory_turn_evidence.py` 追加一条同义断言（用该文件既有的 `evidence` 判据 helper；
没有 helper 就直接断言 `books.evidence_calls == 0`）。

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_tool_checkpoint.py tests/test_turn_books.py -o addopts="" -q`
Expected: FAIL（模块不存在 / 没有 `evidence_calls`）

- [ ] **Step 3: 最小实现**

`tools/checkpoint_tool.py`：

```python
"""``checkpoint_write`` — the model writes one line for the next turn (R8a).

Different from ``memory_write``: that one is durable and cross-session; this one
lives in the session's digest ring and is only *injected* after a compaction.
"""

from __future__ import annotations

from typing import Any

from ai_coding.domain.message import ToolResult
from ai_coding.tools.base import BaseTool, ToolContext, resolve_ctx

__all__ = ["CheckpointTool"]


class CheckpointTool(BaseTool):
    """Write one line about where this turn got to."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "checkpoint_write"

    @property
    def description(self) -> str:
        return (
            "Write one short line for your own future turns in THIS session: what "
            "you were doing, what you decided, what the next step is. Do not repeat "
            "which files changed or the turn's status - the harness records those "
            "itself. For anything that must survive into another session use "
            "memory_write instead."
        )

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "One line, at most ~120 characters.",
                }
            },
            "required": ["note"],
        }

    async def execute(
        self, params: dict[str, Any], ctx: ToolContext | None = None
    ) -> ToolResult:
        note = str(params.get("note", "") or "").strip()
        if not note:
            return ToolResult(
                success=False,
                output="error: 'note' is required",
                error="missing_note",
                data={"note": ""},
            )
        sink = resolve_ctx(ctx).note_checkpoint
        if sink is None:
            return ToolResult(
                success=False,
                output="error: this turn cannot keep a checkpoint",
                error="checkpoint_unavailable",
                data={"note": note},
            )
        sink(note)
        return ToolResult(success=True, output=f"checkpoint: {note}")
```

`core/turn_books.py`：

```python
#: Tools whose success is not evidence about the world (R8a D-14).
STATE_ONLY_TOOLS = frozenset({"checkpoint_write"})

...
    #: Successful calls that are evidence about the world (R8a D-14).
    evidence_calls: int = 0

...
        succeeded = [item for item in outcomes if item.result.success]
        self.successful_calls += len(succeeded)
        self.evidence_calls += sum(
            1 for item in succeeded if item.call.name not in STATE_ONLY_TOOLS
        )
```

`core/agent_loop.py` 第 475 行改成 `evidence="tool" if books.evidence_calls else "model"`。

`cli.py` 在 memory 工具注册处（第 447–456 行）加：

```python
        if registry.get("checkpoint_write") is None:
            from ai_coding.tools.checkpoint_tool import CheckpointTool

            registry.register(CheckpointTool())
```

`config/models.py` 在 `working_memory_chars` 旁边加：

```python
    # R8a: how many characters one model-written checkpoint line may keep.  The
    # ring pays for it only after a compaction (measured: 104 chars of fixed
    # overhead, 1096 per ring: docs/r8-analysis.md 3.1).
    working_memory_note_chars: int = 120
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_tool_checkpoint.py tests/test_turn_books.py tests/test_loop_digest.py tests/test_memory_turn_evidence.py tests/test_cli.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`

- [ ] **Step 5: 提交**

```bash
git add ai_coding/tools/checkpoint_tool.py ai_coding/core/turn_books.py ai_coding/core/agent_loop.py ai_coding/cli.py ai_coding/config/models.py tests/test_tool_checkpoint.py tests/test_turn_books.py tests/test_memory_turn_evidence.py
git commit -m "R8a-4: checkpoint_write, and a turn that only wrote one is not evidence"
```

---

### Task 8: Phase 0 spike（取消路径能不能用）

**Files:**
- Modify: `docs/r8-plan.md`（把结论写进「Spike 结论」一节）

**Interfaces:**
- Produces: 明确的 go / no-go：终端 Esc 是否交付；`killpg` 是否真能杀掉卡住的进程组

- [ ] **Step 1: 跑三个实验**

```bash
# ① 后台 reader 里 Esc 键绑定会不会注册成功、是否触发
.venv/bin/python - <<'PY'
import asyncio
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings

async def main() -> None:
    kb = KeyBindings()
    fired = asyncio.Event()

    @kb.add("escape")
    def _cancel(event) -> None:  # noqa: ANN001
        fired.set()

    session = PromptSession(key_bindings=kb)
    task = asyncio.create_task(session.prompt_async("try> "))
    await asyncio.sleep(0.3)
    print("escape bound:", bool(session.app.key_processor._bindings))
    task.cancel()

asyncio.run(main())
PY

# ② killpg 能不能真的把卡住的进程组清掉
.venv/bin/python - <<'PY'
import os, signal, subprocess, time

p = subprocess.Popen("sleep 30", shell=True, start_new_session=True)
time.sleep(0.2)
os.killpg(os.getpgid(p.pid), signal.SIGKILL)
time.sleep(0.2)
try:
    os.killpg(p.pid, 0)
    print("ALIVE")
except ProcessLookupError:
    print("DEAD")
PY
```

- [ ] **Step 2: 记录结论**

在本文档末尾加「## Spike 结论」一节，逐条写：① Esc 键绑定是否注册并触发；② `killpg` 之后
`ProcessLookupError` 是否出现；③ `patch_stdout` 下提示行是否还能用。
**任一条不过**：Task 13 只交付程序化取消 API，终端按键记回 backlog（并在结论里写明）。

- [ ] **Step 3: 提交**

```bash
git add docs/r8-plan.md
git commit -m "R8c-0: the cancel spike, measured"
```

---

### Task 9: runner 边跑边说（保住三条不变量）

**Files:**
- Modify: `ai_coding/core/events.py`、`ai_coding/infra/subprocess_runner.py`
- Test: `tests/test_subprocess_runner.py`（追加）

**Interfaces:**
- Produces: `ToolChunk(call_id, tool_name, stream, text, seq)`；
  `run_process(..., on_chunk=None, cancel=None)`；`run_process_async(..., on_chunk=None, cancel=None)`

- [ ] **Step 1: 写红测试**

```python
def test_r8_chunks_arrive_while_the_command_is_still_running():
    seen: list[tuple[str, str]] = []
    outcome = run_process(
        "for i in 1 2 3; do echo step-$i; sleep 0.1; done",
        shell=True,
        timeout=10,
        on_chunk=lambda stream, text: seen.append((stream, text)),
    )
    assert outcome.returncode == 0
    assert "".join(text for _, text in seen) == "step-1\nstep-2\nstep-3\n"
    assert all(stream == "stdout" for stream, _ in seen)


def test_r8_streaming_does_not_change_the_outcome():
    plain = run_process("echo hi && echo err >&2", shell=True, timeout=5)
    streamed = run_process(
        "echo hi && echo err >&2", shell=True, timeout=5, on_chunk=lambda *_: None
    )
    assert (streamed.stdout, streamed.stderr, streamed.returncode) == (
        plain.stdout,
        plain.stderr,
        plain.returncode,
    )


def test_r8_a_chunk_callback_that_raises_does_not_break_the_run():
    def boom(*_args: object) -> None:
        raise RuntimeError("observer")

    outcome = run_process("echo hi", shell=True, timeout=5, on_chunk=boom)
    assert outcome.returncode == 0
    assert outcome.stdout == "hi\n"


def test_r8_stdin_data_still_reaches_the_child():
    outcome = run_process("cat", shell=True, timeout=5, stdin_data="from-stdin\n")
    assert outcome.stdout == "from-stdin\n"


def test_r8_timeout_still_kills_the_group_and_keeps_partial_output():
    outcome = run_process("echo partial; sleep 30", shell=True, timeout=0.5)
    assert outcome.timed_out is True
    assert "partial" in outcome.stdout


def test_r8_async_variant_streams_too():
    seen: list[str] = []

    async def main() -> None:
        await run_process_async(
            "echo async-hi",
            shell=True,
            timeout=5,
            on_chunk=lambda _stream, text: seen.append(text),
        )

    asyncio.run(main())
    assert seen == ["async-hi\n"]
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_subprocess_runner.py -o addopts="" -q`
Expected: `TypeError: run_process() got an unexpected keyword argument 'on_chunk'`

- [ ] **Step 3: 最小实现**

`core/events.py`：加 `ToolChunk`（字段见 Interfaces）并加进 `LoopEvent` 联合：

```python
@dataclass(frozen=True)
class ToolChunk:
    """One streamed fragment of a running tool (R8c).

    Observation only: chunks never enter a request, the history or the session
    file - the model still only sees the final ``ToolResult`` (docs/r8-analysis.md 6.4).
    """

    call_id: str
    tool_name: str
    stream: str          # "stdout" | "stderr" | "progress"
    text: str
    seq: int = 0
```

`infra/subprocess_runner.py`：把 `communicate()` 换成「两个读线程 + 主线程等待 + 有界排空」。
关键片段（其余保持今天的分支与返回形态）：

```python
    if payload is not None:
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except OSError:
            pass

    buffers: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    lock = threading.Lock()

    def _reader(stream: Any, name: str) -> None:
        try:
            fd = stream.fileno()
            while True:
                data = os.read(fd, 4096)      # returns as soon as anything arrives
                if not data:
                    break
                with lock:
                    buffers[name].append(data)
                if on_chunk is not None:
                    try:
                        on_chunk(name, _decode(data))
                    except Exception:          # noqa: BLE001 - observation is optional
                        pass
        except Exception:                      # noqa: BLE001 - EOF / closed pipes
            pass

    readers = [
        threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for thread in readers:
        thread.start()

    timed_out = False
    deadline = started + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _kill_group(proc)
            break
        try:
            proc.wait(timeout=min(HEARTBEAT_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            continue

    for thread in readers:                     # bounded drain: never the blocker
        thread.join(timeout=kill_grace)
    if any(thread.is_alive() for thread in readers):
        _close_pipes(proc)
        for thread in readers:
            thread.join(timeout=0.5)
    with lock:
        stdout = _decode(b"".join(buffers["stdout"]))
        stderr = _decode(b"".join(buffers["stderr"]))
```

`run_process_async` 增加 `on_chunk` / `cancel` 两个参数，并把 chunk 交回事件循环：

```python
    loop = asyncio.get_running_loop()
    deliver = (
        None
        if on_chunk is None
        else (lambda stream, text: loop.call_soon_threadsafe(on_chunk, stream, text))
    )
    kwargs: dict[str, Any] = {..., "on_chunk": deliver, "cancel": cancel}
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_subprocess_runner.py -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: R2b 既有 11 条与新用例全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/core/events.py ai_coding/infra/subprocess_runner.py tests/test_subprocess_runner.py
git commit -m "R8c-1: the process runner can speak while it works"
```

---

### Task 10: bash 推流、控制台节流、MCP 进度

**Files:**
- Modify: `ai_coding/tools/bash_tool.py`、`ai_coding/ui/console.py`、`ai_coding/core/mcp.py`、`ai_coding/config/models.py`
- Test: `tests/test_tool_bash.py`、`tests/test_console_events.py`、`tests/test_mcp.py`（各追加）

**Interfaces:**
- Consumes: `ToolChunk`、`ToolContext.emit`、`ToolContext.call_id`
- Produces: `bash` → `ToolChunk`；`ConsoleObserver(console, *, clock=None)` 节流；MCP 进度 → `ToolChunk(stream="progress")`；`UIConfig.tool_chunks: bool = True`

- [ ] **Step 1: 写红测试**

```python
@pytest.mark.asyncio
async def test_r8_bash_emits_chunks_while_running():
    seen: list[object] = []
    result = await BashTool().execute(
        {"command": "for i in 1 2 3; do echo step-$i; sleep 0.05; done"},
        ToolContext(emit=seen.append, call_id="c1", workspace="."),
    )
    assert result.success is True
    chunks = [event for event in seen if isinstance(event, ToolChunk)]
    assert "".join(chunk.text for chunk in chunks) == "step-1\nstep-2\nstep-3\n"
    assert all(chunk.call_id == "c1" and chunk.tool_name == "bash" for chunk in chunks)


def test_r8_console_throttles_chunks_per_call():
    clock = [0.0]
    console = DialogueConsole(console=Console(record=True, width=200))
    observer = ConsoleObserver(console, clock=lambda: clock[0])
    observer.on_event(ToolChunk("c1", "bash", "stdout", "one\n", 0))
    observer.on_event(ToolChunk("c1", "bash", "stdout", "two\n", 1))
    clock[0] += 0.2
    observer.on_event(ToolChunk("c1", "bash", "stdout", "three\n", 2))
    text = console.export_text()
    assert "one" in text and "three" in text and "two" not in text
```

MCP 的用例用假 session（`call_tool` 里回调两次 `progress_callback`），断言收到
`[ToolChunk(stream="progress"), ToolChunk(stream="progress")]`。

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_tool_bash.py tests/test_console_events.py -o addopts="" -q`
Expected: FAIL（没有 chunk 事件）

- [ ] **Step 3: 最小实现**

`tools/bash_tool.py`：

```python
        counter = itertools.count()

        def _chunk(stream: str, text: str) -> None:
            live = resolve_ctx(ctx)
            live.emit(
                ToolChunk(
                    call_id=live.call_id,
                    tool_name="bash",
                    stream=stream,
                    text=text,
                    seq=next(counter),
                )
            )

        outcome = await run_process_async(..., on_chunk=_chunk)
```

`ui/console.py`：

```python
_CHUNK_THROTTLE_SECONDS = 0.15


class ConsoleObserver:
    def __init__(
        self, console: DialogueConsole, *, clock: Callable[[], float] | None = None
    ) -> None:
        self._c = console
        self._clock = clock or time.monotonic
        self._last_chunk: dict[str, float] = {}
...
        elif isinstance(event, ToolChunk):
            now = self._clock()
            if now - self._last_chunk.get(event.call_id, 0.0) < _CHUNK_THROTTLE_SECONDS:
                return
            self._last_chunk[event.call_id] = now
            self._c.info(f"  | {_short(event.text, _OUTPUT_PREVIEW)}")
```

并把 `ToolChunk` 加进 import 与 `_render_part` 的分支顺序里。`core/mcp.py` 的 `call_tool`：

```python
        counter = itertools.count()
        live = resolve_ctx(ctx)

        def _progress(
            progress: float, total: float | None = None, message: str | None = None
        ) -> None:
            body = message or (f"{progress}/{total}" if total else str(progress))
            live.emit(
                ToolChunk(
                    call_id=live.call_id,
                    tool_name=self.name,
                    stream="progress",
                    text=body,
                    seq=next(counter),
                )
            )

        result = await self._session.call_tool(
            tool_name, params, progress_callback=_progress
        )
```

`config/models.py::UIConfig`：

```python
    #: R8c: show a line per tool output fragment (throttled to 0.15s).  Off = the
    #: old "one preview when it finishes" behaviour.
    tool_chunks: bool = True
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_tool_bash.py tests/test_console_events.py tests/test_mcp.py tests/test_mcp_stdio.py -o addopts="" -q && .venv/bin/python -m mypy ai_coding`

- [ ] **Step 5: 提交**

```bash
git add ai_coding/tools/bash_tool.py ai_coding/ui/console.py ai_coding/core/mcp.py ai_coding/config/models.py tests/test_tool_bash.py tests/test_console_events.py tests/test_mcp.py
git commit -m "R8c-2: the console and MCP show progress while a tool runs"
```

---

### Task 11: `cancel=` 一路贯通

**Files:**
- Modify: `ai_coding/infra/deadline.py`、`ai_coding/core/dispatch.py`、`ai_coding/core/agent_loop.py`、`ai_coding/domain/message.py`、`ai_coding/tools/base.py`
- Test: `tests/test_deadline.py`、`tests/test_tool_dispatch.py`、`tests/test_agent_loop_react.py`（各追加）

**Interfaces:**
- Produces: `RunCancelled(label)`；`run_with_deadline(..., cancel=None)`；`CANCELLED_BY_USER`；
  `ToolDispatcher(..., cancel=None)`；`AgentLoop(..., cancel=None)`；`ToolContext.kill_event`

- [ ] **Step 1: 写红测试**

```python
@pytest.mark.asyncio
async def test_r8_a_set_cancel_event_abandons_the_await():
    cancel = asyncio.Event()

    async def slow() -> None:
        await asyncio.sleep(30)

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    with pytest.raises(RunCancelled):
        await asyncio.gather(
            run_with_deadline(slow(), timeout=None, label="tool:bash", cancel=cancel),
            trip(),
        )


@pytest.mark.asyncio
async def test_r8_a_cancelled_call_comes_back_structured():
    cancel = asyncio.Event()
    registry = ToolRegistry()
    registry.register(_NeverEndingTool())
    dispatcher = ToolDispatcher(registry, PermissionGate(), AutoApproveCallback(),
                                cancel=cancel)

    async def trip() -> None:
        await asyncio.sleep(0.05)
        cancel.set()

    outcomes = await asyncio.gather(
        dispatcher.dispatch_batch([_call("slow")], ToolContext()),
        trip(),
    )
    result = outcomes[0][0].result
    assert result.success is False
    assert result.error == CANCELLED_BY_USER


@pytest.mark.asyncio
async def test_r8_the_loop_clears_the_cancel_before_each_batch():
    ... 两批调用；第一批取消、第二批照常跑 ...
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_deadline.py tests/test_tool_dispatch.py -o addopts="" -q`
Expected: FAIL（没有 `cancel` / `RunCancelled` / `CANCELLED_BY_USER`）

- [ ] **Step 3: 最小实现**

`infra/deadline.py`：

```python
class RunCancelled(Exception):
    """The caller asked to stop waiting; the awaitable was abandoned, not awaited."""

    def __init__(self, label: str) -> None:
        super().__init__(f"{label} was cancelled")
        self.label = label


async def run_with_deadline(
    awaitable: Awaitable[_T],
    *,
    timeout: float | None,
    label: str,
    tick: float = HEARTBEAT_SECONDS,
    on_abandon: Callable[[asyncio.Task[Any]], None] | None = None,
    cancel: asyncio.Event | None = None,
) -> _T:
    task = asyncio.ensure_future(awaitable)
    cancel_task = None if cancel is None else asyncio.ensure_future(cancel.wait())
    ...
    try:
        if cancel_task is None and timeout is None:
            return await task
        waiter = {task} if cancel_task is None else {task, cancel_task}
        done, _ = await asyncio.wait(
            waiter, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_task is not None and cancel_task in done:
            _abandon(task, on_abandon)
            raise RunCancelled(label)
        if task not in done:
            _abandon(task, on_abandon)
            raise DeadlineExpired(label, timeout or 0.0)
        return task.result()
    finally:
        for helper in (ticker, cancel_task):
            if helper is not None and not helper.done():
                helper.cancel()
                helper.add_done_callback(_drain)
```

`domain/message.py`：

```python
#: ``ToolResult.error`` when the user cancelled this one call (R8c, D-7).
CANCELLED_BY_USER = "cancelled_by_user"
```

`tools/base.py` 的 `ToolContext` 加：

```python
    # R8c: a thread-safe "stop" flag for tools that own a subprocess (bash).  The
    # dispatcher sets it when the asyncio-level cancel fires, so the child dies
    # even though the awaiting task was abandoned (docs/r8-analysis.md 3.3-S2.1).
    kill_event: Any = None
```

`core/dispatch.py`：构造器加 `cancel: asyncio.Event | None = None`；`_execute` 里：

```python
        kill = threading.Event()
        ctx = replace(ctx, kill_event=kill)
        try:
            return await run_with_deadline(
                tool.execute(call.params, ctx),
                timeout=budget,
                label=f"tool:{call.name}",
                cancel=self.cancel,
            )
        except RunCancelled:
            kill.set()      # the child must die even though we stop waiting
            return self._cancelled(call)
        except DeadlineExpired:
            ...              # unchanged
```

以及 `_cancelled`：

```python
    @staticmethod
    def _cancelled(call: ToolCall) -> ToolResult:
        return ToolResult(
            success=False,
            output="cancelled_by_user: the user stopped this call; change approach or wrap up",
            error=CANCELLED_BY_USER,
            data={"tool": call.name},
        )
```

`AgentLoop(..., cancel: asyncio.Event | None = None)` 传给 `ToolDispatcher`；`_run_tools` 在
`dispatch_batch` 之前 `if self.cancel is not None: self.cancel.clear()`。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_deadline.py tests/test_tool_dispatch.py tests/test_agent_loop_react.py tests/test_dispatch_approval_timeout.py -o addopts="" -q && .venv/bin/python -m mypy ai_coding`

- [ ] **Step 5: 提交**

```bash
git add ai_coding/infra/deadline.py ai_coding/domain/message.py ai_coding/tools/base.py ai_coding/core/dispatch.py ai_coding/core/agent_loop.py tests/test_deadline.py tests/test_tool_dispatch.py tests/test_agent_loop_react.py
git commit -m "R8c-3: cancel one batch, and the model hears about it"
```

---

### Task 12: 取消真的杀进程；取消不算失败

**Files:**
- Modify: `ai_coding/infra/subprocess_runner.py`、`ai_coding/tools/bash_tool.py`、`ai_coding/core/turn_books.py`
- Test: `tests/test_subprocess_runner.py`、`tests/test_turn_books.py`（各追加）

**Interfaces:**
- Produces: `ProcessOutcome.cancelled: bool`；`bash` 把 `ctx.kill_event` 交给 runner；取消不进失败计数

- [ ] **Step 1: 写红测试**

```python
def test_r8_cancelling_really_kills_the_process_group(tmp_path):
    pid_file = tmp_path / "pid"
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    outcome = run_process(
        f"echo $$ > {pid_file}; sleep 30", shell=True, timeout=30, cancel=cancel
    )
    assert outcome.cancelled is True
    assert outcome.timed_out is False
    pid = int(pid_file.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_r8_a_batch_that_was_only_cancelled_is_not_a_failure():
    books = TurnBooks()
    books.note_outcomes([_outcome("bash", success=False, error=CANCELLED_BY_USER)])
    assert books.failed_streak == 0
    assert books.failures == []


def test_r8_a_cancelled_call_does_not_hide_a_real_failure():
    books = TurnBooks()
    books.note_outcomes(
        [
            _outcome("bash", success=False, error=CANCELLED_BY_USER),
            _outcome("bash", success=False, error="timeout"),
        ]
    )
    assert books.failed_streak == 1
    assert [tool for tool, _reason, _preview in books.failures] == ["bash"]
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_subprocess_runner.py tests/test_turn_books.py -o addopts="" -q`
Expected: FAIL（`ProcessOutcome` 没有 `cancelled`；取消被计成失败）

- [ ] **Step 3: 最小实现**

`infra/subprocess_runner.py`：`ProcessOutcome` 加 `cancelled: bool = False`；等待循环里在超时判断之前：

```python
        if cancel is not None and cancel.is_set():
            cancelled = True
            _kill_group(proc)
            break
```

返回时带 `cancelled=cancelled`。`tools/bash_tool.py` 把 `ctx.kill_event` 传下去：

```python
        outcome = await run_process_async(
            ...,
            on_chunk=_chunk,
            cancel=resolve_ctx(ctx).kill_event,
        )
```

`core/turn_books.py::note_outcomes`：

```python
        counted = [item for item in outcomes if item.result.error != CANCELLED_BY_USER]
        if not counted:
            return          # only-cancelled batches are not evidence either way
        ...
        self.failed_streak = 0 if succeeded else self.failed_streak + 1
        self.failures.extend(
            (item.call.name, str(item.result.error or "failed"),
             (item.result.output or "")[:_FAILURE_EXCERPT])
            for item in counted
            if not item.result.success
        )
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_subprocess_runner.py tests/test_turn_books.py tests/test_tool_bash.py -o addopts="" -q`

- [ ] **Step 5: 提交**

```bash
git add ai_coding/infra/subprocess_runner.py ai_coding/tools/bash_tool.py ai_coding/core/turn_books.py tests/test_subprocess_runner.py tests/test_turn_books.py
git commit -m "R8c-4: Esc stops the process, and a cancel is not a failure"
```

---

### Task 13: Esc 键（含门控真终端用例）

**Files:**
- Modify: `ai_coding/ui/reader.py`、`ai_coding/cli.py`
- Test: `tests/test_ui_reader.py`、`tests/test_repl_cancel_pty.py`（新建，门控 `AI_CODING_PTY=1`）
- Create: `scripts/mcp_progress_server.py`、`tests/test_mcp_progress.py`（门控 `AI_CODING_MCP_STDIO=1`）

**Interfaces:**
- Produces: `InteractiveReader(..., cancel_key="escape")`、`InteractiveReader.cancel_requested`、`request_cancel()`

- [ ] **Step 1: 写红测试**

```python
@pytest.mark.asyncio
async def test_r8_escape_sets_the_cancel_event():
    reader = InteractiveReader()
    assert not reader.cancel_requested.is_set()
    reader.request_cancel()
    assert reader.cancel_requested.is_set()


def test_r8_the_reader_exposes_an_escape_binding():
    reader = InteractiveReader()
    assert reader.key_bindings() is not None
```

真终端的门控用例（`tests/test_repl_cancel_pty.py`）：起一个真实 pty，跑一条 `sleep 30` 的 bash，
按 Esc，断言 ① 回合继续、② 工具结果里出现 `cancelled_by_user`、③ `sleep` 的进程组已经不在。
MCP 进度门控用例（`tests/test_mcp_progress.py`）用 `scripts/mcp_progress_server.py`，断言 3 次
进度以 `ToolChunk(stream="progress")` 到达。

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_ui_reader.py -o addopts="" -q`
Expected: FAIL（没有 `request_cancel`）

- [ ] **Step 3: 最小实现**

`ui/reader.py`：

```python
    def __init__(self, ..., cancel_key: str = "escape") -> None:
        ...
        #: R8c: the turn watches this; the Esc key sets it.
        self.cancel_requested = asyncio.Event()
        self._cancel_key = cancel_key

    def request_cancel(self) -> None:
        """Ask the running turn to drop this batch (the Esc key calls this)."""
        self.cancel_requested.set()

    def key_bindings(self) -> Any:
        from prompt_toolkit.key_binding import KeyBindings

        bindings = KeyBindings()

        @bindings.add(self._cancel_key)
        def _cancel(event: Any) -> None:  # noqa: ARG001 - the key needs the event arg
            self.request_cancel()

        return bindings
```

`for_terminal()` 里 `PromptSession(key_bindings=cls(...).key_bindings())` 的装配改为在实例上完成
（先建实例、再把 `key_bindings()` 交给 `PromptSession`）。`cli.py` 把 `reader.cancel_requested`
交给 `AgentLoop(cancel=...)`。

- [ ] **Step 4: 跑绿 + 门控**

Run: `.venv/bin/python -m pytest tests/test_ui_reader.py -o addopts="" -q`
Run: `AI_CODING_PTY=1 .venv/bin/python -m pytest tests/test_repl_cancel_pty.py -o addopts="" -q`
Run: `AI_CODING_MCP_STDIO=1 .venv/bin/python -m pytest tests/test_mcp_progress.py -o addopts="" -q`
Expected: PASS（CI 里跳过门控时要在 `docs/integration_testing.md` 写明原因：需要真 tty / 允许 spawn 子进程）

- [ ] **Step 5: 提交**

```bash
git add ai_coding/ui/reader.py ai_coding/cli.py scripts/mcp_progress_server.py tests/test_ui_reader.py tests/test_repl_cancel_pty.py tests/test_mcp_progress.py
git commit -m "R8c-5: Esc asks the running turn to drop this batch"
```

---

### Task 14: 验收脚本 + 六份文档

**Files:**
- Create: `scripts/r8_cases.py`
- Modify: `docs/features.md`、`docs/data-contracts.md`、`docs/architecture.md`、`docs/how-to-run.md`、`docs/integration_testing.md`、`docs/r7-backlog.md`
- Modify: `docs/r8-plan.md`（把本文件的 `- [ ]` 全部勾掉，并加「执行状态」一节）

**Interfaces:**
- Produces: `scripts/r8_cases.py`（离线 12 + `--real` 3）

- [ ] **Step 1: 写验收脚本**

沿用 `scripts/r7e_cases.py` 的形状（一个 case 一个函数，返回 PASS/FAIL 与一行理由）。离线 12 条：
tie-break、`evidence` 过滤、digest note 渲染、写回历史、`evidence` 排除 checkpoint、chunk 顺序、
`stdin_data` 路径、退出后排空、取消杀进程组、取消不计失败、控制台节流、开关 chunk 的请求逐字节相同。
`--real` 3 条（需要 `config.local.yaml`）是行为观察，不做硬断言。

- [ ] **Step 2: 跑验收**

Run: `.venv/bin/python scripts/r8_cases.py`
Run: `.venv/bin/python scripts/r8_cases.py --real --config config.local.yaml`
Expected: 离线 12/12；真实 3/3

- [ ] **Step 3: 更新六份文档 + backlog**

按 `docs/r8-analysis.md` §12 的清单逐条改：`features.md`（回合摘要环 + 记忆 + 工具）、
`data-contracts.md`（本节 Global Constraints 列出的全部新接口）、`architecture.md`（包结构与三条新边）、
`how-to-run.md`（Esc 会真的杀命令、流式开关、checkpoint 与 memory_write 的分工）、
`integration_testing.md`（`scripts/r8_cases.py` 与两条门控用例）、`r7-backlog.md`（2.2 / 2.5 / 3.6 划掉）。

- [ ] **Step 4: 全量门禁**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿；测试总数从 1010 起只增不减

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "R8: the acceptance script, and the docs the round changed"
```

---

## Self-Review

**1. Spec coverage**

| Spec（`docs/r8-analysis.md`） | Task |
|---|---|
| §3.1 R8a 契约（字段、工具、接线、上限） | 4, 5, 6, 7 |
| §3.1.1 影响面（写回历史、`evidence` 连带、时间事实） | 6, 7 |
| §3.2 R8b（tie-break + 过滤 + 合并钉子） | 1, 2, 3 |
| §3.3-S1 流式（读线程 / `stdin_data` / 排空 / 截断口径） | 9, 10 |
| §3.3-S2 / S2.1 / S2.2（取消、杀进程、不计失败） | 11, 12 |
| §3.3-S3 MCP 进度 | 10, 13（真 stdio 用例） |
| §6.2 层 × 切片矩阵与 §6.4 不变量 | 9, 10, 11, 12 |
| §7 的 47 条用例 | 1–13（逐条落在上面各 Task 的 Step 1） |
| §11 的 D-1…D-14 | Global Constraints（全部按推荐值锁定） |
| §12 文档同步清单 | 14 |

**2. Placeholder scan**：无 TBD / TODO；每个 Step 都有可执行的测试代码、实现代码或命令。

**3. Type consistency**：`evidence_rank`、`DEFAULT_NOTE_CHARS`、`TurnDigest.note/.source/.display()`、
`build_digest(..., model_note, note_chars)`、`TurnBooks.checkpoint/.note_checkpoint/.evidence_calls`、
`ToolContext.note_checkpoint/.kill_event`、`ToolChunk`、`ProcessOutcome.cancelled`、
`run_process(..., on_chunk, cancel)`、`RunCancelled`、`CANCELLED_BY_USER` 在 Task 1–13 中的引用
与首次定义完全一致；`_run_tools(..., books=...)` 的调用点与签名在 Task 6/11 中同步更新。

## Execution Handoff

按用户指令走 **Inline Execution**（「不准派子任务，自己实现，或者就用主模型」）：本会话按
Task 1 → 14 顺序执行，每 Task 跑门禁并单独提交；任何需要改动既有测试断言的时刻先停下来核对原因。

---

## Spike 结论（Task 8 实跑，2026-09-28）

三个问题都是**真跑出来的**，不是推断：

**① Esc 键绑定：go，而且比预想的更好。**
`prompt_toolkit` 的 `KeyBindings.add("escape")` 正常；`KeyBindings.get_bindings_for_keys((Keys.Escape,))`
存在，可以拿来做单元级断言。更重要的是：把 `KeyPress(Keys.Escape)` 喂进 `session.app.key_processor`
**可以在一个正在运行的 `prompt_async(...)` 里触发处理器**（实测 `handler fired: True`）——也就是说
Task 13 不必只依赖真终端：可以有一条**非门控**的测试，真正把 Esc 送到 reader 的按键处理器上。

**② `killpg`：go，但判据有一个坑。**
`os.killpg(pgid, SIGKILL)` 对 `start_new_session=True` 起出来的进程组有效，孙进程一起死
（`sleep 30 & sleep 30` 实测 `DEAD`）。坑在于：**必须先把子进程 reap 掉再判断**——没有 `p.wait()` 时，
被杀的子进程还是僵尸，`os.killpg(pgid, 0)` 仍然成功，于是看起来「还活着」。所以 Task 12 的判据用例
写成「`run_process` 返回之后（它内部已经 `proc.wait()`）再 `killpg(pid, 0)` 抛 `ProcessLookupError`」。

**③ 排空与 `patch_stdout`：沿用 R7e 的既有结论。**
`patch_stdout` 的用法在 `ui/reader.py::for_terminal` 里已经上线（R7e-2），本机 `AI_CODING_PTY=1` 跑
`tests/test_repl_ask_user_pty.py tests/test_ui_reader.py` = **7 passed**，说明真终端路径在这个环境里是
可测的。另外实测：一个**阻塞在 `communicate()` 里的线程**，在 `killpg` 之后会立刻返回并带回已经读到的
部分输出（`b'first'`）——这正是 Task 9「杀完再排空」的设计依据。

**结论**：Esc 交付（含非门控的按键测试），取消路径按 D-10 真的杀进程组，两条门控用例
（真 pty、真 stdio）也保留。
