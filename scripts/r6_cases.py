#!/usr/bin/env python3
"""R6 acceptance cases — offline by default, ``--real`` adds the live ones.

The offline cases replay docs/r6-analysis.md section 1 with fakes: the digest ring,
the compaction-triggered injection, the handoff, the escalation ladder, ask_user
with and without a human, and the evidence/index invariants.  They are the "did it
actually help?" checks for this round, in the shape of scripts/memory_cases.py.

The live cases re-run the same experiments against the configured model.  Their
verdicts are structural (an empty answer, a bash call that reads the session file, a
prompt-token ceiling) rather than stylistic, because LLM prose is not a contract.

    .venv/bin/python scripts/r6_cases.py
    .venv/bin/python scripts/r6_cases.py --only ring_present_after_compaction
    .venv/bin/python scripts/r6_cases.py --real --config config.local.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.config.models import AIConfig, MemoryConfig  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.domain.loop import StepResult, TurnOutcome, TurnStatus  # noqa: E402
from ai_coding.domain.message import (  # noqa: E402
    ChatMessage,
    ToolArtifact,
    ToolCallRef,
    ToolResult,
)
from ai_coding.domain.session import SessionData  # noqa: E402
from ai_coding.domain.task import TaskState  # noqa: E402
from ai_coding.domain.turn_digest import (  # noqa: E402
    TurnDigest,
    append_digest,
    build_digest,
    render_working_memory,
)
from ai_coding.memory.note import NoteKind, new_note  # noqa: E402
from ai_coding.memory.schema import HarvestCandidate  # noqa: E402
from ai_coding.memory.service import MemoryService  # noqa: E402
from ai_coding.memory.store import MemoryStore  # noqa: E402
from ai_coding.security.approval import AutoApproveCallback  # noqa: E402
from ai_coding.security.user_prompt import (  # noqa: E402
    UnavailableUserPromptCallback,
    UserAnswer,
)
from ai_coding.tools.ask_user_tool import AskUserTool  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402

SEP = "-" * 72
CaseFn = Callable[[Path], Coroutine[Any, Any, tuple[bool, str]]]


# ---------------------------------------------------------------- fakes ----


class _Engine(ChatEngine):
    """Scripted engine: answer with text, or keep asking for one tool."""

    def __init__(self, tool_name: str | None = None, reply: str = "做完了") -> None:
        self.tool_name = tool_name
        self.reply = reply
        self.calls = 0
        self.requests: list[list[ChatMessage]] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        self.requests.append(list(messages))
        self.calls += 1
        if self.tool_name is None:
            return StepResult(text=self.reply, tool_calls=[], finish_reason="stop")
        return StepResult(
            text=None,
            tool_calls=[
                ToolCallRef(id=f"c{self.calls}", name=self.tool_name, arguments="{}")
            ],
            finish_reason="tool_calls",
        )


class _Tool(BaseTool):
    parallel_safe = True

    def __init__(self, name: str, success: bool = True) -> None:
        self._name = name
        self.success = success

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
        if self.success:
            return ToolResult(success=True, output="ok")
        return ToolResult(success=False, output="boom", error="nonzero_exit")


class _Sessions:
    """Session sink that keeps what it was handed (no disk)."""

    def __init__(self) -> None:
        self.saved: list[SessionData] = []

    def save(self, session: SessionData) -> None:
        self.saved.append(session)

    def mark_pending_approval(self, session_id: str, request: object) -> None:
        """P3: the real SessionService writes this marker beside the session
        file; these cases never touch disk."""
        return None


class _Context:
    """Stand-in compaction: rewrite the history, or hand it back untouched."""

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
    return AgentLoop(
        engine,
        _Sessions(),
        registry,
        approval=AutoApproveCallback(),
        settings=AIConfig(**overrides),
        context=context,
        workspace=".",
    )


def _session() -> SessionData:
    return SessionData(session_id="case", title="", created_time="t", last_access_time="t")


def _last_request(engine: _Engine) -> str:
    return "\n".join(message.content or "" for message in engine.requests[-1])


# ------------------------------------------------------ offline cases ------


async def case_digest_ring_is_bounded(root: Path) -> tuple[bool, str]:
    session = _session()
    for turn in range(1, 31):
        append_digest(
            session, TurnDigest(turn=turn, request=f"r{turn}", outcome="ok"), keep=12
        )
    ok = len(session.turn_digests) == 12 and session.turn_digests[0].turn == 19
    return ok, f"ring={len(session.turn_digests)} oldest=t{session.turn_digests[0].turn}"


async def case_ring_absent_without_compaction(root: Path) -> tuple[bool, str]:
    engine = _Engine()
    loop = _loop(engine, _Context(rewrite=False))
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    ok = "<working-memory>" not in _last_request(engine)
    return ok, "an ordinary turn pays nothing for the ring"


async def case_ring_present_after_compaction(root: Path) -> tuple[bool, str]:
    engine = _Engine()
    loop = _loop(engine, _Context(rewrite=True))
    session = _session()
    await loop.process_input(session, "一")
    await loop.process_input(session, "二")
    text = _last_request(engine)
    block = text.split("<working-memory>")[-1].split("</working-memory>")[0]
    ok = "<working-memory>" in text and len(block) <= 1200 and "第 2 轮" in text
    return ok, f"digest block injected, {len(block)} chars, names the current turn"


async def case_exhausted_turn_reports(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool_name="poke")
    loop = _loop(engine, max_tool_iterations=2)
    session = _session()
    answer = await loop.process_input(session, "让 make test 通过")
    outcome = loop.last_outcome
    ok = (
        bool(answer)
        and "[回合未完成" in answer
        and outcome is not None
        and outcome.text is None
        and outcome.handoff == answer
    )
    text = None if outcome is None else outcome.text
    return ok, f"{len(answer)} chars of handoff, outcome.text={text!r}"


async def case_one_strategy_nudge(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool_name="flaky")
    loop = _loop(
        engine,
        max_tool_iterations=6,
        stall_iterations=0,
        failed_streak_threshold=2,
    )
    session = _session()
    await loop.process_input(session, "修好它")
    gates = [
        message
        for message in session.messages
        if message.role == "user" and "[strategy-gate]" in (message.content or "")
    ]
    return len(gates) == 1, f"{len(gates)} strategy gate(s), the model gets one"


async def case_stalled_turn_stops_early(root: Path) -> tuple[bool, str]:
    engine = _Engine(tool_name="flaky")
    loop = _loop(engine, max_tool_iterations=8, stall_iterations=3)
    session = _session()
    answer = await loop.process_input(session, "修好它")
    outcome = loop.last_outcome
    iterations = None if outcome is None else outcome.iterations
    reason = None if outcome is None else outcome.reason
    ok = (
        outcome is not None
        and "no progress" in reason
        and iterations is not None
        and iterations < 8
        and "[回合未完成" in answer
        and "最近一次失败: flaky → nonzero_exit" in answer
    )
    return ok, f"{iterations} iterations (budget 8), reason={reason!r}"


async def case_ask_user_without_a_human(root: Path) -> tuple[bool, str]:
    result = await AskUserTool(UnavailableUserPromptCallback()).execute(
        {"question": "改哪个？"}
    )
    ok = result.success is False and result.error == "no_user" and "假设" in result.output
    return ok, f"error={result.error}, the model is told to decide and say so"


async def case_ask_user_with_a_human(root: Path) -> tuple[bool, str]:
    class _Human:
        async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
            return UserAnswer(answered=True, text="改 A")

    result = await AskUserTool(_Human()).execute({"question": "改哪个？"})
    ok = result.success is True and result.data["answer"] == "改 A"
    return ok, f"answer={result.data.get('answer')!r}"


async def case_evidence_and_index(root: Path) -> tuple[bool, str]:
    store = MemoryStore(root / ".memory")
    service = MemoryService(
        store, extractor=_Extractor("项目用 uv 管理依赖"), config=MemoryConfig()
    )
    await service.harvest(
        [ChatMessage(role="user", content="改用 uv", timestamp="t")],
        turn_index=3,
        evidence="tool",
    )
    store.add_note(
        new_note("测试框架是 pytest", kind=NoteKind.DECISION, evidence="model")
    )
    notes = store.active()
    status = service.status()
    block = service.index_block()
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    ok = (
        sorted(note.evidence for note in notes) == ["model", "tool"]
        and status["by_evidence"] == {"tool": 1, "model": 1}
        and len(block) <= 600
        and all(line.startswith("- [") for line in lines)
    )
    return ok, f"{len(notes)} notes, index={len(block)} chars, evidence={status['by_evidence']}"


OFFLINE: list[tuple[str, CaseFn]] = [
    ("digest_ring_is_bounded", case_digest_ring_is_bounded),
    ("ring_absent_without_compaction", case_ring_absent_without_compaction),
    ("ring_present_after_compaction", case_ring_present_after_compaction),
    ("exhausted_turn_reports", case_exhausted_turn_reports),
    ("one_strategy_nudge", case_one_strategy_nudge),
    ("stalled_turn_stops_early", case_stalled_turn_stops_early),
    ("ask_user_without_a_human", case_ask_user_without_a_human),
    ("ask_user_with_a_human", case_ask_user_with_a_human),
    ("evidence_and_index", case_evidence_and_index),
]


# --------------------------------------------------------- live cases ------

_CONFIG = "config.local.yaml"


def _real_loop(config_path: str, workspace: Path, **overrides: Any) -> tuple[Any, Any]:
    """The composition the CLI uses: real engine, real tools, real memory, trace."""
    from ai_coding.ai.factory import build_engine_from_config
    from ai_coding.cli import _build_loop, _build_tool_registry, _make_recorder
    from ai_coding.config.loader import ConfigManager
    from ai_coding.security.user_prompt import CLIUserPromptCallback
    from ai_coding.service.session_service import SessionService

    manager = ConfigManager()
    manager.initialize(config_path)
    app_cfg = manager.app
    for key, value in overrides.items():
        setattr(app_cfg.ai, key, value)
    engine = build_engine_from_config(app_cfg)
    recorder = _make_recorder(app_cfg, str(workspace), None)
    registry = _build_tool_registry(
        app_cfg, str(workspace), None, user_prompt=CLIUserPromptCallback()
    )
    sessions = SessionService(workspace / "sessions")
    loop = _build_loop(
        engine,
        sessions,
        registry,
        app_cfg,
        str(workspace),
        None,
        observer=recorder,
        trace=recorder,
    )
    return loop, recorder


def _trace_events(workspace: Path, session_id: str, turn: int = 1) -> list[dict[str, Any]]:
    path = workspace / ".ai-coding" / "traces" / session_id / f"turn-{turn}.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def _synth_history(
    turns: int = 70, body: int = 3000
) -> tuple[list[ChatMessage], list[TurnDigest]]:
    """A long tool-heavy session *and* the digests the loop would have kept for it.

    The digests are built with the real ``build_digest``, so this replays "R6a had
    been running all along" rather than hand-writing the answer the model is meant
    to find.  Turn 66 carries the marker the live case asks about: after compaction
    the surviving history is the head plus the last 20 messages (turns 69+), so turn
    66 is *ring-only* — exactly the distance R6a claims to cover.

    Note what this fixture deliberately does **not** claim: a 12-line ring cannot
    reach turn 3.  A decision that old belongs to memory (a pinned note in
    <memory-constraints>), which is R3's job — measured, see the R2 result in
    docs/r6-analysis.md 1.2 and section 10.
    """

    def message(role: str, content: str | None, **kw: Any) -> ChatMessage:
        return ChatMessage(role=role, content=content, timestamp="t", **kw)

    messages: list[ChatMessage] = []
    digests: list[TurnDigest] = []

    def turn(number: int, request: str, *, result: str, decision: str) -> None:
        messages.append(message("user", request))
        messages.append(
            message(
                "assistant",
                None,
                tool_calls=[
                    ToolCallRef(id=f"c{number}", name="bash", arguments='{"command":"ls"}')
                ],
            )
        )
        messages.append(
            message("tool", result, tool_call_id=f"c{number}", tool_name="bash")
        )
        messages.append(message("assistant", decision))
        digests.append(
            build_digest(
                turn=number,
                user_input=request,
                outcome=TurnOutcome(
                    status=TurnStatus.COMPLETED,
                    text=decision,
                    iterations=2,
                    reason="model finished",
                ),
                artifacts=[
                    ToolArtifact(
                        path="pyproject.toml", kind="write", bytes=1, description=""
                    )
                ],
            )
        )

    turn(
        1,
        "帮我把这个仓库的构建迁移到新工具链，先看看现在用什么",
        result="build-backend = poetry.core.masonry.api " + "y" * 1500,
        decision="目前用 poetry 管理依赖。",
    )
    turn(
        2,
        "先把锁文件生成出来看看",
        result="poetry 在这个 Python 3.13 环境上解析超时失败 " + "z" * 1500,
        decision="poetry lock 超时失败了。",
    )
    turn(
        3,
        "换个方案试试",
        result="uv lock: resolved 42 packages in 0.8s -> uv.lock",
        decision="决定放弃 poetry，改用 uv：poetry 在 Python 3.13 上解析超时。",
    )
    for number in range(4, 4 + turns):
        if number == 66:
            turn(
                number,
                "第 66 步：把 CI 的缓存从 pip 换成 uv，别再每次重装",
                result=f"RESULT-{number} " + "x" * body,
                decision="改了 .github/workflows/ci.yml：缓存改走 uv，命中率 92%。",
            )
            continue
        turn(
            number,
            f"继续推进第 {number} 步",
            result=f"RESULT-{number} " + "x" * body,
            decision=f"第 {number} 步完成。",
        )
    return messages, digests


async def real_r1_exhausted_turn_reports(root: Path) -> tuple[bool, str]:
    """docs/r6-analysis.md 1.3: the turn that used to print nothing."""
    workspace = root / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b  # bug\n", encoding="utf-8"
    )
    loop, _recorder = _real_loop(_CONFIG, workspace, max_tool_iterations=2)
    session = SessionData(session_id="r1", title="", created_time="t", last_access_time="t")
    answer = await loop.process_input(
        session, "让这个仓库的验收命令 `make test` 通过（必须真实执行看到成功输出）"
    )
    await loop.aclose()
    outcome = loop.last_outcome
    ok = bool(answer.strip()) and "[回合未完成" in answer and outcome is not None
    return ok, f"{len(answer)} chars, head={answer.strip()[:80]!r}"


async def real_r2_compacted_recall(root: Path) -> tuple[bool, str]:
    """docs/r6-analysis.md 1.2/2.1: a ring-only turn is answered without digging.

    The question targets turn 66: it fell out of the surviving history when
    compaction kept the head and the last 20 messages, but it is still inside the
    12-line ring.  Reaching a *deep* past decision is memory's job, not the ring's.
    """
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    messages, digests = _synth_history()
    session = SessionData(
        session_id="r2",
        title="",
        created_time="t",
        last_access_time="t",
        messages=messages,
        task=TaskState(goal="把仓库构建迁移到新工具链"),
        turn_count=len(digests),
        turn_digests=digests,
    )
    session.harvested_messages = len(messages)
    loop, _recorder = _real_loop(_CONFIG, workspace)
    ring = render_working_memory(
        session.turn_digests, current_turn=session.turn_count, max_chars=1200, compacted=True
    )
    answer = await loop.process_input(
        session, "回顾一下：CI 的缓存这块我们做了什么改动？缓存命中率是多少？"
    )
    events = _trace_events(workspace, session.session_id)
    await loop.aclose()
    dug = any(
        "sessions/" in json.dumps(event, ensure_ascii=False)
        and "bash" in json.dumps(event, ensure_ascii=False)
        for event in events
    )
    first_prompt = next(
        (
            int((event.get("data", {}).get("usage") or {}).get("prompt_tokens", 0))
            for event in events
            if event.get("evt") == "ModelStepFinished"
        ),
        0,
    )
    ok = "92" in answer and not dug and 0 < first_prompt < 5000
    return (
        ok,
        f"ring_carries_t66={'92' in ring} answered={'92' in answer} "
        f"session_file_read={dug} first_prompt={first_prompt} "
        f"answer={answer.strip()[:140]!r}",
    )


async def real_r3_ask_user_reaches_a_human(root: Path) -> tuple[bool, str]:
    """docs/r6-analysis.md 2.3: the interactive composition wires a human in.

    Two halves, because there is no TTY here: the *composition* must register the
    tool with a real CLI channel (that is the REPL's wiring), and the tool itself
    must turn a scripted answer into structured data.
    """
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    loop, _recorder = _real_loop(_CONFIG, workspace, max_tool_iterations=2)
    tool = loop.tools.get("ask_user")
    await loop.aclose()
    if tool is None:
        return False, "ask_user was not registered for an interactive loop"

    from ai_coding.security.user_prompt import CLIUserPromptCallback

    wired = isinstance(getattr(tool, "_prompt", None), CLIUserPromptCallback)
    bound = tool.max_duration_seconds is not None and tool.max_duration_seconds > 0

    class _Human:
        async def ask(self, question: str, options: Sequence[str] = ()) -> UserAnswer:
            return UserAnswer(answered=True, text="用 uv")

    result = await AskUserTool(_Human()).execute(
        {"question": "依赖工具用哪个？", "options": ["poetry", "uv"]}
    )
    ok = (
        wired
        and bound
        and result.success is True
        and result.data.get("answer") == "用 uv"
    )
    return (
        ok,
        f"CLI channel wired={wired} wait bounded={bound} "
        f"scripted answer={result.data.get('answer')!r}",
    )


REAL: list[tuple[str, CaseFn]] = [
    ("R1_exhausted_turn_reports", real_r1_exhausted_turn_reports),
    ("R2_compacted_recall", real_r2_compacted_recall),
    ("R3_ask_user", real_r3_ask_user_reaches_a_human),
]


async def main(argv: list[str] | None = None) -> int:
    global _CONFIG
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated case names")
    parser.add_argument("--real", action="store_true", help="also run the live cases")
    parser.add_argument("--config", default="config.local.yaml", help="config for --real")
    args = parser.parse_args(argv)
    _CONFIG = args.config

    wanted = {name for name in args.only.split(",") if name}
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
            if wanted and name not in wanted:
                continue
            with tempfile.TemporaryDirectory(prefix="r6-real-") as raw:
                ok, detail = await case(Path(raw))
            # Reported, not graded: the verdict is structural where it can be, but a
            # live model chooses its own first move (measured: the ring carries the
            # marker every time; whether the model digs into the session file varies).
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    print(SEP)
    if failures:
        print(f"{failures} offline case(s) failed")
    else:
        note = " (live cases above are reported, not graded)" if args.real else ""
        print(f"all offline cases passed{note}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
