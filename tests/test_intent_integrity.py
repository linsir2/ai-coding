"""TDD: Intent integrity closure — constraint injection, cross-turn detection,
anchor verification.
"""

from __future__ import annotations

from ai_coding.domain.session import SessionData

# ===========================================================================
# I2: Constraint injection fix — fact notes with trigger keywords should inject
# ===========================================================================


class TestI2ConstraintInjection:
    """Memory constraints should be injectable regardless of kind."""

    def test_fact_note_with_constraint_keyword_injects(self, tmp_path):
        """A fact note that looks like a constraint should be injectable."""
        from ai_coding.config.models import MemoryConfig
        from ai_coding.memory.note import NoteKind, new_note
        from ai_coding.memory.service import MemoryService
        from ai_coding.memory.store import MemoryStore

        store = MemoryStore(tmp_path / "m.json")
        config = MemoryConfig()
        svc = MemoryService(store, config=config)

        # Create a fact note that is actually a constraint
        note = new_note(
            text="write/edit 工具只能写工作区内的文件，外部路径必须用 bash",
            kind=NoteKind.FACT,
            importance=5,
        )
        store._notes.append(note)
        store._save()

        # index_notes should include this constraint-like fact note
        notes = svc.index_notes()
        assert len(notes) == 1
        assert "write/edit" in notes[0].text


# ===========================================================================
# I2: Cross-turn duplicate failure detection
# ===========================================================================


class TestI2CrossTurnDetection:
    """System should detect cross-turn duplicate failures."""

    def test_detects_cross_turn_duplicate(self, tmp_path):
        """Same error across turns should be detected."""
        from ai_coding.core.sop_router import SOPRouter, ToolObservation
        from ai_coding.skills.registry import SkillRegistry

        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)

        # Simulate cross-turn observations
        observations = [
            ToolObservation(tool_name="write", success=False,
                            error="resolves outside workspace", timestamp="t1"),
            ToolObservation(tool_name="write", success=False,
                            error="resolves outside workspace", timestamp="t2"),
        ]
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        session.tool_observations = observations

        # Check if duplicate is detected
        is_duplicate = router.is_cross_turn_duplicate("resolves outside workspace", session)
        assert is_duplicate is True

    def test_no_false_positive_on_different_errors(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter, ToolObservation
        from ai_coding.skills.registry import SkillRegistry

        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        observations = [
            ToolObservation(tool_name="write", success=False,
                            error="permission denied", timestamp="t1"),
        ]
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        session.tool_observations = observations

        is_duplicate = router.is_cross_turn_duplicate("resolves outside workspace", session)
        assert is_duplicate is False


# ===========================================================================
# I3: Post-compaction anchor verification
# ===========================================================================


class TestI3PostCompactionAnchor:
    """After compaction, task anchor should be verifiable."""

    def test_anchor_injected_when_compacted(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        session.pre_compaction_anchor = "目标: 创建酒店系统 | 关键约束: 外部路径用 bash"
        block = RequestAssembler._format_anchor_block(session, compacted=True)
        assert "酒店系统" in block
        assert "bash" in block

    def test_no_anchor_when_not_compacted(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        session.pre_compaction_anchor = "目标: 创建酒店系统"
        block = RequestAssembler._format_anchor_block(session, compacted=False)
        assert block == ""


def test_the_anchor_tells_the_model_the_summary_may_not_be_recomputable():
    """R22/H11: after a compaction the checkpoint is the only record of the work.

    S3's model answered "8 lines (truth 162)" with "according to the summary,
    all 12 chunks are done".  The summary is generated from 1,000-character
    head/tail slices, so a count in it cannot be re-derived -- the anchor has to
    say that, at the one moment the model would otherwise trust it.
    """
    from ai_coding.core.request_assembly import RequestAssembler

    class _Session:
        pre_compaction_anchor = "统计 data/big.txt 里的 ERROR 行数"

    block = RequestAssembler._format_anchor_block(_Session(), True)
    assert "重新核算" in block
    assert "摘要" in block
    # The goal itself still rides along -- the caveat is additive.
    assert "统计 data/big.txt 里的 ERROR 行数" in block


def test_the_anchor_appears_after_compaction_even_without_a_task_goal():
    """A session with no task/goal still gets the caveat: the summary is lossy
    for every session, not only for plan/goal runs."""
    from ai_coding.core.request_assembly import RequestAssembler

    class _Session:
        pre_compaction_anchor = ""

    block = RequestAssembler._format_anchor_block(_Session(), True)
    assert "重新核算" in block


def test_the_anchor_stays_empty_before_any_compaction():
    from ai_coding.core.request_assembly import RequestAssembler

    class _Session:
        pre_compaction_anchor = "目标"

    assert RequestAssembler._format_anchor_block(_Session(), False) == ""


def test_the_caveat_survives_the_persistent_checkpoint(tmp_path):
    import asyncio
    """`/compact` replaces `session.messages`, so `did_compact` is False on every
    later turn — the caveat must key on "a summary is in force", not on "a
    rewrite happened just now"."""
    from ai_coding.ai.llm_helpers import SUMMARY_SECTIONS
    from ai_coding.core.request_assembly import RequestAssembler
    from ai_coding.core.turn_books import TurnBooks
    from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
    from ai_coding.domain.message import CHECKPOINT_MARK, ChatMessage
    from ai_coding.domain.session import SessionData
    from ai_coding.domain.task import TaskState

    class _NoRewrite:
        async def compress(self, history, **kwargs):  # noqa: ANN003
            return history

    class _Reporter:
        def report_context(self, **kwargs):  # noqa: ANN003
            return "canonical"

        def tokens_spent(self) -> int:
            return 0

    class _Memory:
        def block(self):
            return "", []

    class _Settings:
        working_memory_chars = 1200

    async def _instructions(deadline, session=None):  # noqa: ANN001
        return None

    assembler = RequestAssembler(
        compactor=_NoRewrite(),
        policy=TurnPolicy(TurnPolicyConfig()),
        memory=_Memory(),
        reporter=_Reporter(),
        build_instructions=_instructions,
        settings=_Settings(),
        emit=lambda event: None,
        workspace=str(tmp_path),
    )
    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    session.messages.append(
        ChatMessage(
            role="user",
            content=f"{CHECKPOINT_MARK}\n"
            + "\n".join(f"{section}\n无" for section in SUMMARY_SECTIONS),
            timestamp="t",
        )
    )

    assembled = asyncio.run(
        assembler.assemble(
            session=session,
            task=TaskState(goal=""),
            books=TurnBooks(),
            turn_number=2,
            hook_context="",
            deadline=None,
            index=0,
            previous_request=None,
        )
    )

    assert any("重新核算" in (m.content or "") for m in assembled.request)
