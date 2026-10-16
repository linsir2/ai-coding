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
