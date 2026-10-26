"""M-E4 (plan-m-e §D): prefix-cache friendliness.

The request is assembled so that everything volatile (memory, task, hook,
digest, the environment block, ...) rides a tail *after* the history, which is
what keeps the prompt-cache prefix alive.  The guard here is the boolean
invariant over ``canonical_request(instructions, Assembled.history, tools_text)``:

    stable_b.startswith(stable_a)

— a byte-level, path-independent check.  A percentage threshold is
deliberately NOT used: M-A6 measured the whole-request ratio drifting with the
workspace path (87% short, 77% at 442 chars), so a fixed number is both wrong
and flaky.  The tail is set aside by passing ``history`` (pre-tail), never
``request``, because ``canonical_request`` does not know where the tail begins
and would always report a break.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from ai_coding.ai.prompt import BASE_INSTRUCTIONS, PromptAssembler
from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.trace import canonical_request
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState

#: The tail blocks ``request_assembly`` can emit (the ``TailBlock.name``
#: literals).  Enumerated for documentation and diagnosis — the assertion is
#: the whole stable prefix, which covers all of them at once (plan-m-e §D.1 (b)).
TAIL_BLOCK_NAMES = (
    "environment",
    "memory",
    "mcp_catalog",
    "task",
    "recommended_skills",
    "referenced_files",
    "error_recovery",
    "anchor",
    "hook",
    "digest",
    "budget",
)

#: A fixed tool surface: the comparison is over canonical requests, so the
#: schema text must be identical across the two turns (a change is a skip
#: condition, not a failure).
TOOLS_TEXT = '[{"name":"read","description":"read a file"}]'

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


class _Compactor:
    """Hands the history straight back (append-only) unless asked to rewrite."""

    def __init__(self, rewrite: bool = False) -> None:
        self.rewrite = rewrite

    async def compress(self, history, *, deadline, on_summary=None, force=False, pinned=()):
        if not self.rewrite:
            return history
        return [*history, ChatMessage(role="user", content="[compressed]", timestamp="")]


class _Memory:
    def __init__(self, block: str = "") -> None:
        self._block = block

    def block(self) -> tuple[str, list[str]]:
        return self._block, (["n1"] if self._block else [])


class _Reporter:
    def __init__(self, tools_text: str) -> None:
        self.tools_text = tools_text
        self.calls: list[dict[str, Any]] = []

    def report_context(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return canonical_request(kwargs["instructions"], kwargs["history"], self.tools_text)

    def tokens_spent(self) -> int:
        return 0


class _Settings:
    working_memory_chars = 1200

    def __init__(self, auto_compact: bool = True) -> None:
        self.auto_compact = auto_compact


def _session() -> SessionData:
    session = SessionData(session_id="s1", title="", created_time="t", last_access_time="t")
    session.messages.append(ChatMessage(role="user", content="开始", timestamp=""))
    return session


def _assembler(
    *,
    task_injection: str = "message",
    tools_text: str = TOOLS_TEXT,
    rewrite: bool = False,
    memory_block: str = "<memory-constraints>\n- 约束\n</memory-constraints>",
    catalog: str = "<mcp-catalog>x</mcp-catalog>",
    workspace: str = "/tmp/ws",
    instructions: str | None = None,
) -> tuple[RequestAssembler, _Reporter]:
    async def _build(deadline: Any, session: Any = None) -> str | None:
        if instructions is not None:
            return instructions
        return PromptAssembler(BASE_INSTRUCTIONS).build(tools=("read", "bash"))

    reporter = _Reporter(tools_text)
    assembler = RequestAssembler(
        compactor=_Compactor(rewrite),
        policy=TurnPolicy(TurnPolicyConfig(task_injection=task_injection)),
        memory=_Memory(memory_block),
        reporter=reporter,
        build_instructions=_build,
        settings=_Settings(),
        emit=lambda event: None,
        catalog_builder=lambda: catalog,
        workspace=workspace,
    )
    return assembler, reporter


async def _assemble(
    assembler: RequestAssembler, session: SessionData, *, turn_number: int, index: int
):
    return await assembler.assemble(
        session=session,
        task=TaskState(goal="长任务"),
        books=TurnBooks(),
        turn_number=turn_number,
        hook_context="<hook-context>h</hook-context>",
        deadline=None,
        index=index,
        previous_request=None,
    )

def _skip_reason(
    *,
    task_injection: str,
    compacted: bool,
    instructions_a: str | None,
    instructions_b: str | None,
    tools_a: str,
    tools_b: str,
    plan_mode: bool,
) -> str | None:
    """Why the two-turn prefix may legitimately break — or ``None``.

    Five conditions (plan-m-e §D.3), each a real, legal break:

    1. ``task_injection="system"`` — the task list is appended to the system
       prompt, so a ``todo_write`` moves the stable front;
    2. a compaction in the turn — the history is rewritten, so the prefix is
       *meant* to change;
    3. the instructions changed — a single string comparison covers every cause
       (mode, tools, catalog), not a narrow enumeration;
    4. the tool surface changed — ``canonical_request`` puts the schemas first,
       so any schema change moves the front;
    5. plan mode — the reminder is appended to the history end and re-added
       every request, so ``startswith`` would break at it.
    """
    if task_injection == "system":
        return "task_injection=system rewrites the system prompt"
    if compacted:
        return "the turn compacted its history"
    if instructions_a != instructions_b:
        return "the instructions changed between the two turns"
    if tools_a != tools_b:
        return "the tool surface changed between the two turns"
    if plan_mode:
        return "the plan reminder is re-added to the history every request"
    return None


# ------------------------------------------------------------------ W7 / W8


@pytest.mark.asyncio
async def test_every_injection_channel_leaves_the_stable_prefix_alone(tmp_path):
    """W7 (plan-m-e M-E4): every injection channel lands in the volatile tail.

    The stable prefix is the whole ``canonical_request(instructions, history,
    tools_text)`` — the tail set aside by passing ``history``, not ``request``.
    If any channel were injected *before* the history, the prefix would move;
    it does not, so the prompt cache keeps hitting.
    """
    session = _session()
    assembler, reporter = _assembler()
    first = await _assemble(assembler, session, turn_number=1, index=1)
    session.messages.append(ChatMessage(role="user", content="第二轮", timestamp=""))
    second = await _assemble(assembler, session, turn_number=2, index=2)

    names = {name for name, _size in reporter.calls[-1]["tail_blocks"]}
    assert names <= set(TAIL_BLOCK_NAMES)
    # The channels this scenario always populates — every one of them after the
    # history.  The remaining names (digest, budget, ...) are conditional and
    # simply absent here; the whole-prefix assertion below covers them too.
    assert {"environment", "memory", "mcp_catalog", "task", "hook"} <= names

    stable_a = canonical_request(first.instructions, first.history, TOOLS_TEXT)
    stable_b = canonical_request(second.instructions, second.history, TOOLS_TEXT)
    assert stable_b.startswith(stable_a)


async def _run_scenario(tmp_path, scenario: dict[str, Any]):
    session = _session()
    if scenario.get("plan_mode"):
        # ``mode`` is a projection of ``agent`` (M1b): assigning to it raises.
        session.agent = "plan"
    assembler, reporter = _assembler(
        task_injection=scenario.get("task_injection", "message"),
        tools_text=scenario.get("tools_text", TOOLS_TEXT),
        rewrite=scenario.get("compact", False),
        instructions=scenario.get("instructions"),
    )
    first = await _assemble(assembler, session, turn_number=1, index=1)
    session.messages.append(ChatMessage(role="user", content="第二轮", timestamp=""))
    if scenario.get("tools_change"):
        reporter.tools_text = scenario.get("tools_text", TOOLS_TEXT) + ' {"changed":true}'
    second = await _assemble(assembler, session, turn_number=2, index=2)

    tools_a = scenario.get("tools_text", TOOLS_TEXT)
    tools_b = reporter.tools_text
    skip = _skip_reason(
        task_injection=scenario.get("task_injection", "message"),
        compacted=scenario.get("compact", False),
        instructions_a=first.instructions,
        instructions_b=second.instructions,
        tools_a=tools_a,
        tools_b=tools_b,
        plan_mode=scenario.get("plan_mode", False),
    )
    return first, second, tools_a, tools_b, skip


@pytest.mark.asyncio
async def test_a_turns_prefix_is_byte_stable_when_nothing_volatile_changed(tmp_path):
    """W8 (plan-m-e M-E4): two consecutive requests share a stable prefix.

    Same whole-prefix boolean invariant as W7, but across turns and with the
    five legitimate-break conditions as skips.  The guard asserts at least one
    scenario actually reached the assertion — otherwise the skips could hide a
    vacuous pass.
    """
    scenarios: list[dict[str, Any]] = [
        {"name": "append_only"},
        {"name": "system_injection", "task_injection": "system"},
        {"name": "mid_turn_compaction", "compact": True},
        {"name": "tools_change", "tools_change": True},
        {"name": "plan_mode", "plan_mode": True},
    ]
    checked = 0
    for scenario in scenarios:
        first, second, tools_a, tools_b, skip = await _run_scenario(tmp_path, scenario)
        if skip is not None:
            continue
        stable_a = canonical_request(first.instructions, first.history, tools_a)
        stable_b = canonical_request(second.instructions, second.history, tools_b)
        assert stable_b.startswith(stable_a), f"prefix broke in {scenario['name']!r}"
        checked += 1
    assert checked >= 1, "every scenario was skipped — the invariant was never checked"


def test_the_five_skip_conditions_are_detected():
    """Each legitimate break is recognised, so a real regression cannot hide.

    The prefix is stable in the ordinary case and breaks for exactly the five
    reasons documented in ``_skip_reason``.
    """
    base = {
        "task_injection": "message",
        "compacted": False,
        "instructions_a": "SYS",
        "instructions_b": "SYS",
        "tools_a": TOOLS_TEXT,
        "tools_b": TOOLS_TEXT,
        "plan_mode": False,
    }
    assert _skip_reason(**base) is None
    assert "system" in (_skip_reason(**{**base, "task_injection": "system"}) or "")
    assert "compact" in (_skip_reason(**{**base, "compacted": True}) or "")
    assert "instructions" in (_skip_reason(**{**base, "instructions_b": "OTHER"}) or "")
    assert "tool surface" in (_skip_reason(**{**base, "tools_b": "OTHER"}) or "")
    assert "plan reminder" in (_skip_reason(**{**base, "plan_mode": True}) or "")


# ----------------------------------------------------------------------- W9


def test_the_system_prompt_carries_no_sub_second_timestamp():
    """W9 (plan-m-e M-E4): no second-level timestamp in the prompt's stable front.

    A timestamp at the top of the system prompt breaks the prefix on every turn
    (measured: caching then costs 25% *more*).  The scan is scoped to the system
    prompt and the tool definitions — the environment tail block legitimately
    carries ``now_local()``, and that is the correct design, so it must not be
    flagged.
    """
    instructions = PromptAssembler(BASE_INSTRUCTIONS).build(
        project_instructions="项目约定",
        skills_catalog="<skills>x</skills>",
        tools=("read", "bash"),
    )
    assert not _TIMESTAMP_RE.search(instructions)
    assert not _TIMESTAMP_RE.search(TOOLS_TEXT)
    # The tail block does carry the moment, and that is fine: it sits after the
    # history, so it never touches the cached prefix.
    assert _TIMESTAMP_RE.search("时间: 2026-10-26T12:00:00+08:00")
