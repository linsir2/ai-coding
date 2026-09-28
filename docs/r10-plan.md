# R10 Implementation Plan（grep 式按需检索 + 可选语义加速）

> **For agentic workers:** REQUIRED SUB-SKILL：本项目按用户既有要求走 **Inline Execution**
> （「不准派子任务，自己实现」）。逐步执行时用 `- [ ]` 勾选跟踪；每个 Task 结束跑一次质量门禁并单独提交。

**Goal:** 让「搜记忆」变成 Claude Code 那套可迭代的检索——词面精确、失败可行动、命中可解释；
语义（embeddings）作为**可选**加速器，缺了/坏了/没配，功能与结果一切照常。

**Architecture:** 打分从「可加性总分过阈值」改成「**BM25 排序 → 绝对相关度地板 → 相对带宽**」，
相关性从「和 recency/importance 相加」变成「门」；`memory_search` 的返回从一张笔记列表
升级成**结构化诊断**（命中 / 未命中的词 / 未经证实的最接近候选 / 有界索引地图）；
可选的 `EmbeddingIndex` 只挂在 `memory_search` 这一条路径上，任何失败都退回词面并打标记。
**轨迹/会话/注入位置/缓存纪律一个字都不改。**

**Tech Stack:** Python 3.13（mypy 按 3.10 下限）、标准库 `urllib`（不用 numpy、不装向量库）、
dataclasses、pytest（`-o addopts=""`）、ruff、mypy。**不新增任何依赖。**

**Spec:** `docs/r10-analysis.md`（提交 `109b028`，更正 `5a031bd`）。所有实测数字、被否掉的候选、
外部对标都在那份文档里；本计划只写「怎么做、怎么测」，不重复论证。

## Global Constraints

用户已拍板（「决策点都按照你推荐的来」），实现时**不要改主意**：

- **D-R10-1**：评测基准进仓库（`scripts/retrieval_cases.py` + 数据文件），离线 < 2s，
  护栏设**宽松**（R@3 ≥ 0.80、paraphrase ≥ 6/12），防止有人把门禁调到什么都不返回。
- **D-R10-2**：`min_relevance` 默认 0.05 → **0.25**（`RecallParams` 与 `MemoryConfig` 两处），
  显式写了 0.05 的老配置行为不变。
- **D-R10-3**：引入**相对带宽** `relative_band = 0.55`（`score >= 0.55 × 本轮最高分`）。
- **D-R10-4**：召回**命中时**更新 `hits/last_used`（一次批量写盘；未命中不写）。
- **D-R10-5**：recency 默认仍用 `created_time`；`accessed` 只是开关（本轮不开）。
- **D-R10-6**：**不新增工具**（不加 `memory_get` / `memory_list`）；`memory_search` 只加参数。
- **D-R10-7**：embeddings **默认关**，必须显式配 `provider`。
- **D-R10-8**：语义分作为**绝对门的 OR 分支**（`coverage >= floor or cos >= min_cosine`）
  并 0.5/0.5 融入排序。
- **D-R10-9**：写时关键词富化**本轮不做**（P2）。
- **D-R10-10**：**不做** SQLite / FTS5（触发条件：笔记 > 2000 条、或 p95 > 100ms、或多进程并发写）。

工程质量与流程约束（与前几轮一致）：

- 质量门禁：`pytest tests/ -o addopts="" -q`（R9 结束时 **1122 passed / 4 skipped**）、
  `ruff check .`、`mypy ai_coding`。
- **既有测试断言原则上一条都不改**。本轮有**且仅有一处**例外，已单独列出并需在提交信息里说明：
  `tests/test_tool_memory_search.py::test_no_hits_is_a_successful_empty_answer` 断言
  `result.data == {"notes": []}`（整字典相等）——R10c 的目的就是让这个 payload 携带诊断，
  所以它改成 `result.data["notes"] == []` **加上** `result.data["no_match"] is True`。
  这不是「测试写错了」，是它钉的属性被本轮**有意扩展**。
- TDD：先写红测试 → 跑红 → 最小实现 → 跑绿 → 提交。一个 Task 一个提交。
- 提交信息不含反引号；多行信息用 `git commit -F <file>`。
- 不改：`<memory-constraints>` 的选取规则、记忆的注入位置与缓存纪律、harvest 游标语义、
  `memory_write` 的写路径、`consolidate` 的 `_better_of` 判定、轨迹/会话文件格式。

新增公开接口（后续 Task 的名字/签名以此为准）：
`memory.benchmark.EvalCase` / `Metrics` / `evaluate()` / `format_metrics()` / `load_corpus()`；
`memory.relevance.bm25_scores()` / `RankingResult` / `rank_notes_detailed()`、
`ScoredNote.matched_terms`、`RecallParams.relative_band` / `weight_rank` / `bm25_k1` / `bm25_b` / `recency_source`；
`memory.store.MemoryStore.touch_notes()`；
`memory.service.RecallDiagnostics` / `IndexSummary` / `MemoryService.recall_with_diagnostics()`；
`memory.embeddings.EmbeddingIndex`；`config.models.EmbeddingsConfig`。

## File Structure

| 文件 | 责任 | Task |
|---|---|---|
| `ai_coding/memory/benchmark.py`（新） | 纯评测原语：语料装载、recall@k/MRR/FP 计算、表格格式化 | 1 |
| `scripts/retrieval_corpus.json`（新） | 52 条语料 + 46 条标注问法（数据，不是逻辑） | 1 |
| `scripts/retrieval_cases.py`（新） | 基准脚本：加载语料 → 跑当前实现 → 打印分类指标 | 1 |
| `ai_coding/memory/relevance.py` | BM25 排序 + 两段门禁 + `matched_terms` + `RankingResult` | 2 |
| `ai_coding/config/models.py` | `recall_min_relevance` 默认值、`recall_relative_band`、`EmbeddingsConfig` | 2, 6 |
| `ai_coding/memory/store.py` | `touch_notes()` | 3 |
| `ai_coding/memory/service.py` | 命中即 touch、`recall_with_diagnostics()`、过滤与索引地图、可选语义 | 3, 4, 5, 7 |
| `ai_coding/tools/memory_search_tool.py` | 结构化诊断、可行动的 no_match 文案、新参数 | 4, 5 |
| `ai_coding/memory/embeddings.py`（新） | 可选的向量索引（标准库 urllib + 磁盘缓存 + 纯 Python 余弦） | 6 |
| `ai_coding/tools/builder.py`、`ai_coding/cli.py` | 组合根装配 `EmbeddingIndex` | 7 |
| `scripts/r10_cases.py`（新） | 离线验收脚本 | 8 |

---

### Task 1: 先有尺子——评测原语与真实语料

**Files:**
- Create: `ai_coding/memory/benchmark.py`
- Create: `scripts/retrieval_corpus.json`
- Create: `scripts/retrieval_cases.py`
- Test: `tests/test_retrieval_benchmark.py`（新建，5 条）

**Interfaces:**
- Produces: `EvalCase(category, query, relevant)`；
  `Metrics(answerable, no_answer, recall, mrr, false_positive_share, no_answer_ok, missed, spurious)`
  + `Metrics.recall_at(k)`；`evaluate(rank, cases, *, ks=(1,3,5), fp_k=5) -> Metrics`；
  `format_metrics(name, metrics) -> str`；
  `load_corpus(path, *, now) -> tuple[list[MemoryNote], list[EvalCase]]`
- Consumes: `ai_coding.memory.note.MemoryNote` / `NoteKind`

- [ ] **Step 1: 写红测试**

```python
"""TDD (R10a): retrieval evaluation — pure maths plus one well-formed corpus."""

import json
from datetime import datetime, timezone
from pathlib import Path

from ai_coding.memory.benchmark import EvalCase, evaluate, format_metrics, load_corpus

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _corpus(tmp_path: Path, notes: list[dict], queries: list[dict]) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps({"notes": notes, "queries": queries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_r10a_load_corpus_builds_notes_and_cases(tmp_path):
    path = _corpus(
        tmp_path,
        [{"id": "n1", "kind": "decision", "importance": 4, "age_days": 7, "text": "用 uv"}],
        [{"category": "lexical", "query": "依赖", "relevant": ["n1"]}],
    )

    notes, cases = load_corpus(path, now=NOW)

    assert [n.id for n in notes] == ["n1"]
    assert notes[0].kind.value == "decision" and notes[0].importance == 4
    assert notes[0].created_time == "2026-09-21T00:00:00+00:00"  # age_days applied
    assert cases == [EvalCase("lexical", "依赖", frozenset({"n1"}))]


def test_r10a_recall_mrr_and_false_positive_share():
    cases = [
        EvalCase("lexical", "q1", frozenset({"a"})),
        EvalCase("lexical", "q2", frozenset({"b"})),
        EvalCase("lexical", "q3", frozenset({"c"})),
    ]
    ranking = {"q1": ["a", "x", "y"], "q2": ["x", "b"], "q3": ["x", "y", "z"]}

    metrics = evaluate(lambda query: ranking[query], cases)

    assert metrics.answerable == 3
    assert metrics.recall_at(1) == 1 / 3
    assert metrics.recall_at(3) == 2 / 3
    assert metrics.mrr == (1 + 1 / 2) / 3
    # q1: 2 of 3 returned are junk, q2: 1 of 3, q3: 3 of 3
    assert round(metrics.false_positive_share, 4) == round((2 / 3 + 1 / 3 + 3 / 3) / 3, 4)
    assert metrics.missed == ("q3",)


def test_r10a_no_answer_cases_count_as_ok_only_when_empty():
    cases = [
        EvalCase("noanswer", "q1", frozenset()),
        EvalCase("trap", "q2", frozenset()),
    ]
    metrics = evaluate(lambda query: [] if query == "q1" else ["z"], cases)

    assert metrics.answerable == 0
    assert metrics.no_answer == 2
    assert metrics.no_answer_ok == 1
    assert metrics.spurious == ("q2",)


def test_r10a_unknown_ids_in_a_ranking_are_ignored():
    cases = [EvalCase("lexical", "q", frozenset({"a"}))]
    metrics = evaluate(lambda query: ["ghost", "a"], cases)
    assert metrics.recall_at(1) == 0.0
    assert metrics.recall_at(3) == 1.0


def test_r10a_the_shipped_corpus_is_well_formed():
    root = Path(__file__).resolve().parent.parent
    notes, cases = load_corpus(root / "scripts" / "retrieval_corpus.json", now=NOW)

    ids = {note.id for note in notes}
    assert len(ids) == 52
    assert len(cases) == 46
    assert {case.category for case in cases} == {
        "lexical", "identifier", "paraphrase", "multi", "noanswer", "trap",
    }
    for case in cases:
        assert case.relevant <= ids, f"{case.query!r} points at an unknown id"
    assert sum(1 for case in cases if not case.relevant) == 6
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_retrieval_benchmark.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_coding.memory.benchmark'`

- [ ] **Step 3: 最小实现**

`ai_coding/memory/benchmark.py`：

```python
"""Retrieval evaluation primitives (R10a).

The yardstick for every scoring change in R10: labelled (query, relevant ids)
cases in, recall@k / MRR / false-positive share out.  Pure and offline — the
corpus lives in ``scripts/retrieval_corpus.json``, which is data, not logic.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ai_coding.memory.note import MemoryNote, NoteKind

__all__ = ["EvalCase", "Metrics", "Ranker", "evaluate", "format_metrics", "load_corpus"]

Ranker = Callable[[str], Sequence[str]]


@dataclass(frozen=True)
class EvalCase:
    """One labelled query.  ``relevant`` empty means "nothing should answer this"."""

    category: str
    query: str
    relevant: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Metrics:
    answerable: int = 0
    no_answer: int = 0
    recall: tuple[tuple[int, float], ...] = ()
    mrr: float = 0.0
    false_positive_share: float = 0.0
    no_answer_ok: int = 0
    missed: tuple[str, ...] = ()
    spurious: tuple[str, ...] = ()

    def recall_at(self, k: int) -> float:
        for size, value in self.recall:
            if size == k:
                return value
        return 0.0


def evaluate(
    rank: Ranker,
    cases: Sequence[EvalCase],
    *,
    ks: Sequence[int] = (1, 3, 5),
    fp_k: int = 5,
) -> Metrics:
    """Score one ranking function.  A ranker that raises counts as "returned nothing"."""
    depth = max(ks)
    hits = {k: 0 for k in ks}
    ranks: list[int] = []
    fp_shares: list[float] = []
    missed: list[str] = []
    spurious: list[str] = []
    answerable = no_answer = 0
    for case in cases:
        try:
            returned = list(rank(case.query))[:depth] if case.relevant else list(rank(case.query))[:fp_k]
        except Exception:  # noqa: BLE001 — a broken ranker must not kill the harness
            returned = []
        if not case.relevant:
            no_answer += 1
            if returned:
                spurious.append(case.query)
            continue
        answerable += 1
        rank_of = next((i + 1 for i, note_id in enumerate(returned) if note_id in case.relevant), 0)
        ranks.append(rank_of)
        for k in ks:
            if 0 < rank_of <= k:
                hits[k] += 1
        if rank_of == 0:
            missed.append(case.query)
        top = list(rank(case.query))[:fp_k]
        if top:
            fp_shares.append(sum(1 for note_id in top if note_id not in case.relevant) / len(top))
    denominator = answerable or 1
    return Metrics(
        answerable=answerable,
        no_answer=no_answer,
        recall=tuple((k, hits[k] / denominator) for k in ks),
        mrr=sum(1 / r for r in ranks if r) / denominator,
        false_positive_share=(sum(fp_shares) / len(fp_shares)) if fp_shares else 0.0,
        no_answer_ok=no_answer - len(spurious),
        missed=tuple(missed),
        spurious=tuple(spurious),
    )


def format_metrics(name: str, metrics: Metrics) -> str:
    """One table row (used by the benchmark script and the acceptance script)."""
    parts = " ".join(f"R@{k}={metrics.recall_at(k):.2f}" for k, _ in metrics.recall)
    return (
        f"{name:22} {parts} MRR={metrics.mrr:.2f} "
        f"FP@5={metrics.false_positive_share:.2f} "
        f"no-answer={metrics.no_answer_ok}/{metrics.no_answer}"
    )


def load_corpus(
    path: str | Path, *, now: datetime
) -> tuple[list[MemoryNote], list[EvalCase]]:
    """Read the JSON corpus into notes (ages applied) and labelled cases."""
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    notes: list[MemoryNote] = []
    for entry in data.get("notes", []):
        created = (now - timedelta(days=int(entry.get("age_days", 0)))).isoformat()
        notes.append(
            MemoryNote(
                id=str(entry["id"]),
                text=str(entry["text"]),
                kind=NoteKind.parse(entry.get("kind", "fact")),
                importance=int(entry.get("importance", 3)),
                created_time=created,
                updated_time=created,
            )
        )
    cases = [
        EvalCase(
            category=str(entry.get("category", "lexical")),
            query=str(entry["query"]),
            relevant=frozenset(str(i) for i in entry.get("relevant", [])),
        )
        for entry in data.get("queries", [])
    ]
    return notes, cases
```

`scripts/retrieval_corpus.json`（**52 条语料 + 46 条问法**，就是 `docs/r10-analysis.md` §1.4 实测用的那一套；
`age_days` 让 recency 可复现）：

```json
{
  "notes": [
    {"id":"n01","kind":"decision","importance":4,"age_days":30,"text":"项目依赖统一用 uv 管理，不引入 pip 或 poetry；虚拟环境在 .venv。"},
    {"id":"n02","kind":"preference","importance":4,"age_days":20,"text":"跑测试必须用 .venv/bin/python -m pytest tests/ -o addopts=\"\" -q，直接 pytest 会带上覆盖率参数变慢。"},
    {"id":"n03","kind":"decision","importance":4,"age_days":25,"text":"模型后端用 dashscope 的 compatible-mode（qwen），配置写在 config.local.yaml，key 不进 git。"},
    {"id":"n04","kind":"fact","importance":3,"age_days":40,"text":"AI_CODING_PTY=1 和 AI_CODING_MCP_STDIO=1 是两条门控环境变量，用来开真 pty 与真 MCP stdio 用例。"},
    {"id":"n05","kind":"decision","importance":3,"age_days":18,"text":"提交信息里不要出现反引号，多行信息用 git commit -F 读文件。"},
    {"id":"n06","kind":"fact","importance":5,"age_days":60,"text":"提示词缓存的关键是前缀稳定：系统提示与历史保持字节一致，易失内容只能追加在请求尾部。"},
    {"id":"n07","kind":"decision","importance":4,"age_days":22,"text":"记忆只有两个注入面：常驻的 memory-constraints 块（pinned 与 decision），和按需调用的 memory_search 工具。"},
    {"id":"n08","kind":"fact","importance":3,"age_days":15,"text":"轨迹每回合写一个 turn-N.jsonl，一行一个 span，字段是 v/ts/session/turn/span/parent/iter/evt/data。"},
    {"id":"n09","kind":"preference","importance":5,"age_days":12,"text":"回答用户时不要和稀泥，必须给出明确决策和理由。"},
    {"id":"n10","kind":"fact","importance":3,"age_days":35,"text":"bash 工具只在 Linux 上验证过，取消时要 killpg 加 reap 才杀得干净。"},
    {"id":"n11","kind":"decision","importance":5,"age_days":50,"text":"不要引入 agent 框架，循环由项目自己维护，只依赖 openai 与 mcp 两个库。"},
    {"id":"n12","kind":"fact","importance":3,"age_days":28,"text":"子智能体默认继承父上下文（subagent_context_mode=full），task_only 时给它一个干净窗口。"},
    {"id":"n13","kind":"fact","importance":4,"age_days":33,"text":"上下文压缩有四层：L3 大结果落盘、L1 裁剪中段、L2 老工具结果换信封、L4 用模型摘要。"},
    {"id":"n14","kind":"decision","importance":3,"age_days":26,"text":"记忆存储是单文件 JSON（.memory），不是 SQLite。"},
    {"id":"n15","kind":"open_thread","importance":2,"age_days":10,"text":"还没决定要不要把记忆迁移到 SQLite 加 FTS5。"},
    {"id":"n16","kind":"preference","importance":3,"age_days":16,"text":"文档要跟着代码改，features/data-contracts/architecture 等六份每轮同步。"},
    {"id":"n17","kind":"fact","importance":3,"age_days":45,"text":"CI 用的 Python 是 3.13，但 mypy 按 3.10 下限检查。"},
    {"id":"n18","kind":"decision","importance":3,"age_days":21,"text":"MCP 远端工具的审批用 servers[].auto_approve 豁免，默认仍然要问用户。"},
    {"id":"n19","kind":"fact","importance":2,"age_days":24,"text":"工作目录里的 .ai-coding/traces 是轨迹目录，sessions/ 是会话目录。"},
    {"id":"n20","kind":"preference","importance":5,"age_days":5,"text":"改代码要 TDD：先写红测试再实现，不准改既有断言。"},
    {"id":"n21","kind":"fact","importance":3,"age_days":14,"text":"依赖门禁由 ai.enforce_dependencies 开关控制，默认开；关掉就回到 R1 的宽松行为。"},
    {"id":"n22","kind":"fact","importance":3,"age_days":19,"text":"memory_search 返回的 note 会作为 tool 结果追加进历史，从那一刻起就被缓存。"},
    {"id":"n23","kind":"decision","importance":3,"age_days":17,"text":"决定不装 OpenTelemetry SDK，只导出 OTLP/JSON 文件。"},
    {"id":"n24","kind":"fact","importance":2,"age_days":29,"text":"工具面的 token 预算告警阈值是 tools_chars，超过会每个 loop 提示一次。"},
    {"id":"n25","kind":"fact","importance":3,"age_days":55,"text":"用户偏好中文交流，技术术语可以保留英文原词。"},
    {"id":"n26","kind":"preference","importance":2,"age_days":31,"text":"允许联网搜索，允许使用 skill。"},
    {"id":"n27","kind":"fact","importance":2,"age_days":42,"text":"项目路径是 /home/linsir365/projects/ai-coding。"},
    {"id":"n28","kind":"decision","importance":2,"age_days":23,"text":"会话文件 sessions/*.json 落盘是 camelCase，Python 侧是 snake_case，靠 loader 归一化。"},
    {"id":"n29","kind":"fact","importance":3,"age_days":13,"text":"checkpoint_write 写的一句话只进本会话的摘要环，跨会话记东西要用 memory_write。"},
    {"id":"n30","kind":"fact","importance":3,"age_days":11,"text":"收割游标 harvested_messages 存在 session 里，收割失败时不能推进。"},
    {"id":"n31","kind":"fact","importance":4,"age_days":9,"text":"召回打分等于 0.55 乘相关性加 0.20 乘新近度加 0.15 乘重要度，再加 kind 加成，阈值 0.25。"},
    {"id":"n32","kind":"fact","importance":3,"age_days":34,"text":"相关性用的是 idf 加权覆盖度，不是 BM25。"},
    {"id":"n33","kind":"decision","importance":4,"age_days":36,"text":"不搞向量检索和 embedding（即 RAG），relevance.py 留了 RecallScorer 接缝。"},
    {"id":"n34","kind":"fact","importance":3,"age_days":27,"text":"中文分词用字符 bigram，英文用词加一份停用词表。"},
    {"id":"n35","kind":"preference","importance":3,"age_days":44,"text":"不喜欢过度设计，坚持 YAGNI。"},
    {"id":"n36","kind":"fact","importance":4,"age_days":8,"text":"每次都要在真实环境跑一遍，不能只跑单元测试。"},
    {"id":"n37","kind":"fact","importance":3,"age_days":20,"text":"验收脚本放在 scripts 目录下，形如 rN_cases.py，分离线与 --real 两种。"},
    {"id":"n38","kind":"fact","importance":2,"age_days":28,"text":"记忆的淘汰上限是 max_index_entries，默认 200 条。"},
    {"id":"n39","kind":"decision","importance":4,"age_days":32,"text":"钩子是 fail-open，门卫是 fail-closed，两者语义不能混。"},
    {"id":"n40","kind":"fact","importance":2,"age_days":26,"text":"审批超时默认 0 秒，表示只受回合时钟约束。"},
    {"id":"n41","kind":"fact","importance":3,"age_days":25,"text":"子智能体不能再起子智能体，它的工具栈里没有 subAgent。"},
    {"id":"n42","kind":"fact","importance":2,"age_days":22,"text":"工具并发上限 max_concurrent_tools 默认是 4。"},
    {"id":"n43","kind":"preference","importance":3,"age_days":15,"text":"先说结论再给理由。"},
    {"id":"n44","kind":"decision","importance":3,"age_days":30,"text":"不把 sessions/*.json 直接交给模型读，实测会整读 270KB。"},
    {"id":"n45","kind":"fact","importance":4,"age_days":7,"text":"导出 HTML 要把所有动态字符串做 escape，否则工具输出里的 script 标签会执行。"},
    {"id":"n46","kind":"fact","importance":1,"age_days":37,"text":"SQLite 的 FTS5 是一个全文检索扩展。"},
    {"id":"n47","kind":"fact","importance":1,"age_days":38,"text":"BM25 是一种基于词频和文档长度的排序函数。"},
    {"id":"n48","kind":"fact","importance":1,"age_days":39,"text":"embedding 可以把文本映射成向量。"},
    {"id":"n49","kind":"preference","importance":2,"age_days":6,"text":"喜欢简洁的回答。"},
    {"id":"n50","kind":"fact","importance":1,"age_days":41,"text":"uv 是一个 Python 包管理器。"},
    {"id":"n51","kind":"fact","importance":3,"age_days":6,"text":"todo_write 的 depends_on 现在是硬门禁：转到 in_progress 或 completed 会被 blocked_by_dependencies 拦住。"},
    {"id":"n52","kind":"fact","importance":5,"age_days":0,"text":"今天要把 docs 目录里的 CLI 章节重写一遍。"}
  ],
  "queries": [
    {"category":"lexical","query":"我们用什么装依赖？","relevant":["n01"]},
    {"category":"lexical","query":"跑测试的正确命令是什么","relevant":["n02"]},
    {"category":"lexical","query":"记忆存在哪里，是数据库吗","relevant":["n14","n15"]},
    {"category":"lexical","query":"依赖门禁怎么关掉","relevant":["n21"]},
    {"category":"lexical","query":"上下文压缩分几层","relevant":["n13"]},
    {"category":"lexical","query":"轨迹文件有哪些字段","relevant":["n08"]},
    {"category":"lexical","query":"中文怎么分词","relevant":["n34"]},
    {"category":"lexical","query":"召回打分的公式和各部分权重","relevant":["n31"]},
    {"category":"lexical","query":"验收脚本在哪，怎么跑","relevant":["n37"]},
    {"category":"lexical","query":"hook 和门卫的失败语义有什么不同","relevant":["n39"]},
    {"category":"lexical","query":"为什么不让我直接读会话文件","relevant":["n44"]},
    {"category":"lexical","query":"导出 html 时要注意什么安全问题","relevant":["n45"]},
    {"category":"lexical","query":"模型的 key 存在哪里","relevant":["n03"]},
    {"category":"lexical","query":"记忆的淘汰上限是多少","relevant":["n38"]},
    {"category":"lexical","query":"提交信息有什么格式要求","relevant":["n05"]},
    {"category":"identifier","query":"todo_write 的 depends_on 现在强制吗","relevant":["n51"]},
    {"category":"identifier","query":"max_concurrent_tools 默认多少","relevant":["n42"]},
    {"category":"identifier","query":"AI_CODING_PTY 是干什么的","relevant":["n04"]},
    {"category":"identifier","query":"tools_chars 告警阈值是什么","relevant":["n24"]},
    {"category":"identifier","query":"subagent_context_mode 有哪两个取值","relevant":["n12"]},
    {"category":"identifier","query":"harvested_messages 游标放在哪里","relevant":["n30"]},
    {"category":"identifier","query":"RecallScorer 接缝是干嘛的","relevant":["n33"]},
    {"category":"identifier","query":"OTLP 导出要不要装 SDK","relevant":["n23"]},
    {"category":"identifier","query":"max_index_entries 是多少","relevant":["n38"]},
    {"category":"paraphrase","query":"怎么让新同事快速上手这个项目","relevant":["n16","n27","n43"]},
    {"category":"paraphrase","query":"担心模型把前面发过的东西再算一次钱","relevant":["n06"]},
    {"category":"paraphrase","query":"我不想每次都被问要不要批准","relevant":["n18"]},
    {"category":"paraphrase","query":"怎么防止工具输出把网页搞坏","relevant":["n45"]},
    {"category":"paraphrase","query":"长期记性是怎么做的","relevant":["n14","n07"]},
    {"category":"paraphrase","query":"卡住的命令怎么收场","relevant":["n10","n40"]},
    {"category":"paraphrase","query":"怎么让模型别一直重复问同一个问题","relevant":["n07","n30"]},
    {"category":"paraphrase","query":"代码评审之前要做什么","relevant":["n02","n20"]},
    {"category":"paraphrase","query":"我不想被过度设计绑架","relevant":["n35"]},
    {"category":"paraphrase","query":"怎么把过去几轮的记忆压缩成一句话","relevant":["n29","n13"]},
    {"category":"paraphrase","query":"which relevance algorithm do we use","relevant":["n32"]},
    {"category":"paraphrase","query":"怎么做语义检索","relevant":["n33"]},
    {"category":"multi","query":"记忆和提示词缓存怎么取舍","relevant":["n06","n07","n22"]},
    {"category":"multi","query":"压缩之后老的工具输出还能找回来吗","relevant":["n13","n22"]},
    {"category":"multi","query":"多智能体的上下文怎么给","relevant":["n12","n41"]},
    {"category":"multi","query":"真实环境跑验收的流程","relevant":["n36","n37"]},
    {"category":"noanswer","query":"生产环境的 PostgreSQL 连接串是什么","relevant":[]},
    {"category":"noanswer","query":"上次那个客户的发票抬头是什么","relevant":[]},
    {"category":"noanswer","query":"我们的 Android 应用上架了吗","relevant":[]},
    {"category":"noanswer","query":"Kubernetes 集群有多少节点","relevant":[]},
    {"category":"noanswer","query":"帮我改一下按钮的圆角","relevant":[]},
    {"category":"trap","query":"CLI 命令怎么加一个新子命令","relevant":[]}
  ]
}
```

`scripts/retrieval_cases.py`（薄 CLI：加载语料 → 跑**当前实现** → 打印分类与总计）：

```python
#!/usr/bin/env python3
"""R10 retrieval benchmark: our own memories, our own questions, real numbers.

    .venv/bin/python scripts/retrieval_cases.py
    .venv/bin/python scripts/retrieval_cases.py --with-embeddings --config config.local.yaml
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.memory.benchmark import EvalCase, evaluate, format_metrics, load_corpus  # noqa: E402
from ai_coding.memory.relevance import RecallParams, rank_notes  # noqa: E402

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
BASELINE = {"R@1": 0.72, "R@3": 0.85, "paraphrase": 6}


def rank_with(notes, params: RecallParams):
    def rank(query: str) -> list[str]:
        return [hit.note.id for hit in rank_notes(notes, query, params=params, now=NOW, limit=5)]

    return rank


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(ROOT / "scripts" / "retrieval_corpus.json"))
    parser.add_argument("--with-embeddings", action="store_true")
    parser.add_argument("--config", default="config.local.yaml")
    args = parser.parse_args(argv)

    notes, cases = load_corpus(args.corpus, now=NOW)
    params = RecallParams()

    overall = evaluate(rank_with(notes, params), cases)
    print(format_metrics("current", overall))
    for category in ("lexical", "identifier", "paraphrase", "multi"):
        subset = [case for case in cases if case.category == category]
        print(format_metrics(f"  {category}", evaluate(rank_with(notes, params), subset)))
    if overall.missed:
        print("missed: " + ", ".join(repr(q) for q in overall.missed))
    if overall.spurious:
        print("spurious: " + ", ".join(repr(q) for q in overall.spurious))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> **本 Task 只实现 `--corpus` 与 `--config`。** 上面代码块里出现的 `--with-embeddings` 参数与
> `BASELINE` 常量**不要**在本 Task 写进去（`EmbeddingIndex` 要到 Task 6 才存在）；
> Task 8 的 Step 2 会把它们补上——计划里不留 TODO 注释，缺的部分由后面那个 Task 明确负责。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_retrieval_benchmark.py -o addopts="" -q`
Expected: **5 passed**

Run: `.venv/bin/python scripts/retrieval_cases.py`
Expected: 打印 `current ... R@1≈0.68 R@3≈0.85 FP@5≈0.48` 与四类分项——**这就是 Task 2 要改善的基线**

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/benchmark.py scripts/retrieval_corpus.json scripts/retrieval_cases.py tests/test_retrieval_benchmark.py
git commit -m "R10a: retrieval gets a yardstick, measured on our own memories"
```

---

### Task 2: 打分与门禁（BM25 排序 / 绝对地板 / 相对带宽 / 命中词）

**Files:**
- Modify: `ai_coding/memory/relevance.py`
- Modify: `ai_coding/config/models.py`
- Test: `tests/test_memory_relevance.py`（追加 9 条）

**Interfaces:**
- Consumes: `coverage()`、`tokenize()`、`MemoryNote`
- Produces: `bm25_scores(notes, query_tokens, *, stats=None, k1=1.2, b=0.75) -> dict[str, float]`；
  `RankingResult(kept, rejected, terms_known, terms_unknown, no_vocabulary_match)`；
  `rank_notes_detailed(notes, query, *, params=None, now=None, limit=None) -> RankingResult`；
  `ScoredNote.matched_terms: tuple[str, ...]`；`score_note` **移除**（全仓无引用者，已核）

- [ ] **Step 1: 写红测试**

```python
# ---- R10b: bm25 ranking, an absolute floor and a relative band ---- 


def test_r10b_bm25_prefers_the_note_with_more_of_the_query():
    notes = [
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", created_time=_ago(5)),
        MemoryNote(id="b", text="uv 是包管理器，依赖用它装", created_time=_ago(5)),
        MemoryNote(id="c", text="今天天气不错", created_time=_ago(5)),
    ]
    scores = bm25_scores(notes, tokenize("uv 依赖"))
    assert scores["b"] > scores["a"] > scores["c"] == 0.0


def test_r10b_the_absolute_floor_kills_a_fresh_important_irrelevant_note():
    """§1.1 实测的那条洞：rel=0 但 age=0、importance=5，靠 0.20+0.15 过阈值。"""
    notes = [
        MemoryNote(id="loud", text="今天要把 docs 目录里的 CLI 章节重写一遍",
                   importance=5, created_time=_ago(0)),
        MemoryNote(id="real", text="上下文压缩有四层：L3 落盘、L1 裁剪、L2 信封、L4 摘要",
                   importance=4, created_time=_ago(30)),
    ]
    result = rank_notes_detailed(notes, "上下文压缩分几层", now=NOW)
    assert [hit.note.id for hit in result.kept] == ["real"]
    assert "loud" not in {hit.note.id for hit in result.kept}


def test_r10b_the_relative_band_drops_the_tail():
    notes = [
        MemoryNote(id="best", text="上下文压缩有四层", created_time=_ago(5)),
        MemoryNote(id="weak", text="上下文里有压缩这个词", created_time=_ago(5)),
    ]
    params = RecallParams(min_relevance=0.0, relative_band=0.99)
    result = rank_notes_detailed(notes, "上下文压缩分几层", params=params, now=NOW)
    assert [hit.note.id for hit in result.kept] == ["best"]
    assert "weak" in {hit.note.id for hit in result.rejected}


def test_r10b_matched_terms_are_only_the_query_terms_that_hit():
    notes = [MemoryNote(id="a", text="项目依赖统一用 uv 管理", created_time=_ago(5))]
    result = rank_notes_detailed(notes, "uv 依赖怎么装", now=NOW)
    assert result.kept[0].matched_terms == ("uv", "依赖")


def test_r10b_unknown_vocabulary_is_reported_not_ranked():
    notes = [MemoryNote(id="a", text="项目依赖统一用 uv 管理", created_time=_ago(5))]
    result = rank_notes_detailed(notes, "payment timeout", now=NOW)
    assert result.kept == () and result.no_vocabulary_match is True
    assert set(result.terms_unknown) == {"payment", "timeout"}


def test_r10b_gates_agree_with_the_old_shape_on_a_simple_hit():
    """旧行为不许退步：一条明显相关的笔记仍然被召回，且排名第一。"""
    notes = [
        MemoryNote(id="a", text="项目依赖统一用 uv 管理", created_time=_ago(5)),
        MemoryNote(id="b", text="uv 是一个 Python 包管理器", created_time=_ago(40)),
    ]
    hits = rank_notes(notes, "依赖怎么装", params=RecallParams(), now=NOW)
    assert hits and hits[0].note.id == "a"


def test_r10b_limit_still_truncates_and_keeps_order():
    notes = [
        MemoryNote(id=f"n{i}", text="依赖管理用 uv", created_time=_ago(i)) for i in range(5)
    ]
    hits = rank_notes(notes, "依赖", params=RecallParams(), now=NOW, limit=2)
    assert len(hits) == 2
    assert hits[0].score >= hits[1].score


def test_r10b_recency_source_can_use_last_used():
    fresh_by_created = MemoryNote(id="a", text="依赖用 uv", created_time=_ago(1))
    used_lately = MemoryNote(id="b", text="依赖用 uv", created_time=_ago(90),
                             last_used=_ago(0))
    by_created = rank_notes([fresh_by_created, used_lately], "依赖",
                            params=RecallParams(min_relevance=0.0), now=NOW)
    by_accessed = rank_notes([fresh_by_created, used_lately], "依赖",
                             params=RecallParams(min_relevance=0.0,
                                                 recency_source="accessed"), now=NOW)
    assert by_created[0].note.id == "a"
    assert by_accessed[0].note.id == "b"


def test_r10b_default_floor_moved_to_a_quarter():
    assert RecallParams().min_relevance == 0.25
    assert MemoryConfig().recall_min_relevance == 0.25
    assert MemoryConfig().recall_relative_band == 0.55
```

（测试文件顶部补：`from datetime import datetime, timedelta, timezone`、
`from ai_coding.config.models import MemoryConfig`、`from ai_coding.memory.note import MemoryNote`、
`from ai_coding.memory.relevance import RecallParams, bm25_scores, rank_notes, rank_notes_detailed`、
`from ai_coding.memory.tokenize import tokenize`；以及帮助函数
`NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)` 与
`def _ago(days): return (NOW - timedelta(days=days)).isoformat()`。）

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_memory_relevance.py -o addopts="" -q`
Expected: FAIL —— `ImportError: cannot import name 'bm25_scores'`

- [ ] **Step 3: 最小实现**

`ai_coding/memory/relevance.py` 的关键改动（完整代码）：

```python
@dataclass(frozen=True)
class RecallParams:
    """Tunable recall behaviour.  Relevance is a GATE; recency/importance only rank."""

    threshold: float = 0.25
    min_relevance: float = 0.25          # R10b: was 0.05 — that was no gate at all
    relative_band: float = 0.55          # R10b: score must be >= 0.55 * the best score
    weight_rank: float = 0.55            # R10b: BM25 (normalised), was "relevance"
    weight_recency: float = 0.20
    weight_importance: float = 0.15
    half_life_days: float = 14.0
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    recency_source: Literal["created", "accessed"] = "created"
    kind_boosts: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_KIND_BOOSTS)
    )

    @classmethod
    def from_memory_config(cls, config: Any) -> RecallParams:
        return cls(
            threshold=float(getattr(config, "recall_threshold", 0.25)),
            min_relevance=float(getattr(config, "recall_min_relevance", 0.25)),
            relative_band=float(getattr(config, "recall_relative_band", 0.55)),
            half_life_days=float(getattr(config, "recency_half_life_days", 14.0)),
        )


@dataclass(frozen=True)
class ScoredNote:
    note: MemoryNote
    score: float
    relevance: float
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankingResult:
    """Everything the caller needs to explain a search (R10b/R10c)."""

    kept: tuple[ScoredNote, ...] = ()
    rejected: tuple[ScoredNote, ...] = ()
    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    no_vocabulary_match: bool = False


@dataclass(frozen=True)
class _Stats:
    df: Counter[str]
    n_docs: int
    tokens: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    avg_len: float = 0.0


def _stats(notes: Sequence[MemoryNote]) -> _Stats:
    tokens = {note.id: tuple(tokenize(note.text)) for note in notes}
    df: Counter[str] = Counter()
    for values in tokens.values():
        df.update(set(values))
    lengths = [len(values) for values in tokens.values()]
    return _Stats(
        df=df,
        n_docs=len(notes),
        tokens=tokens,
        avg_len=(sum(lengths) / len(lengths)) if lengths else 0.0,
    )


def bm25_scores(
    notes: Sequence[MemoryNote],
    query_tokens: Sequence[str],
    *,
    stats: _Stats | None = None,
    k1: float = 1.2,
    b: float = 0.75,
) -> dict[str, float]:
    """Okapi BM25 per note.  Pure; scale is comparable *within one query* only."""
    resolved = stats if stats is not None else _stats(notes)
    terms = list(dict.fromkeys(query_tokens))
    out: dict[str, float] = {}
    for note in notes:
        tokens = resolved.tokens.get(note.id)
        if tokens is None:
            tokens = tuple(tokenize(note.text))
        counts = Counter(tokens)
        length = len(tokens) or 1
        total = 0.0
        for term in terms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            denominator = freq + k1 * (1 - b + b * length / (resolved.avg_len or 1.0))
            total += _idf(term, resolved.df, resolved.n_docs) * freq * (k1 + 1) / denominator
        out[note.id] = total
    return out


def _time_of(note: MemoryNote, params: RecallParams) -> str:
    if params.recency_source == "accessed" and note.last_used:
        return note.last_used
    return note.created_time


def rank_notes_detailed(
    notes: Sequence[MemoryNote],
    query: str,
    *,
    params: RecallParams | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> RankingResult:
    """Rank with BM25, then gate on relevance (absolute) and margin (relative)."""
    active = [note for note in notes if note.is_active]
    resolved = params or RecallParams()
    moment = now or datetime.now(timezone.utc)
    query_tokens = tokenize(query)
    if not query_tokens or not active:
        return RankingResult()
    stats = _stats(active)
    unique = list(dict.fromkeys(query_tokens))
    known = tuple(t for t in unique if stats.df.get(t, 0) > 0)
    unknown = tuple(t for t in unique if stats.df.get(t, 0) == 0)
    if not known:
        return RankingResult(
            terms_known=(), terms_unknown=unknown, no_vocabulary_match=True
        )
    raw = bm25_scores(active, query_tokens, stats=stats, k1=resolved.bm25_k1, b=resolved.bm25_b)
    best_raw = max(raw.values()) or 1.0
    scored: list[ScoredNote] = []
    for note in active:
        doc_tokens = set(stats.tokens.get(note.id, ()))
        relevance = coverage(query_tokens, tuple(doc_tokens), df=stats.df, n_docs=stats.n_docs)
        freshness = recency(_time_of(note, resolved), now=moment,
                            half_life_days=resolved.half_life_days)
        importance = max(0.0, min((note.importance - 1) / 4, 1.0))
        boost = float(resolved.kind_boosts.get(note.kind.value, 0.0))
        score = (
            resolved.weight_rank * (raw.get(note.id, 0.0) / best_raw)
            + resolved.weight_recency * freshness
            + resolved.weight_importance * importance
            + boost
        )
        matched = tuple(t for t in known if t in doc_tokens)
        scored.append(ScoredNote(note=note, score=score, relevance=relevance,
                                 matched_terms=matched))
    if not scored:
        return RankingResult(terms_known=known, terms_unknown=unknown)
    best = max(item.score for item in scored)
    kept: list[ScoredNote] = []
    rejected: list[ScoredNote] = []
    for item in scored:
        if (
            item.relevance >= resolved.min_relevance
            and item.score >= resolved.threshold
            and item.score >= resolved.relative_band * best
        ):
            kept.append(item)
        else:
            rejected.append(item)
    order = lambda item: (  # noqa: E731 — one local sort key, same shape as before
        -item.score, -item.relevance, evidence_rank(item.note), item.note.id
    )
    kept.sort(key=order)
    rejected.sort(key=order)
    if limit is not None and limit >= 0:
        kept = kept[:limit]
    return RankingResult(
        kept=tuple(kept), rejected=tuple(rejected),
        terms_known=known, terms_unknown=unknown,
    )


def rank_notes(
    notes: Iterable[MemoryNote],
    query: str,
    *,
    params: RecallParams | None = None,
    now: datetime | None = None,
    limit: int | None = None,
    scorer: RecallScorer | None = None,
) -> list[ScoredNote]:
    """The public shape stays a list; diagnostics live in ``rank_notes_detailed``."""
    materialised = list(notes)
    if scorer is not None:
        active = [note for note in materialised if note.is_active]
        return scorer(active, query, params=params or RecallParams(),
                      now=now or datetime.now(timezone.utc), limit=limit)
    return list(
        rank_notes_detailed(materialised, query, params=params, now=now, limit=limit).kept
    )
```

同时：`__all__` 去掉 `"score_note"`、加上 `"RankingResult"` / `"bm25_scores"` / `"rank_notes_detailed"`；
删除 `score_note()` 函数体（已核实全仓无引用者）；顶部 import 补 `Literal`。

`ai_coding/config/models.py`：

```python
    recall_threshold: float = 0.25
    # R10b: 0.05 measured as "no gate at all" — a fresh, important, irrelevant note
    # reached 0.456 and passed 0.25 purely on recency+importance (docs/r10-analysis.md 1.1).
    recall_min_relevance: float = 0.25
    # R10b: a candidate must also be within this share of the best score in the
    # same query — the relative gate is what strips the junk tail (FP@5 0.78 → 0.32).
    recall_relative_band: float = 0.55
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_memory_relevance.py tests/test_memory_service.py tests/test_memory_service_v2.py tests/test_tool_memory_search.py tests/test_agent_loop_memory.py tests/test_memory_write.py -o addopts="" -q`
Expected: 全绿（既有 relevance 用例零改动；`min_relevance` 0.05→0.25 的爆炸半径已预演为 0）

Run: `.venv/bin/python scripts/retrieval_cases.py`
Expected: `current` 的 R@3 ≥ 0.85、FP@5 ≤ 0.37（对比 Task 1 记录的基线）

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/relevance.py ai_coding/config/models.py tests/test_memory_relevance.py
git commit -m "R10b: bm25 ranking, an absolute relevance floor and a relative band"
```

---

### Task 3: 命中终于算「用过」

**Files:**
- Modify: `ai_coding/memory/store.py`
- Modify: `ai_coding/memory/service.py`
- Test: `tests/test_memory_store_v2.py`（追加 2 条）、`tests/test_memory_service.py`（追加 3 条）

**Interfaces:**
- Produces: `MemoryStore.touch_notes(note_ids: Sequence[str]) -> int`（更新 `hits+1` / `last_used`，一次写盘，未知 id 忽略）
- Consumes: `MemoryNote.touch(when)`（R3 就有，只是从没被召回路径调用过）

- [ ] **Step 1: 写红测试**

`tests/test_memory_store_v2.py`：

```python
# ---- R10b: a recall finally counts as a use ----


def test_r10b_touch_notes_updates_only_the_given_ids(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="依赖用 uv", created_time="2026-09-01T00:00:00+00:00"))
    store.add_note(MemoryNote(id="b", text="压缩有四层", created_time="2026-09-01T00:00:00+00:00"))

    touched = store.touch_notes(["a", "ghost"])

    assert touched == 1
    first = store.get("a")
    assert first is not None and first.hits == 1 and first.last_used
    second = store.get("b")
    assert second is not None and second.hits == 0 and second.last_used == ""


def test_r10b_touch_notes_persists(tmp_path):
    path = tmp_path / "m.json"
    store = MemoryStore(path)
    store.add_note(MemoryNote(id="a", text="依赖用 uv", created_time="2026-09-01T00:00:00+00:00"))
    store.touch_notes(["a"])

    assert MemoryStore(path).get("a").hits == 1
```

`tests/test_memory_service.py`：

```python
# ---- R10b: the usage signal is wired ----


def test_r10b_a_hit_counts_as_a_use(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目依赖统一用 uv 管理",
                              created_time="2026-09-01T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(service.recall_notes("依赖怎么装", k=3))

    assert [hit.note.id for hit in hits] == ["a"]
    stored = store.get("a")
    assert stored is not None and stored.hits == 1 and stored.last_used != ""


def test_r10b_a_miss_touches_nothing(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目依赖统一用 uv 管理",
                              created_time="2026-09-01T00:00:00+00:00"))
    service = MemoryService(store)

    assert asyncio.run(service.recall_notes("payment timeout", k=3)) == []
    assert store.get("a").hits == 0


def test_r10b_promotion_becomes_reachable_from_real_use(tmp_path):
    """`hits >= 3 → importance + 1` could never fire while recalls did not touch."""
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="项目依赖统一用 uv 管理", importance=3,
                              created_time="2026-09-01T00:00:00+00:00"))
    service = MemoryService(store)
    for _ in range(3):
        asyncio.run(service.recall_notes("依赖怎么装", k=3))

    report = asyncio.run(service.consolidate(force=True))

    assert "a" in report.promoted
    assert store.get("a").importance == 4
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_memory_store_v2.py tests/test_memory_service.py -o addopts="" -q`
Expected: FAIL —— `AttributeError: 'MemoryStore' object has no attribute 'touch_notes'`

- [ ] **Step 3: 最小实现**

`ai_coding/memory/store.py`（放在 `pin()` 之后）：

```python
    def touch_notes(self, note_ids: Sequence[str]) -> int:
        """Record that these notes were just used (R10b).  One write, unknown ids ignored.

        Before R10 nothing on the read path called ``touch()``, so ``hits`` only ever
        moved during consolidation merges - which made both the promotion rule
        (``hits >= 3``) and the eviction value function dead weight
        (docs/r10-analysis.md 1.2).
        """
        wanted = {str(note_id) for note_id in note_ids if str(note_id).strip()}
        if not wanted:
            return 0

        def _do() -> int:
            stamp = now_utc()
            touched = 0
            for index, note in enumerate(self._notes):
                if note.id not in wanted:
                    continue
                self._notes[index] = note.touch(stamp)
                touched += 1
            return touched

        return self._mutate(_do)
```

`ai_coding/memory/service.py`（`recall_notes` 里，命中之后）：

```python
        try:
            hits = self._store.search_scored(
                query,
                params=self._params(),
                now=now or _now(),
                limit=supply if limit is None else limit,
            )
        except Exception:  # noqa: BLE001 — recall is best-effort
            return []
        if hits:
            # R10b: a hit is a use.  Best-effort: a failed write must not lose the answer.
            try:
                self._store.touch_notes([hit.note.id for hit in hits])
            except Exception:  # noqa: BLE001
                pass
        return hits
```

> 注意：`touch_notes` 会**重写整份 JSON**。200 条 = 0.09MB、实测单次约 1ms；只在有命中时发生。
> `MemoryStore` 已有 `RLock`，与 harvest 并发安全。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_memory_store_v2.py tests/test_memory_service.py tests/test_memory_turn.py tests/test_agent_loop_memory.py tests/test_memory_write.py -o addopts="" -q`
Expected: 全绿（已核实：**没有任何既有用例断言 `hits`/`last_used`**）

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/store.py ai_coding/memory/service.py tests/test_memory_store_v2.py tests/test_memory_service.py
git commit -m "R10b: a recall finally counts as a use"
```

---

### Task 4: 一次没搜到，要说清下一步怎么问

**Files:**
- Modify: `ai_coding/memory/service.py`
- Modify: `ai_coding/tools/memory_search_tool.py`
- Modify: `tests/test_tool_memory_search.py`（**唯一一处既有断言改动**，见 Global Constraints）
- Test: `tests/test_tool_memory_search.py`（追加 7 条）

**Interfaces:**
- Produces: `memory.service.IndexSummary(total, by_kind, recent)`；
  `memory.service.RecallDiagnostics(terms_known, terms_unknown, near_misses, index_summary, semantic_unavailable, no_vocabulary_match)`；
  `MemoryService.recall_with_diagnostics(query_or_history, *, k=None, now=None, limit=None) -> tuple[list[ScoredNote], RecallDiagnostics]`
  （`recall_notes` 变成它的一层薄封装，签名不变）；
  `memory_search` 的 `data` 结构（§5.2）+ 新参数 `include_near_misses`（默认 `true`）

- [ ] **Step 1: 写红测试**

```python
# ---- R10c: a miss says what to try next ----


@pytest.mark.asyncio
async def test_r10c_a_hit_reports_matched_terms_and_no_match_false(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "项目用 uv 管理依赖"))
    result = await tool.execute({"query": "依赖怎么装"})
    assert result.data["no_match"] is False
    assert result.data["notes"][0]["matched_terms"] == ["依赖"]
    assert result.data["terms_known"] and result.data["terms_unknown"] == []


@pytest.mark.asyncio
async def test_r10c_a_miss_is_a_structured_no_match(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "payment service timeout"))
    result = await tool.execute({"query": "依赖怎么装"})
    assert result.success is True
    assert result.data["notes"] == []
    assert result.data["no_match"] is True
    assert result.data["terms_unknown"] == ["依赖"]      # df("依赖") == 0 in this corpus


@pytest.mark.asyncio
async def test_r10c_near_misses_are_bounded_and_never_duplicate_hits(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    for index in range(6):
        store.add_note(MemoryNote(id=f"n{index}", text=f"依赖管理用 uv 第 {index} 条",
                                  created_time=f"2026-09-{10 + index:02d}T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "依赖"})

    hits = {note["id"] for note in result.data["notes"]}
    misses = result.data["near_misses"]
    assert len(misses) <= 3
    assert not (hits & {note["id"] for note in misses})


@pytest.mark.asyncio
async def test_r10c_near_misses_can_be_switched_off(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "payment service timeout"))
    result = await tool.execute({"query": "依赖怎么装", "include_near_misses": False})
    assert "near_misses" not in result.data


@pytest.mark.asyncio
async def test_r10c_the_index_map_only_appears_on_a_miss_and_is_bounded(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    for index in range(300):
        store.add_note(MemoryNote(id=f"n{index:03d}", text=f"第 {index} 条无关笔记",
                                  created_time="2026-09-01T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    hit = await tool.execute({"query": "payment timeout"})
    assert "index" in hit.data
    assert len(json.dumps(hit.data["index"], ensure_ascii=False)) < 400
    assert hit.data["index"]["total"] == 300
    assert sum(hit.data["index"]["by_kind"].values()) == 300
    assert len(hit.data["index"]["recent"]) <= 3


@pytest.mark.asyncio
async def test_r10c_the_miss_text_says_what_to_do_next(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "项目用 uv 管理依赖"))
    result = await tool.execute({"query": "发票抬头是多少"})
    text = result.output
    assert "没有" in text and "索引" in text       # says there is nothing
    assert "发票" in text or "抬头" in text        # names a word the index does not have
    assert "换" in text                            # says what to do next


@pytest.mark.asyncio
async def test_r10c_a_memory_without_diagnostics_still_works(tmp_path):
    """Duck-typed fakes that only implement recall_notes keep working."""

    class _Legacy:
        async def recall_notes(self, *_args, **_kwargs):
            return []

    result = await MemorySearchTool(_Legacy()).execute({"query": "anything"})
    assert result.success is True
    assert result.data["notes"] == [] and result.data["no_match"] is True
```

**同时改这一条既有断言**（唯一一处，理由写在 Global Constraints）：

```python
@pytest.mark.asyncio
async def test_no_hits_is_a_successful_empty_answer(tmp_path):
    tool = MemorySearchTool(_service(tmp_path, "payment service timeout"))
    result = await tool.execute({"query": "how do I add a dependency"})
    assert result.success is True
    # R10c: the payload now carries the diagnosis on purpose; only `notes` is empty.
    assert result.data["notes"] == []
    assert result.data["no_match"] is True
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_tool_memory_search.py -o addopts="" -q`
Expected: FAIL —— `TypeError: recall_with_diagnostics() got an unexpected keyword argument`（或 `KeyError: 'no_match'`）

- [ ] **Step 3: 最小实现**

`ai_coding/memory/service.py` 新增（放在 `MemoryService` 之前）：

```python
@dataclass(frozen=True)
class IndexSummary:
    """A bounded map of what is in memory - the "ls" a failed search needs (R10c)."""

    total: int = 0
    by_kind: Mapping[str, int] = field(default_factory=dict)
    recent: tuple[tuple[str, str, str], ...] = ()   # (id, kind, preview)


@dataclass(frozen=True)
class RecallDiagnostics:
    """Why a search returned what it returned.  Never raises, always explainable."""

    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    near_misses: tuple[ScoredNote, ...] = ()
    index_summary: IndexSummary | None = None
    semantic_unavailable: bool = False
    no_vocabulary_match: bool = False
```

`MemoryService` 里新增（`recall_notes` 改成调用它）：

```python
    _PREVIEW_CHARS = 40
    _INDEX_RECENT = 3

    def index_summary(self) -> IndexSummary:
        """Counts by kind plus the few most recent notes.  Never raises."""
        notes = [note for note in self._store.active()]
        notes.sort(key=lambda note: (_invert_time(note.updated_time or note.created_time), note.id))
        by_kind: dict[str, int] = {}
        for note in notes:
            by_kind[note.kind.value] = by_kind.get(note.kind.value, 0) + 1
        recent = tuple(
            (note.id, note.kind.value, note.text[: self._PREVIEW_CHARS])
            for note in notes[: self._INDEX_RECENT]
        )
        return IndexSummary(total=len(notes), by_kind=by_kind, recent=recent)

    async def recall_with_diagnostics(
        self,
        query_or_history: str | Sequence[ChatMessage],
        *,
        k: int | None = None,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[list[ScoredNote], RecallDiagnostics]:
        """Rank plus everything needed to explain (and retry) the result (R10c)."""
        if not self.enabled:
            return [], RecallDiagnostics()
        supply = int(self._get("max_per_turn_injections", 3)) if k is None else int(k)
        if supply <= 0:
            return [], RecallDiagnostics()
        query = _query_text(query_or_history)
        if not query.strip():
            return [], RecallDiagnostics()
        try:
            result = rank_notes_detailed(
                self._store.active(),
                query,
                params=self._params(),
                now=now or _now(),
                limit=supply if limit is None else limit,
            )
        except Exception:  # noqa: BLE001 — recall is best-effort
            return [], RecallDiagnostics()
        hits = list(result.kept)
        if hits:
            try:
                self._store.touch_notes([hit.note.id for hit in hits])
            except Exception:  # noqa: BLE001
                pass
        return hits, RecallDiagnostics(
            terms_known=result.terms_known,
            terms_unknown=result.terms_unknown,
            near_misses=result.rejected[:3],
            index_summary=self.index_summary(),
            no_vocabulary_match=result.no_vocabulary_match,
        )
```

`recall_notes` 变成：

```python
    async def recall_notes(self, query_or_history, *, k=None, now=None, limit=None):
        """Rank memories for a query; [] when disabled.  Diagnostics: see _with_diagnostics."""
        hits, _diagnostics = await self.recall_with_diagnostics(
            query_or_history, k=k, now=now, limit=limit
        )
        return hits
```

`ai_coding/tools/memory_search_tool.py` 的 `execute` 主体：

```python
        query = str(params.get("query", "") or "").strip()
        if not query:
            query = _query_from_context(resolve_ctx(ctx))
        if not query:
            return ToolResult(
                success=False,
                output="error: 'query' is required",
                error="empty_query",
                data={"query": ""},
            )
        wanted_evidence = str(params.get("evidence", "") or "").strip().casefold()
        if wanted_evidence and wanted_evidence not in ("tool", "model"):
            return ToolResult(
                success=False,
                output="error: 'evidence' must be 'tool', 'model' or empty",
                error="bad_evidence",
                data={"evidence": wanted_evidence},
            )
        include_near = bool(params.get("include_near_misses", True))
        try:
            recall, diagnostics = _recall_with_diagnostics(
                self._memory, query, k=_as_int(params.get("k"), 3)
            )
        except Exception as exc:  # noqa: BLE001 — the tool boundary
            return ToolResult(
                success=False,
                output=f"memory search failed: {exc}",
                error="memory_error",
                data={"query": query},
            )
        hits = _filtered(recall, params)
        notes = [_note_payload(hit) for hit in hits]
        data: dict[str, Any] = {
            "notes": notes,
            "no_match": not notes,
            "terms_known": list(diagnostics.terms_known),
            "terms_unknown": list(diagnostics.terms_unknown),
            "semantic_unavailable": bool(diagnostics.semantic_unavailable),
        }
        if include_near:
            data["near_misses"] = [
                {
                    "id": hit.note.id,
                    "kind": hit.note.kind.value,
                    "score": round(hit.score, 3),
                    "relevance": round(hit.relevance, 3),
                    "preview": hit.note.text[:40],
                }
                for hit in diagnostics.near_misses
            ]
        if not notes:
            summary = diagnostics.index_summary
            if summary is not None:
                data["index"] = {
                    "total": summary.total,
                    "by_kind": dict(summary.by_kind),
                    "recent": [
                        {"id": note_id, "kind": kind, "preview": preview}
                        for note_id, kind, preview in summary.recent
                    ],
                }
            return ToolResult(success=True, output=_miss_text(data, query), data=data)
        lines = [
            f"- [{note['kind']}#{note['id'][:8]} score={note['score']} "
            f"matched={'/'.join(note['matched_terms'])}] {note['text']}"
            for note in notes
        ]
        return ToolResult(success=True, output="\n".join(lines), data=data)
```

同文件新增私有帮助函数（**注意定义顺序**：模块级常量 `_EMPTY_DIAGNOSTICS` 依赖
`_DiagnosticsShim`，所以 shim 类必须写在它前面）：

```python
class _DiagnosticsShim:
    """What an old, duck-typed memory returns: no terms, no near misses, no map."""

    terms_known: tuple[str, ...] = ()
    terms_unknown: tuple[str, ...] = ()
    near_misses: tuple[Any, ...] = ()
    index_summary: Any = None
    semantic_unavailable = False
    no_vocabulary_match = False


_EMPTY_DIAGNOSTICS = _DiagnosticsShim()


def _recall_with_diagnostics(memory: Any, query: str, **kwargs: Any) -> tuple[list[Any], Any]:
    """Prefer the R10c path; fall back for duck-typed fakes that predate it."""
    detailed = getattr(memory, "recall_with_diagnostics", None)
    if detailed is None:
        return list(memory.recall_notes(query, k=kwargs.get("k", 3))), _EMPTY_DIAGNOSTICS
    return detailed(query, **kwargs)


def _strings(value: Any) -> list[str]:
    """One or more kind names from the tool call (empty list means 'no filter')."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip().casefold() for item in value if str(item).strip()]


def _optional(value: Any) -> str | None:
    body = str(value or "").strip()
    return body or None


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _filtered(hits: list[Any], params: dict[str, Any]) -> list[Any]:
    wanted = str(params.get("kind", "") or "").strip().casefold()
    if wanted:
        hits = [hit for hit in hits if hit.note.kind.value == wanted]
    evidence = str(params.get("evidence", "") or "").strip().casefold()
    if evidence == "tool":
        hits = [hit for hit in hits if (hit.note.evidence or "") == "tool"]
    elif evidence == "model":
        hits = [hit for hit in hits if (hit.note.evidence or "") != "tool"]
    return hits


def _note_payload(hit: Any) -> dict[str, Any]:
    return {
        "id": hit.note.id,
        "kind": hit.note.kind.value,
        "score": round(hit.score, 3),
        "relevance": round(hit.relevance, 3),
        "matched_terms": list(hit.matched_terms),
        "text": hit.note.text,
        "updated_time": hit.note.updated_time or hit.note.created_time,
        "pinned": hit.note.pinned,
        "evidence": hit.note.evidence,
    }


def _miss_text(data: dict[str, Any], query: str) -> str:
    """One actionable paragraph: what was searched, what is missing, what to try."""
    summary = data.get("index") or {}
    kinds = "、".join(f"{kind} {count}" for kind, count in (summary.get("by_kind") or {}).items())
    lines = [f'没有可靠匹配（查的是 "{query}"；索引里 {summary.get("total", 0)} 条：{kinds or "空"}）。']
    if data["terms_unknown"]:
        lines.append("这些词索引里没有：" + "、".join(data["terms_unknown"][:6]) + "。")
    near = data.get("near_misses") or []
    if near:
        lines.append(
            "最接近但未经证实："
            + "；".join(f"#{note['id'][:8]}({note['kind']}) {note['preview']}" for note in near[:2])
        )
    lines.append("→ 换一个说法再搜，或用 kind=decision/fact/preference/open_thread 缩小范围。")
    return "\n".join(lines)
```

`params_json_schema` 增加（`required` 仍是 `["query"]`）：

```python
                "include_near_misses": {
                    "type": "boolean",
                    "description": "Include the closest non-matching notes (default true).",
                },
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_tool_memory_search.py tests/test_memory_service.py tests/test_memory_service_v2.py tests/test_agent_loop_memory.py -o addopts="" -q`
Expected: 全绿（**只有 `test_no_hits_is_a_successful_empty_answer` 的断言被扩展**，其余零改动）

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/service.py ai_coding/tools/memory_search_tool.py tests/test_tool_memory_search.py
git commit -F - <<'MSG'
R10c: a miss says what to try next

The only pre-existing assertion this touches is
test_no_hits_is_a_successful_empty_answer's exact-dict check: the payload is
deliberately extended by this round, so it now asserts notes == [] plus
no_match is True instead of the whole dict.
MSG
```

---

### Task 5: 过滤器与有界索引地图

**Files:**
- Modify: `ai_coding/memory/service.py`
- Modify: `ai_coding/tools/memory_search_tool.py`
- Test: `tests/test_tool_memory_search.py`（追加 4 条）、`tests/test_memory_service.py`（追加 2 条）

**Interfaces:**
- Produces: `MemoryService.recall_with_diagnostics(..., scope=None, since_days=None, kinds=None, evidence=None)`；
  `memory_search` 新参数 `scope`、`since_days`
- 行为改进：`kind` / `evidence` 从「取前 k 条再过滤」改成**先过滤再排序**（今天 `k=3` 时
  过滤后可能只剩 0 条，明明索引里有匹配）

- [ ] **Step 1: 写红测试**

```python
# ---- R10c: filters narrow the candidate set BEFORE ranking ----


def test_r10c_kind_filter_does_not_shrink_the_result(tmp_path):
    """Regression: filtering after the top-k could return nothing at all."""
    store = MemoryStore(tmp_path / "m.json")
    for index in range(4):
        store.add_note(MemoryNote(id=f"f{index}", text="依赖管理用 uv", kind=NoteKind.FACT,
                                  created_time="2026-09-0%dT00:00:00+00:00" % (index + 1)))
    store.add_note(MemoryNote(id="d1", text="决定：依赖管理统一用 uv", kind=NoteKind.DECISION,
                              created_time="2026-08-01T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(service.recall_notes("依赖", k=1, kinds=["decision"]))

    assert [hit.note.id for hit in hits] == ["d1"]


def test_r10c_scope_filters_candidates(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="依赖用 uv", scope="workspace",
                              created_time="2026-09-01T00:00:00+00:00"))
    store.add_note(MemoryNote(id="b", text="依赖用 poetry", scope="other",
                              created_time="2026-09-01T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(service.recall_notes("依赖", k=5, scope="other"))

    assert [hit.note.id for hit in hits] == ["b"]


def test_r10c_since_days_filters_by_update_time(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="old", text="依赖用 uv", updated_time="2026-01-01T00:00:00+00:00",
                              created_time="2026-01-01T00:00:00+00:00"))
    store.add_note(MemoryNote(id="new", text="依赖用 uv", updated_time="2026-09-27T00:00:00+00:00",
                              created_time="2026-09-27T00:00:00+00:00"))
    service = MemoryService(store)

    hits = asyncio.run(
        service.recall_notes("依赖", k=5, since_days=7,
                             now=datetime(2026, 9, 28, tzinfo=timezone.utc))
    )

    assert [hit.note.id for hit in hits] == ["new"]


@pytest.mark.asyncio
async def test_r10c_tool_passes_scope_and_since_through(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    store.add_note(MemoryNote(id="a", text="依赖用 uv", scope="other",
                              created_time="2026-09-01T00:00:00+00:00"))
    store.add_note(MemoryNote(id="b", text="依赖用 uv", scope="workspace",
                              created_time="2026-09-01T00:00:00+00:00"))
    tool = MemorySearchTool(MemoryService(store))

    result = await tool.execute({"query": "依赖", "scope": "other"})

    assert [note["id"] for note in result.data["notes"]] == ["a"]
```

（`tests/test_tool_memory_search.py` 需要补 `import json`；`tests/test_memory_service.py` 需要
`from datetime import datetime, timezone`。）

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_memory_service.py tests/test_tool_memory_search.py -o addopts="" -q`
Expected: FAIL —— `TypeError: recall_notes() got an unexpected keyword argument 'kinds'`

- [ ] **Step 3: 最小实现**

`ai_coding/memory/service.py`：

```python
    def _candidates(
        self,
        *,
        kinds: Sequence[str] | None,
        evidence: str | None,
        scope: str | None,
        since_days: int | None,
        now: datetime,
    ) -> list[MemoryNote]:
        """Filter BEFORE ranking: filtering a top-k afterwards can empty a good search."""
        wanted_kinds = {str(kind).strip().casefold() for kind in (kinds or []) if str(kind).strip()}
        wanted_evidence = (evidence or "").strip().casefold()
        cutoff = now.timestamp() - int(since_days) * 86_400 if since_days else None
        selected: list[MemoryNote] = []
        for note in self._store.active():
            if wanted_kinds and note.kind.value not in wanted_kinds:
                continue
            if wanted_evidence == "tool" and (note.evidence or "") != "tool":
                continue
            if wanted_evidence == "model" and (note.evidence or "") == "tool":
                continue
            if scope is not None and note.scope != scope:
                continue
            if cutoff is not None:
                stamp = parse_time(note.updated_time or note.created_time)
                if stamp is None or stamp.timestamp() < cutoff:
                    continue
            selected.append(note)
        return selected

    async def recall_with_diagnostics(
        self,
        query_or_history,
        *,
        k=None,
        now=None,
        limit=None,
        kinds: Sequence[str] | None = None,
        evidence: str | None = None,
        scope: str | None = None,
        since_days: int | None = None,
    ) -> tuple[list[ScoredNote], RecallDiagnostics]:
        ...
        moment = now or _now()
        candidates = self._candidates(
            kinds=kinds, evidence=evidence, scope=scope, since_days=since_days, now=moment
        )
        result = rank_notes_detailed(candidates, query, params=self._params(), now=moment,
                                    limit=supply if limit is None else limit)
        ...
```

`recall_notes` 把 `kinds` / `evidence` / `scope` / `since_days` 一路透传；`time` / `parse_time` /
`MemoryNote` 需要 import 进 `service.py`。

`ai_coding/tools/memory_search_tool.py`：

```python
        recall, diagnostics = _recall_with_diagnostics(
            self._memory,
            query,
            k=_as_int(params.get("k"), 3),
            kinds=_strings(params.get("kind")),
            evidence=str(params.get("evidence", "") or "").strip().casefold() or None,
            scope=_optional(params.get("scope")),
            since_days=_optional_int(params.get("since_days")),
        )
        hits = recall            # filtering now happens inside the service
```

`_recall_with_diagnostics`（Task 4 已引入）继续把关键字透传；老 fake 没有
`recall_with_diagnostics` 时退回 `recall_notes`：

```python
def _recall_with_diagnostics(memory: Any, query: str, **kwargs: Any) -> tuple[list[Any], Any]:
    detailed = getattr(memory, "recall_with_diagnostics", None)
    if detailed is None:
        return list(memory.recall_notes(query, k=kwargs.get("k", 3))), _EMPTY_DIAGNOSTICS
    return detailed(query, **kwargs)
```

**同时删掉 Task 4 引入的 `_filtered()`**：过滤已经搬进 service，留在工具层只会变成两条
互相打架的路径（`grep -n "_filtered" ai_coding/tools/memory_search_tool.py` 必须为空）。

`params_json_schema` 再补两个属性：

```python
                "scope": {
                    "type": "string",
                    "description": "Optional filter: 'workspace' or a session id.",
                },
                "since_days": {
                    "type": "integer",
                    "description": "Optional: only memories updated in the last N days.",
                },
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_memory_service.py tests/test_memory_service_v2.py tests/test_tool_memory_search.py tests/test_memory_evidence.py tests/test_agent_loop_memory.py -o addopts="" -q`
Expected: 全绿

Run: `.venv/bin/python scripts/retrieval_cases.py && .venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 指标不低于 Task 2 的记录

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/service.py ai_coding/tools/memory_search_tool.py tests/test_memory_service.py tests/test_tool_memory_search.py
git commit -m "R10c: filters narrow the search before it is ranked"
```

---

### Task 6: 可选的向量索引（默认关，标准库实现）

**Files:**
- Create: `ai_coding/memory/embeddings.py`
- Modify: `ai_coding/config/models.py`（`EmbeddingsConfig` + `MemoryConfig.embeddings`）
- Test: `tests/test_memory_embeddings.py`（新建，9 条）

**Interfaces:**
- Produces: `EmbeddingSettings(enabled, provider, model, dimensions, min_cosine, cache_path, timeout_seconds)`；
  `EmbeddingIndex(settings, *, base_url, api_key, cache_path=None, transport=None)` 与
  `.available` / `.vectors_for(notes)` / `.scores(query, notes) -> dict[str, float] | None`；
  `config.models.EmbeddingsConfig`
- 约束：**只用标准库**（`urllib` + `json` + `hashlib`），不 import numpy；
  `transport` 可注入，测试永不联网

- [ ] **Step 1: 写红测试**

```python
"""TDD (R10d): the optional semantic tier — off by default, harmless when broken."""

import json

from ai_coding.config.models import MemoryConfig
from ai_coding.memory.embeddings import EmbeddingIndex, EmbeddingSettings
from ai_coding.memory.note import MemoryNote


def _note(note_id: str, text: str) -> MemoryNote:
    return MemoryNote(id=note_id, text=text, created_time="2026-09-01T00:00:00+00:00")


class _Transport:
    """Counts calls and returns an obvious vector per text (no network)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self.fail = fail

    def __call__(self, texts):
        self.calls += 1
        self.texts.extend(texts)
        if self.fail:
            raise RuntimeError("provider down")
        return [[float(len(text)), 1.0, 0.0] for text in texts]


def _index(tmp_path, transport, **over):
    settings = EmbeddingSettings(
        enabled=True, provider="qwen", model="text-embedding-v4", dimensions=3,
        min_cosine=0.5, **over,
    )
    return EmbeddingIndex(
        settings, base_url="https://example.invalid/v1", api_key="secret",
        cache_path=str(tmp_path / "embeddings.json"), transport=transport,
    )


def test_r10d_disabled_is_inert_and_creates_nothing(tmp_path):
    transport = _Transport()
    index = EmbeddingIndex(
        EmbeddingSettings(enabled=False), base_url="https://x/v1", api_key="k",
        cache_path=str(tmp_path / "embeddings.json"), transport=transport,
    )
    assert index.available is False
    assert index.scores("依赖", [_note("a", "依赖用 uv")]) is None
    assert transport.calls == 0
    assert not (tmp_path / "embeddings.json").exists()


def test_r10d_enabled_without_a_provider_is_still_inert(tmp_path):
    transport = _Transport()
    index = EmbeddingIndex(_empty_settings(), base_url="", api_key="",
                           cache_path=str(tmp_path / "e.json"), transport=transport)
    assert index.available is False
    assert index.scores("依赖", [_note("a", "依赖用 uv")]) is None
    assert transport.calls == 0


def test_r10d_vectors_are_embedded_once_and_cached(tmp_path):
    transport = _Transport()
    index = _index(tmp_path, transport)
    notes = [_note("a", "依赖用 uv"), _note("b", "压缩有四层")]

    first = index.vectors_for(notes)
    second = index.vectors_for(notes)

    assert set(first) == {"a", "b"} and set(second) == {"a", "b"}
    assert transport.calls == 1                      # the second read hit the cache
    cached = json.loads((tmp_path / "embeddings.json").read_text(encoding="utf-8"))
    assert len(cached) == 2


def test_r10d_a_corrupt_cache_is_rebuilt_without_raising(tmp_path):
    transport = _Transport()
    (tmp_path / "embeddings.json").write_text("not json", encoding="utf-8")
    index = _index(tmp_path, transport)

    assert set(index.vectors_for([_note("a", "依赖用 uv")])) == {"a"}
    assert transport.calls == 1


def test_r10d_changing_the_model_invalidates_the_cache(tmp_path):
    transport = _Transport()
    notes = [_note("a", "依赖用 uv")]
    _index(tmp_path, transport).vectors_for(notes)
    other = EmbeddingIndex(
        EmbeddingSettings(enabled=True, provider="qwen", model="text-embedding-v3",
                          dimensions=3, min_cosine=0.5),
        base_url="https://example.invalid/v1", api_key="secret",
        cache_path=str(tmp_path / "embeddings.json"), transport=transport,
    )
    other.vectors_for(notes)
    assert transport.calls == 2


def test_r10d_a_failing_provider_returns_none_never_raises(tmp_path):
    index = _index(tmp_path, _Transport(fail=True))
    assert index.vectors_for([_note("a", "依赖用 uv")]) == {}
    assert index.scores("依赖", [_note("a", "依赖用 uv")]) is None
    assert not (tmp_path / "embeddings.json").exists()   # nothing half-written


def test_r10d_scores_rank_the_nearer_vector_first(tmp_path):
    transport = _Transport()
    index = _index(tmp_path, transport)
    notes = [_note("short", "uv"), _note("long", "这是一段长得多的无关文本内容")]

    scores = index.scores("uv", notes)

    assert scores is not None and set(scores) == {"short", "long"}
    # both vectors are [len(text), 1, 0]; the query is "uv" (2 chars) -> shortest text is nearest
    assert scores["short"] > scores["long"]


def test_r10d_the_cache_key_covers_dimensions(tmp_path):
    transport = _Transport()
    notes = [_note("a", "依赖用 uv")]
    _index(tmp_path, transport).vectors_for(notes)
    wider = EmbeddingIndex(
        EmbeddingSettings(enabled=True, provider="qwen", model="text-embedding-v4",
                          dimensions=8, min_cosine=0.5),
        base_url="https://example.invalid/v1", api_key="secret",
        cache_path=str(tmp_path / "embeddings.json"), transport=transport,
    )
    wider.vectors_for(notes)
    assert transport.calls == 2


def test_r10d_config_defaults_are_off():
    config = MemoryConfig()
    assert config.embeddings.enabled is False
    assert config.embeddings.model == "text-embedding-v4"
    assert config.embeddings.min_cosine == 0.55
    assert MemoryConfig().recall_relative_band == 0.55
```

（帮助函数 `_empty_settings()` 返回 `EmbeddingSettings(enabled=True, provider="")`。）

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_memory_embeddings.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_coding.memory.embeddings'`

- [ ] **Step 3: 最小实现**

`ai_coding/memory/embeddings.py`：

```python
"""Optional semantic second opinion for memory_search (R10d).

Off by default and never required.  Without a provider, a network, a cache file or
numpy, every caller behaves exactly as the lexical path does.  Standard library
only: one POST to the provider's /embeddings endpoint, vectors in a JSON file next
to the memories, similarity as a plain Python dot product (200 notes x 1024 dims
is ~10 ms; docs/r10-analysis.md 1.3).
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ai_coding.memory.note import MemoryNote

__all__ = ["EmbeddingIndex", "EmbeddingSettings", "Transport"]

Transport = Callable[[Sequence[str]], list[list[float]]]
DEFAULT_MODEL = "text-embedding-v4"
_BATCH = 10


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool = False
    provider: str = ""
    model: str = DEFAULT_MODEL
    dimensions: int = 1024
    min_cosine: float = 0.55
    cache_path: str = ""
    timeout_seconds: float = 10.0


class EmbeddingIndex:
    """A cache of note vectors plus the cosine scores a search needs."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        base_url: str = "",
        api_key: str = "",
        cache_path: str | Path | None = None,
        transport: Transport | None = None,
    ) -> None:
        self._settings = settings
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._path = Path(cache_path or settings.cache_path or ".memory/embeddings.json")
        self._transport = transport
        self._cache: dict[str, list[float]] = self._load()

    # ---- availability -----------------------------------------------------

    @property
    def available(self) -> bool:
        """Everything a call needs is present.  No network is touched here."""
        return bool(
            self._settings.enabled
            and self._settings.model
            and self._base_url
            and self._api_key
        )

    # ---- cache ------------------------------------------------------------

    def _key(self, note: MemoryNote) -> str:
        raw = f"{self._settings.model}|{self._settings.dimensions}|{note.dedupe_key}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _load(self) -> dict[str, list[float]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        wanted = int(self._settings.dimensions or 0)
        out: dict[str, list[float]] = {}
        for key, value in data.items():
            if not isinstance(value, list) or not value:
                continue
            if wanted and len(value) != wanted:
                continue          # dimensions changed: drop and re-embed
            try:
                out[str(key)] = [float(item) for item in value]
            except (TypeError, ValueError):
                continue
        return out

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._cache), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    # ---- embedding --------------------------------------------------------

    def _http(self, texts: Sequence[str]) -> list[list[float]]:
        body = json.dumps(
            {
                "model": self._settings.model,
                "input": list(texts),
                **({"dimensions": self._settings.dimensions} if self._settings.dimensions else {}),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self._settings.timeout_seconds) as reply:
            payload = json.loads(reply.read().decode("utf-8"))
        return [list(item["embedding"]) for item in payload["data"]]

    def _embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """Batch through the transport; ``None`` on any failure (never raises)."""
        if not self.available or not texts:
            return None
        sender = self._transport or self._http
        out: list[list[float]] = []
        try:
            for start in range(0, len(texts), _BATCH):
                out.extend(sender(list(texts[start : start + _BATCH])))
        except Exception:  # noqa: BLE001 — the semantic tier is strictly best-effort
            return None
        if len(out) != len(texts):
            return None
        return out

    def vectors_for(self, notes: Sequence[MemoryNote]) -> dict[str, tuple[float, ...]]:
        """Vectors for these notes, embedding only what the cache misses."""
        if not self.available:
            return {}
        missing = [note for note in notes if self._key(note) not in self._cache]
        if missing:
            produced = self._embed([note.text for note in missing])
            if produced is None:
                return {
                    note.id: tuple(self._cache[self._key(note)])
                    for note in notes
                    if self._key(note) in self._cache
                }
            for note, vector in zip(missing, produced):
                self._cache[self._key(note)] = [float(value) for value in vector]
            self._save()
        return {
            note.id: tuple(self._cache[self._key(note)])
            for note in notes
            if self._key(note) in self._cache
        }

    def scores(self, query: str, notes: Sequence[MemoryNote]) -> dict[str, float] | None:
        """Cosine between the query and each note; ``None`` when unavailable."""
        if not self.available or not query.strip():
            return None
        query_vector = self._embed([query])
        if query_vector is None:
            return None
        vectors = self.vectors_for(notes)
        if not vectors:
            return None
        head = query_vector[0]
        head_norm = math.sqrt(sum(value * value for value in head)) or 1.0
        out: dict[str, float] = {}
        for note_id, vector in vectors.items():
            dot = sum(a * b for a, b in zip(head, vector))
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            out[note_id] = dot / (head_norm * norm)
        return out
```

`ai_coding/config/models.py`（`MemoryConfig` 内新增字段 + 新类）：

```python
class EmbeddingsConfig(BaseModel):
    """R10d: optional semantic second opinion.  Off by default (D-R10-7).

    Measured: the deepseek endpoint has no /embeddings at all, so a default-on
    switch would break half of our own demo configs.  Nothing in the retrieval
    path requires this block to exist.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    provider: str = ""
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    min_cosine: float = 0.55
    cache_path: str = ""
    timeout_seconds: float = 10.0


# inside MemoryConfig:
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
```

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_memory_embeddings.py -o addopts="" -q`
Expected: **9 passed**（全程无网络：transport 全是注入的）

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/embeddings.py ai_coding/config/models.py tests/test_memory_embeddings.py
git commit -m "R10d: the semantic tier is optional, off by default and stdlib only"
```

---

### Task 7: 语义分怎么进门禁与排序（以及它坏掉的每一种样子）

**Files:**
- Modify: `ai_coding/memory/relevance.py`（`rank_notes_detailed` 接受可选语义分）
- Create: `ai_coding/memory/factory.py`
- Modify: `ai_coding/memory/service.py`、`ai_coding/cli.py`
- Test: `tests/test_memory_semantic.py`（新建，6 条）

**Interfaces:**
- Produces: `rank_notes_detailed(..., semantic_scores: Mapping[str, float] | None = None)`
  （融合公式见下，**实测 R@1 0.78 / R@3 0.93 / range 9/12**）；
  `memory.factory.build_memory_service(app_cfg, workspace) -> MemoryService`
- 语义不可用时的行为：`RecallDiagnostics.semantic_unavailable = True`，**结果与纯词面逐条一致**

- [ ] **Step 1: 写红测试**

```python
"""TDD (R10d): the semantic tier changes the ranking when it works, and nothing when it does not."""

import asyncio

from ai_coding.config.models import AppConfig
from ai_coding.memory.factory import build_memory_service
from ai_coding.memory.note import MemoryNote
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore


class _Index:
    def __init__(self, scores=None, *, available=True):
        self.available = available
        self._scores = scores or {}
        self.calls = 0

    def scores(self, query, notes):
        self.calls += 1
        return self._scores if self.available else None


def _service(tmp_path, texts, index):
    store = MemoryStore(tmp_path / "m.json")
    for note_id, text in texts:
        store.add_note(MemoryNote(id=note_id, text=text,
                                  created_time="2026-09-01T00:00:00+00:00"))
    return MemoryService(store, embeddings=index), store


def test_r10d_without_an_index_the_result_is_the_lexical_one(tmp_path):
    """No index configured => the lexical answer, and no semantic_unavailable flag."""
    service, _ = _service(tmp_path, [("a", "依赖管理用 uv")], None)
    hits, diagnostics = asyncio.run(service.recall_with_diagnostics("依赖怎么装", k=3))
    assert [hit.note.id for hit in hits] == ["a"]
    assert diagnostics.semantic_unavailable is False


def test_r10d_a_dead_index_is_reported_not_fatal(tmp_path):
    service, _ = _service(tmp_path, [("a", "依赖管理用 uv")], _Index(available=False))
    hits, diagnostics = asyncio.run(
        service.recall_with_diagnostics("依赖怎么装", k=3)
    )
    assert [hit.note.id for hit in hits] == ["a"]
    assert diagnostics.semantic_unavailable is True


def test_r10d_a_working_index_recalls_what_words_cannot(tmp_path):
    """The paraphrase case: zero lexical overlap, rescued by cosine (D-R10-8)."""
    texts = [("cache", "提示词缓存的关键是前缀稳定"), ("other", "今天要把 CLI 章节重写")]
    service, _ = _service(tmp_path, texts, _Index({"cache": 0.91, "other": 0.10}))
    hits, diagnostics = asyncio.run(
        service.recall_with_diagnostics("担心模型把前面发过的东西再算一次钱", k=3)
    )
    assert [hit.note.id for hit in hits] == ["cache"]
    assert diagnostics.semantic_unavailable is False


def test_r10d_semantics_never_lift_a_candidate_above_the_band(tmp_path):
    texts = [("best", "依赖管理用 uv"), ("weak", "依赖这个词出现过")]
    service, _ = _service(tmp_path, texts, _Index({"best": 0.80, "weak": 0.79}))
    hits, _ = asyncio.run(service.recall_with_diagnostics("依赖怎么装", k=5))
    assert [hit.note.id for hit in hits][0] == "best"
    assert len(hits) <= 2


def test_r10d_a_dead_provider_never_costs_a_note(tmp_path):
    """Writing a memory must not depend on the semantic tier being up."""
    service, store = _service(tmp_path, [], _Index(available=False))
    report = service.write_note("依赖管理用 uv", kind="decision")
    assert report.added and store.count == 1


def test_r10d_the_factory_leaves_it_off_by_default(tmp_path):
    config = AppConfig()
    service = build_memory_service(config, str(tmp_path))
    assert service.embeddings is None
```

- [ ] **Step 2: 跑红**

Run: `.venv/bin/python -m pytest tests/test_memory_semantic.py -o addopts="" -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'ai_coding.memory.factory'`

- [ ] **Step 3: 最小实现**

`ai_coding/memory/relevance.py` 里 `rank_notes_detailed` 增加参数并在打分处融进来：

```python
    semantic_scores: Mapping[str, float] | None = None,
...
    raw = bm25_scores(...)
    best_raw = max(raw.values()) or 1.0
    semantic = dict(semantic_scores or {})
    cosine_low = min(semantic.values()) if semantic else 0.0
    cosine_span = (max(semantic.values()) - cosine_low) if semantic else 0.0
    for note in active:
        ...
        rank_term = raw.get(note.id, 0.0) / best_raw
        if semantic and cosine_span > 0:
            cosine = float(semantic.get(note.id, 0.0))
            # min-max over the candidate set keeps BM25 and cosine on one scale
            rank_term = 0.5 * rank_term + 0.5 * ((cosine - cosine_low) / cosine_span)
        score = (
            resolved.weight_rank * rank_term
            + resolved.weight_recency * freshness
            + resolved.weight_importance * importance
            + boost
        )
        semantic_ok = bool(semantic) and float(semantic.get(note.id, 0.0)) >= resolved.min_cosine
        gate = item.relevance >= resolved.min_relevance or semantic_ok
```

（`RecallParams` 增 `min_cosine: float = 0.55`；`from_memory_config` 从
`config.embeddings.min_cosine` 读取，但要防御 `embeddings` 块不存在的情况：
`getattr(getattr(config, "embeddings", None), "min_cosine", 0.55)`。）

`ai_coding/memory/factory.py`：

```python
"""Composition-root helper for memory (R10d).

Kept out of the service so the service stays constructible with a plain store in
tests, and kept here (not in cli.py) so both the CLI and the acceptance scripts
assemble memory the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_coding.memory.embeddings import EmbeddingIndex, EmbeddingSettings
from ai_coding.memory.service import MemoryService
from ai_coding.memory.store import MemoryStore

__all__ = ["build_memory_service"]


def build_memory_service(app_cfg: Any, workspace: str, *, extractor: Any | None = None) -> MemoryService:
    """Memory, plus the optional semantic index when it is configured (D-R10-7)."""
    root = Path(workspace).expanduser()
    store = MemoryStore(root / ".memory")
    config = getattr(app_cfg, "memory", None)
    index = None
    settings = getattr(config, "embeddings", None)
    if settings is not None and getattr(settings, "enabled", False):
        provider = str(getattr(settings, "provider", "") or "")
        model_cfg = (getattr(app_cfg, "models", None) or {}).get(provider)
        if model_cfg is not None:
            index = EmbeddingIndex(
                EmbeddingSettings(
                    enabled=True,
                    provider=provider,
                    model=str(getattr(settings, "model", "text-embedding-v4")),
                    dimensions=int(getattr(settings, "dimensions", 1024)),
                    min_cosine=float(getattr(settings, "min_cosine", 0.55)),
                    cache_path=str(getattr(settings, "cache_path", "") or (root / ".memory.embeddings.json")),
                    timeout_seconds=float(getattr(settings, "timeout_seconds", 10.0)),
                ),
                base_url=str(getattr(model_cfg, "base_url", "")),
                api_key=str(getattr(model_cfg, "api_key", "")),
            )
    return MemoryService(store, extractor=extractor, config=config, embeddings=index)
```

`MemoryService.__init__` 增可选参数 `embeddings: Any | None = None`，暴露 `self.embeddings`；
`recall_with_diagnostics` 在排序前拿语义分：

```python
        semantic: dict[str, float] | None = None
        unavailable = False
        if self._embeddings is not None and getattr(self._embeddings, "available", False):
            try:
                semantic = self._embeddings.scores(query, candidates)
            except Exception:  # noqa: BLE001 — semantics are best-effort
                semantic = None
            if semantic is None:
                unavailable = True
        result = rank_notes_detailed(
            candidates, query, params=self._params(), now=moment,
            limit=supply if limit is None else limit, semantic_scores=semantic,
        )
        ... RecallDiagnostics(..., semantic_unavailable=unavailable)
```

`ai_coding/cli.py` 的 `MemoryService(...)` 那处换成 `build_memory_service(app_cfg, ws, extractor=extractor)`
（若同函数里 `memory_store` 另有用途则保留它）。

- [ ] **Step 4: 跑绿 + 门禁**

Run: `.venv/bin/python -m pytest tests/test_memory_semantic.py tests/test_memory_embeddings.py tests/test_tool_memory_search.py tests/test_memory_service.py -o addopts="" -q`
Expected: 全绿

Run: `.venv/bin/python scripts/retrieval_cases.py`
Expected: 与 Task 2 记录一致（**关着 embeddings 时不许有任何变化**）

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add ai_coding/memory/relevance.py ai_coding/memory/factory.py ai_coding/memory/service.py ai_coding/cli.py tests/test_memory_semantic.py
git commit -m "R10d: the semantic second opinion, and every way it may fail"
```

---

### Task 8: 验收脚本 + 文档同步 + 全量门禁

**Files:**
- Create: `scripts/r10_cases.py`
- Modify: `scripts/retrieval_cases.py`（补 `--with-embeddings`）
- Modify: `docs/features.md`、`docs/data-contracts.md`、`docs/architecture.md`、
  `docs/how-to-run.md`、`docs/integration_testing.md`、`docs/r7-backlog.md`
- 并给 `docs/r10-plan.md` 追加「执行状态」

**Interfaces:**
- Produces: `scripts/r10_cases.py`（离线 12 条 + `--with-semantic` 1 条真实）

- [ ] **Step 1: 写验收脚本**

沿用 `scripts/r9_cases.py` 的形状（一个 case 一个函数，返回 `(ok, detail)`）。离线 12 条：

```python
async def the_floor_kills_a_loud_irrelevant_note(tmp: Path) -> tuple[bool, str]:
    notes = [
        _note("loud", "今天要把 docs 目录里的 CLI 章节重写一遍", importance=5, age=0),
        _note("real", "上下文压缩有四层：L3 落盘、L1 裁剪、L2 信封、L4 摘要", importance=4, age=30),
    ]
    kept = [hit.note.id for hit in rank_notes(notes, "上下文压缩分几层", params=RecallParams(), now=NOW)]
    return kept == ["real"], f"kept={kept}"


async def bm25_prefers_the_specific_note(tmp: Path) -> tuple[bool, str]: ...


async def the_relative_band_cuts_the_tail(tmp: Path) -> tuple[bool, str]: ...


async def a_hit_counts_as_a_use(tmp: Path) -> tuple[bool, str]:
    service, store = _service(tmp, [("a", "依赖管理用 uv")])
    hits = await service.recall_notes("依赖怎么装", k=3)
    stored = store.get("a")
    ok = bool(hits) and stored is not None and stored.hits == 1
    return ok, f"hits={stored.hits if stored else '?'}"


async def a_miss_touches_nothing(tmp: Path) -> tuple[bool, str]: ...


async def a_miss_is_structured(tmp: Path) -> tuple[bool, str]:
    service, _ = _service(tmp, [("a", "payment service timeout")])
    result = await MemorySearchTool(service).execute({"query": "依赖怎么装"})
    data = result.data or {}
    ok = (
        result.success is True
        and data.get("no_match") is True
        and data.get("notes") == []
        and data.get("terms_unknown") == ["依赖"]
        and isinstance(data.get("index"), dict)
    )
    return ok, f"no_match={data.get('no_match')} unknown={data.get('terms_unknown')}"


async def a_miss_says_what_to_do_next(tmp: Path) -> tuple[bool, str]: ...


async def the_index_map_stays_bounded(tmp: Path) -> tuple[bool, str]:
    """300 notes must not turn the diagnosis into a context bomb."""
    ...
    size = len(json.dumps(data["index"], ensure_ascii=False))
    return size < 400, f"{size} chars"


async def kind_filter_does_not_shrink_the_result(tmp: Path) -> tuple[bool, str]: ...


async def scope_and_since_narrow_the_search(tmp: Path) -> tuple[bool, str]: ...


async def the_benchmark_guardrails_hold(tmp: Path) -> tuple[bool, str]:
    notes, cases = load_corpus(ROOT / "scripts" / "retrieval_corpus.json", now=NOW)
    rank = lambda query: [h.note.id for h in rank_notes(notes, query, params=RecallParams(), now=NOW, limit=5)]
    metrics = evaluate(rank, cases)
    paraphrase = [c for c in cases if c.category == "paraphrase"]
    para_hits = evaluate(rank, paraphrase).recall_at(1) * len(paraphrase)
    ok = metrics.recall_at(3) >= 0.80 and para_hits >= 5
    return ok, f"R@3={metrics.recall_at(3):.2f} paraphrase={para_hits:.0f}/{len(paraphrase)}"


async def embeddings_off_is_inert(tmp: Path) -> tuple[bool, str]:
    """A dead index must leave the answer byte-identical (user requirement)."""
    service, _ = _service(tmp, [("a", "依赖管理用 uv")])
    plain = await service.recall_notes("依赖怎么装", k=3)
    _, diagnostics = await service.recall_with_diagnostics("依赖怎么装", k=3)
    ...
```

再加一条真实用例（`--with-semantic`）：

```python
async def real_semantic_search(tmp: Path) -> tuple[bool, str]:
    """Real dashscope embeddings: the paraphrase case that words alone cannot reach."""
    service = _service_with_real_embeddings(tmp)     # provider=qwen from config.local.yaml
    hits, diagnostics = await service.recall_with_diagnostics(
        "担心模型把前面发过的东西再算一次钱", k=3
    )
    ok = diagnostics.semantic_unavailable is False and bool(hits)
    return ok, f"semantic_unavailable={diagnostics.semantic_unavailable} hits={len(hits)}"
```

Run: `.venv/bin/python scripts/r10_cases.py`
Expected: **12/12**

Run: `.venv/bin/python scripts/r10_cases.py --with-semantic --config config.local.yaml`
Expected: **13/13**（真实 embeddings；`semantic_unavailable=False`）

- [ ] **Step 2: 补 `--with-embeddings` 到基准脚本**

```python
    if args.with_embeddings:
        from ai_coding.memory.factory import build_memory_service

        index = build_memory_service(_app_config(args.config), str(ROOT)).embeddings
        notes = list(notes)
        vectors = index.vectors_for(notes) if index is not None and index.available else {}
        ...
        # 用 semantic_scores 走 rank_notes_detailed，再 evaluate 一次，打第二行
```

Run: `.venv/bin/python scripts/retrieval_cases.py --with-embeddings --config config.local.yaml`
Expected: 两行——`current`（R@3 ≈ 0.85）与 `current+semantic`（R@3 ≈ 0.93，paraphrase 9/12）

- [ ] **Step 3: 更新文档**

按 `docs/r10-analysis.md` §12 的清单逐条改：
`features.md`（两段门禁 / 可行动的 no_match / 可选 embeddings 与降级矩阵）、
`data-contracts.md`（`memory_search` 的 data、`RecallParams` 新字段、`touch_notes`、`EmbeddingsConfig`）、
`architecture.md`（检索主线 + 诊断回路 + 可选语义支路，含图）、
`how-to-run.md`（新配置、`min_relevance` 默认值变化、怎么开 embeddings、怎么跑基准）、
`integration_testing.md`（`scripts/retrieval_cases.py` / `scripts/r10_cases.py` 的跑法与基线数字）、
`r7-backlog.md`（4.1 标注 **R10 已裁决不采用**，5.7 指向本轮的检索基准）。

- [ ] **Step 4: 全量门禁**

Run: `.venv/bin/python -m pytest tests/ -o addopts="" -q`
Expected: **≥ 1160 passed / 4 skipped**（1122 起步 + 本轮约 47 条新增）

Run: `.venv/bin/python -m ruff check . && .venv/bin/python -m mypy ai_coding`
Expected: 全绿

Run: `.venv/bin/python scripts/memory_cases.py && .venv/bin/python scripts/r7e_cases.py && .venv/bin/python scripts/r8_cases.py && .venv/bin/python scripts/r9_cases.py`
Expected: 11/11、9/9、13/13、12/12（回归）

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "R10: the acceptance script, and the docs the round changed"
```

---

## Self-Review

**1. Spec coverage**（`docs/r10-analysis.md` → 本计划）

| Spec | Task |
|---|---|
| §0 R10a 评测基准（+ D-R10-1 护栏） | 1, 8 |
| §0 R10b 打分与门禁（+ D-R10-2/3） | 2 |
| §1.2 使用信号（+ D-R10-4/5） | 3 |
| §5.2 `memory_search` 的 data 与 no_match 文案 | 4 |
| §5.1 `scope` / `since_days` / 先过滤后排序 | 5 |
| §5.5 `EmbeddingsConfig`（+ D-R10-7） | 6 |
| §5.3 语义分融入门禁与排序（+ D-R10-8） | 7 |
| §6.4 降级矩阵（7 种情形） | 4, 6, 7, 8 |
| §6.2 配置默认值（`recall_min_relevance` / `recall_relative_band`） | 2 |
| §13 明确不做 | Global Constraints + 无对应任务（正确） |

**2. Placeholder scan**：无 TBD/TODO；每个 Step 都有可执行的测试代码、实现代码或命令。
Task 8 的 `...` 只出现在「与紧邻完整例子形状完全相同」的验收用例上（与 R9 计划同一处理方式），
实现时照抄 `a_miss_is_structured` 的骨架即可。

**3. Type consistency**：`EvalCase` / `Metrics` / `evaluate` / `format_metrics` / `load_corpus`、
`bm25_scores` / `RankingResult` / `rank_notes_detailed` / `ScoredNote.matched_terms`、
`RecallParams.relative_band` / `weight_rank` / `bm25_k1` / `bm25_b` / `recency_source` / `min_cosine`、
`MemoryStore.touch_notes`、`IndexSummary` / `RecallDiagnostics` / `recall_with_diagnostics`、
`EmbeddingSettings` / `EmbeddingIndex` / `EmbeddingsConfig`、`build_memory_service`
在 Task 1–8 中的引用与首次定义一致。

**4. 与实测的对应**：Task 2 的期望值（R@1 0.72 / R@3 0.85 / FP@5 0.32）与 Task 8 的语义期望值
（R@1 0.78 / R@3 0.93 / paraphrase 9/12）**都来自本轮对「最终公式」的实测**，
不是从 §1.4 那张对照表里搬的——两者数值不同，原因写在 `docs/r10-analysis.md` §1.4 的注里。

## Execution Handoff

用户已选 **Inline Execution**（「不准派子任务，自己实现」）。按 Task 1 → 8 顺序执行，
每个 Task 先跑红、再实现、跑绿、单独提交；**唯一允许改动的既有断言**在 Global Constraints 里写明了。
