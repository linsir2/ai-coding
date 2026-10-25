#!/usr/bin/env python3
"""Memory-layer acceptance cases (R3c) — offline, no network, exit 0 only if all pass.

Every case is one of the ten measured failures from docs/r3-analysis.md section 1,
re-run against the current implementation.  These are the "did it actually help?"
checks: the same scenarios that used to return nothing, duplicate five times, rank
the oldest first, delete the security rule, or lose an early decision.

    .venv/bin/python scripts/memory_cases.py [--only name,name]

An extra case (11) measures the prompt-cache prefix against the two compaction
regimes, because that is the cost side of the same design.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.ai.prompt import PromptAssembler  # noqa: E402
from ai_coding.config.models import AIConfig, MemoryConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.core.context_compression import ContextManager  # noqa: E402
from ai_coding.domain.loop import StepResult  # noqa: E402
from ai_coding.domain.message import ChatMessage  # noqa: E402
from ai_coding.memory.note import NoteKind, new_note  # noqa: E402
from ai_coding.memory.schema import HarvestCandidate  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402

SEP = "-" * 64
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _user(content: str) -> ChatMessage:
    return ChatMessage(role="user", content=content, timestamp="t")


def _service(store: MemoryStore, **overrides: Any) -> MemoryService:
    return MemoryService(store, config=MemoryConfig(**overrides))


class _Extractor:
    """Records the batches it is handed and answers with fixed candidates."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.seen: list[list[ChatMessage]] = []

    async def __call__(self, messages, conclusion=None):
        self.seen.append(list(messages))
        return list(self.items)


class _Engine(ChatEngine):
    def __init__(self, reply: str = "好的") -> None:
        self.reply = reply
        self.requests: list[dict[str, Any]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(
            {
                "instructions": instructions or "",
                "messages": [message.content or "" for message in messages],
            }
        )
        return StepResult(text=self.reply, finish_reason="stop")


def _loop(
    engine: ChatEngine,
    root: Path,
    *,
    memory: MemoryService | None = None,
    settings: AIConfig | None = None,
    summarizer=None,
    context: Any = None,
) -> AgentLoop:
    cfg = settings or AIConfig(stop_policy="natural", max_tool_iterations=1)
    return AgentLoop(
        engine,
        SessionService(root / "sessions"),
        ToolRegistry(),
        context=context
        if context is not None
        else ContextManager(cfg, transcripts_root=root, summarizer=summarizer),
        memory=memory,
        prompt=PromptAssembler("You are a coding assistant."),
        settings=cfg,
        workspace=str(root),
    )


# ---- the eleven cases ------------------------------------------------------


async def case_1_chinese_recall(root: Path) -> tuple[bool, str]:
    """Used to return nothing: the whole sentence was one keyword."""
    store = MemoryStore(root / "m.json")
    store.add_note(new_note("这个项目用 uv 管理依赖，不要用 pip"))
    hits = await _service(store).recall_notes([_user("帮我给这个项目加一个依赖")])
    return bool(hits), f"hits={[h.note.text for h in hits]}"


async def case_2_ranking(root: Path) -> tuple[bool, str]:
    """Used to return insertion order (oldest first)."""
    store = MemoryStore(root / "m.json")
    for text, age in [
        ("alpha uses dependency X", 10),
        ("beta unrelated note", 9),
        ("gamma uses dependency Y", 8),
        ("epsilon uses dependency Z", 2),
    ]:
        store.add_note(new_note(text, when=_stamp(age)))
    hits = await _service(store).recall_notes([_user("how do I add a dependency")], k=3)
    order = [h.note.text.split()[0] for h in hits]
    return order[:3] == ["epsilon", "gamma", "alpha"], f"order={order}"


async def case_3_dedupe(root: Path) -> tuple[bool, str]:
    """Used to store (and inject) the same fact five times."""
    store = MemoryStore(root / "m.json")
    service = _service(store, max_index_entries=50)
    for _ in range(5):
        store.add_note(new_note("记住：项目用 uv 管理依赖"))
    hits = await service.recall_notes([_user("uv 管理依赖")], k=10)
    return store.count == 1 and len(hits) == 1, f"store={store.count} injected={len(hits)}"


async def case_4_supersede(root: Path) -> tuple[bool, str]:
    """A contradiction used to leave both notes alive and contradictory."""
    store = MemoryStore(root / "m.json")
    old = store.add_note(new_note("项目用 uv 管理依赖", kind=NoteKind.DECISION))
    extractor = _Extractor(
        HarvestCandidate(
            text="项目改用 poetry 管理依赖",
            kind=NoteKind.DECISION,
            action="supersede",
            target_id=old,
        )
    )
    service = MemoryService(store, extractor=extractor, config=MemoryConfig())
    await service.harvest([_user("我们改成 poetry 了")])
    active = [note.text for note in store.active()]
    hits = await service.recall_notes([_user("依赖怎么装")], k=5)
    ok = active == ["项目改用 poetry 管理依赖"] and all("poetry" in h.note.text for h in hits)
    return ok, f"active={active} recalled={[h.note.text for h in hits]}"


async def case_5_eviction(root: Path) -> tuple[bool, str]:
    """FIFO used to delete the security rule first."""
    store = MemoryStore(root / "m.json")
    store.add_note(
        new_note("安全红线：任何情况下不得把密钥写进代码", kind=NoteKind.DECISION, pinned=True)
    )
    for index in range(4):
        store.add_note(new_note(f"琐碎事实 {index}：今天第 {index} 次构建通过"))
    service = _service(store, max_index_entries=3)
    await service.harvest([_user("x")])  # any harvest enforces the cap
    texts = [note.text for note in store.all()]
    ok = any("安全红线" in text for text in texts) and len(texts) <= 3
    return ok, f"kept={[text[:12] for text in texts]}"


async def case_6_disabled(root: Path) -> tuple[bool, str]:
    """enabled=false used to still inject."""
    store = MemoryStore(root / "m.json")
    store.add_note(new_note("项目用 uv 管理依赖"))
    service = _service(store, enabled=False)
    hits = await service.recall_notes([_user("uv 依赖")])
    report = await service.harvest([_user("记住：用 poetry")])
    ok = hits == [] and not report.changed and store.count == 1
    return ok, f"recalled={len(hits)} changed={report.changed}"


async def case_7_delta_only(root: Path) -> tuple[bool, str]:
    """The extractor is never handed the whole history, and never sees it twice.

    The harvest is *scheduled*, not awaited (``_schedule_harvest``), so a turn can
    finish before the extractor runs and the delta -- "everything since the
    cursor" -- coalesces the turns that outran extraction.  That is by design
    (``agent_loop.py:1509-1514``).  What must hold is the invariant, not the batch
    count: nothing is lost, nothing is shown twice, and no batch is the whole
    history.
    """
    extractor = _Extractor("事实")
    root_ws = root / "ws"
    root_ws.mkdir()
    memory = MemoryService(
        MemoryStore(root / "m.json"), extractor=extractor, config=MemoryConfig()
    )
    loop = _loop(_Engine(), root_ws, memory=memory)
    session = loop.sessions.create()
    for index in range(4):
        await loop.process_input(session, f"第{index}轮：聊点别的")
    # The harvest chain is background work; drain it so the measurement is complete.
    await loop.flush_harvest()
    sizes = [len(batch) for batch in extractor.seen]
    flat = [message for batch in extractor.seen for message in batch]
    total = len(session.messages)
    identity_chain = len(flat) == total and all(
        left is right for left, right in zip(flat, session.messages, strict=False)
    )
    whole_history = any(len(batch) == total for batch in extractor.seen)
    ok = identity_chain and not whole_history
    return ok, (
        f"batch_sizes={sizes} total={total} "
        f"identity_chain={identity_chain} whole_history={whole_history}"
    )


async def case_8_decision_survives(root: Path) -> tuple[bool, str]:
    """The core case: an early decision used to vanish after compaction."""
    store = MemoryStore(root / "m.json")
    store.add_note(
        new_note(
            "重要决定：所有对外 HTTP 调用必须走 retry 封装",
            kind=NoteKind.DECISION,
            pinned=True,
        )
    )
    memory = _service(store)
    ws = root / "ws"
    ws.mkdir()
    settings = AIConfig(stop_policy="natural", max_tool_iterations=1, max_context_tokens=200)
    engine = _Engine("闲聊回答" * 20)
    loop = _loop(engine, ws, memory=memory, settings=settings)
    session = loop.sessions.create()
    for index in range(13):
        await loop.process_input(session, f"闲聊第{index}轮")
    # R6a added a fourth volatile tail block (the digest ring), so the constraint
    # block is no longer the second-to-last message.  Assert the invariant instead of
    # the position: the decision is *injected into every request* and never becomes
    # part of the stored transcript.
    request_text = "\n".join(engine.requests[-1]["messages"])
    stored = "\n".join(message.content or "" for message in session.messages)
    junk = store.count
    injected = "retry 封装" in request_text
    ok = injected and "retry 封装" not in stored and junk <= 5
    return (
        ok,
        f"决策注入每轮请求={injected} 未进transcript={'retry 封装' not in stored} "
        f"记忆条数={junk}（旧实现是 100 条垃圾）",
    )


async def case_9_relevance_wins(root: Path) -> tuple[bool, str]:
    """k=1 used to return the oldest weak match."""
    store = MemoryStore(root / "m.json")
    store.add_note(new_note("build 相关：构建脚本用 make", when=_stamp(9)))
    store.add_note(new_note("测试相关：pytest 跑全量用例", when=_stamp(8)))
    store.add_note(
        new_note(
            "构建流程：make build 会先跑 lint 再编译，产物在 dist/，失败看 build.log",
            when=_stamp(1),
        )
    )
    hits = await _service(store).recall_notes([_user("make build 失败了")], k=1)
    got = hits[0].note.text if hits else ""
    return "dist/" in got, f"picked={got[:24]}"


async def case_10_no_false_positive(root: Path) -> tuple[bool, str]:
    """A shared common word used to inject unrelated notes."""
    store = MemoryStore(root / "m.json")
    store.add_note(new_note("payment service timeout is configured in config/pay.yaml"))
    store.add_note(new_note("the auth service signs tokens with RS256"))
    hits = await _service(store).recall_notes(
        [_user("please look at the file in the user module")]
    )
    return hits == [], f"injected={[h.note.text[:20] for h in hits]}"


async def case_11_cache_prefix(root: Path) -> tuple[bool, str]:
    """Cost side: append-only turns keep the prompt-cache prefix intact.

    The volatile tail (``<environment>`` / ``<task-state>`` / ``[budget]`` ...) is
    rebuilt every turn and sits *after* the history, so a byte-level ratio over the
    whole request is diluted by it -- and by the workspace path inside it, which is
    why a fixed percentage threshold was both wrong and machine-dependent (87% on a
    short path, 77% at 442 characters).

    What must hold is the invariant, not a percentage: with the tail set aside,
    request N's stable part is an exact prefix of request N+1's.

    The "per-turn-trim" arm reproduces the M3 policy on purpose (a context object
    that compacts on *every* turn), which rewrites the history -- so its stable part
    is *not* a prefix.  That contrast is what the case exists to show.
    """

    #: The tail blocks are recognisable by these markers; the history never is.
    volatile_markers = (
        "<environment>",
        "<task-state>",
        "<referenced-files>",
        "<requested-skills>",
        "[budget]",
    )

    def stable(request: dict) -> str:
        """A request's instructions + history, with the volatile tail set aside."""

        messages = list(request["messages"])
        while messages and any(marker in messages[-1] for marker in volatile_markers):
            messages.pop()
        return request["instructions"] + "\n".join(messages)

    class _AlwaysCompacts:
        """M3 behaviour: run the pipeline every request, no budget check."""

        def __init__(self, inner: ContextManager) -> None:
            self._inner = inner

        async def compress(self, messages):
            return await self._inner.compress(messages)

    shares: dict[str, int] = {}
    intact: dict[str, bool] = {}
    long_reply = "好的，这个模块负责装配与转发，入口在 build()，注意所有超时走 deadline 封装。" * 3
    modern = AIConfig(stop_policy="natural", max_tool_iterations=1, max_messages=400)
    legacy = AIConfig(stop_policy="natural", max_tool_iterations=1, max_messages=20)
    for label, arm_settings, build_context in [
        (
            "append-only",
            modern,
            lambda ws, cfg: ContextManager(cfg, transcripts_root=ws),
        ),
        (
            "per-turn-trim",
            legacy,
            lambda ws, cfg: _AlwaysCompacts(
                ContextManager(cfg, transcripts_root=ws)
            ),
        ),
    ]:
        ws = root / label
        ws.mkdir()
        engine = _Engine(long_reply)
        loop = _loop(
            engine,
            ws,
            settings=arm_settings,
            context=build_context(ws, arm_settings),
        )
        session = loop.sessions.create()
        for index in range(18):
            await loop.process_input(session, f"第{index}轮：看看模块 {index}")
        stable_a = stable(engine.requests[-2])
        stable_b = stable(engine.requests[-1])
        shares[label] = round(100 * len(stable_a) / max(len(stable_b), 1))
        intact[label] = stable_b.startswith(stable_a)
    ok = intact["append-only"] and not intact["per-turn-trim"]
    return ok, (
        f"prefix_intact append-only={intact['append-only']} "
        f"per-turn-trim={intact['per-turn-trim']} "
        f"(stable_share append-only={shares['append-only']}% "
        f"per-turn-trim={shares['per-turn-trim']}%)"
    )


CASES: list[tuple[str, CaseFn]] = [
    ("case1-chinese-recall", case_1_chinese_recall),
    ("case2-ranking", case_2_ranking),
    ("case3-dedupe", case_3_dedupe),
    ("case4-supersede", case_4_supersede),
    ("case5-eviction", case_5_eviction),
    ("case6-disabled", case_6_disabled),
    ("case7-delta-only", case_7_delta_only),
    ("case8-decision-survives", case_8_decision_survives),
    ("case9-relevance", case_9_relevance_wins),
    ("case10-no-false-positive", case_10_no_false_positive),
    ("case11-cache-prefix", case_11_cache_prefix),
]


async def real_case(config_path: str) -> int:
    """Real-LLM A/B (gate ``AI_CODING_MEMORY_IT=1``): does recall actually change an answer?

    Session A states a constraint; session B (a *new* session, same workspace) asks
    for the command to add a dependency.  With memory on the answer must use uv and
    must not print ``pip install``; with memory off the same question is allowed to
    answer with pip — that difference is the evidence that the effect is memory.
    """
    from ai_coding.ai.factory import build_engine_from_config  # noqa: PLC0415
    from ai_coding.ai.llm_helpers import build_extractor  # noqa: PLC0415
    from ai_coding.config.loader import ConfigLoader  # noqa: PLC0415

    app_cfg = ConfigLoader.load_app(config_path)
    model = app_cfg.models[app_cfg.default_model]
    print(f"model={app_cfg.default_model} base_url={model.base_url}")

    async def run_arm(memory_enabled: bool, tag: str) -> str:
        ws = Path(tempfile.mkdtemp(prefix=f"memory_it_{tag}_"))
        memory_cfg = app_cfg.memory.model_copy(update={"enabled": memory_enabled})
        memory = MemoryService(
            MemoryStore(ws / ".memory"),
            extractor=build_extractor(model),
            config=memory_cfg,
        )
        sessions = SessionService(ws / "sessions")
        first = sessions.create()
        engine_a = build_engine_from_config(app_cfg)
        loop_a = _loop(engine_a, ws, memory=memory)
        await loop_a.process_input(
            first,
            "项目约定（请记住）：这个仓库统一用 uv 管理依赖，禁止使用 pip 安装任何东西。",
        )
        await loop_a.aclose()

        second = sessions.create()  # a NEW session: no history at all
        engine_b = build_engine_from_config(app_cfg)
        loop_b = _loop(engine_b, ws, memory=memory)
        answer = await loop_b.process_input(
            second, "帮这个项目加一个依赖，给我具体命令（只要命令）。"
        )
        await loop_b.aclose()
        print(f"  [{tag}] memory_notes={memory.store.count} answer={answer[:120]!r}")
        return answer or ""

    with_memory = await run_arm(True, "with-memory")
    without_memory = await run_arm(False, "without-memory")
    uses_uv = "uv" in with_memory
    no_pip = "pip install" not in with_memory.replace("uv pip", "")
    contrast = "uv" not in without_memory  # the control did *not* know the convention
    print(SEP)
    print(f"  with-memory  : uses_uv={uses_uv} no_pip_install={no_pip}")
    print(f"  without-memory: mentions pip install={'pip install' in without_memory}")
    ok = uses_uv and no_pip
    print(f"  {'PASS' if ok else 'FAIL'}  real-memory-case（A 会话教的约定，B 会话用上了）")
    if contrast:
        print("  contrast: 关掉记忆的对照组没能复现该约定 —— 差异确实来自记忆")
    else:
        print("  note: 对照组也答对了，差异不够强——可换个更冷门的约定重跑")
    return 0 if ok else 2


async def main(only: str = "", config_path: str = "", real: bool = False) -> int:
    if real:
        return await real_case(config_path or str(ROOT / "config.local.yaml"))
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    selected = [(name, fn) for name, fn in CASES if not wanted or name in wanted]
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="memory_cases_") as tmp:
        for name, fn in selected:
            root = Path(tmp) / name
            root.mkdir(parents=True, exist_ok=True)
            print(SEP + f" [{name}] ")
            try:
                ok, detail = await fn(root)
            except Exception as exc:  # noqa: BLE001 — a case runner reports, not crashes
                ok, detail = False, f"raised {type(exc).__name__}: {exc}"
            print(f"  {'PASS' if ok else 'FAIL'}  {detail}")
            results.append((name, ok, detail))
    print(SEP)
    for name, ok, _detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(1 for _name, ok, _detail in results if ok)
    print(f"SUMMARY {passed}/{len(results)} passed")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated case names")
    parser.add_argument("--real", action="store_true", help="run the real-LLM A/B case")
    parser.add_argument("--config", default="", help="config file for --real")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.only, args.config, args.real)))
