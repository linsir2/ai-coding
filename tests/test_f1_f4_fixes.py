"""TDD: Fix F1-F4 based on audit findings."""

from __future__ import annotations

from types import SimpleNamespace

from ai_coding.domain.session import SessionData
from ai_coding.domain.subagent import SubagentResult
from ai_coding.skills.registry import SkillRegistry

# ===========================================================================
# F1 Fix: check_recovery uses skill pattern_dna as fallback (no dead record_recovery)
# ===========================================================================


class TestF1CheckRecovery:
    """check_recovery should work without record_recovery (removed)."""

    def test_returns_none_when_no_failures(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = router.check_recovery([], session)
        assert result is None

    def test_falls_back_to_skill_pattern_dna(self, tmp_path):
        """When no cache entry, check_recovery should find skill pattern_dna."""
        from ai_coding.core.sop_router import SOPRouter
        skill_dir = tmp_path / "sandbox-fix"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: sandbox-fix\n"
            "description: Fix sandbox\n"
            'pattern_dna: "Use bash heredoc for external paths"\n'
            "triggers:\n"
            "  failure_patterns: ['resolves outside']\n"
            "---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = router.check_recovery(["Error: path resolves outside workspace"], session)
        assert result is not None
        assert "bash heredoc" in result


# ===========================================================================
# F2 Fix: phase threshold lowered, no conflict with F3
# ===========================================================================


class TestF2PhaseFix:
    """Phase computation with lower threshold."""

    def test_debugging_on_first_failure_streak(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        books = SimpleNamespace(failed_streak=1)
        task = SimpleNamespace(goal="test", items=[], spec=property(lambda self: []))
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = RequestAssembler._compute_phase(session, books, task)
        assert result == "debugging"

    def test_phase_filter_allows_when_matching(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        skill_dir = tmp_path / "debug-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: debug-skill\ndescription: Debug\nphase: debugging\n"
            "triggers:\n  keywords: ['错误']\n---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("代码有错误需要修复", [], "", [], phase="debugging")
        assert len(matches) == 1
        assert matches[0].name == "debug-skill"


# ===========================================================================
# F3 Fix: accurate consecutive failure detection
# ===========================================================================


class TestF3InterventionFix:
    """Intervene should only trigger on truly consecutive same-pattern failures."""

    def test_no_intervention_on_different_errors(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        skill_dir = tmp_path / "fix-skill"
        skill_dir.mkdir()
        (fix_skill := skill_dir / "SKILL.md").write_text(
            "---\nname: fix-skill\ndescription: Fix\n"
            "pattern_dna: 'Use bash'\n"
            "triggers:\n  failure_patterns: ['resolves outside']\n"
            "---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        # Different errors, not consecutive same pattern
        failures = ["permission denied", "file not found"]
        result = router.intervene(failures, [], None, {})
        assert result.mode == "none"

    def test_soft_on_single_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        skill_dir = tmp_path / "fix-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: fix-skill\ndescription: Fix\n"
            "pattern_dna: 'Use bash heredoc'\n"
            "triggers:\n  failure_patterns: ['resolves outside']\n"
            "---\n\n# Skill\n## 使用场景\n...\n## 步骤\n1. ...\n## 验证\n...\n"
        )
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        result = router.intervene(["resolves outside workspace"], [], None, {})
        assert result.mode == "soft"
        assert "bash heredoc" in result.content

    def test_hard_on_two_consecutive_same(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        skill_dir = tmp_path / "fix-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: fix-skill\ndescription: Fix\n"
            "pattern_dna: 'Use bash'\n"
            "triggers:\n  failure_patterns: ['resolves outside']\n"
            "---\n\n# Skill\n## 使用场景\n...\n## 步骤\n1. step1\n2. step2\n## 验证\n...\n"
        )
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        # Two consecutive same-pattern failures
        failures = ["path resolves outside workspace", "file resolves outside workspace"]
        result = router.intervene(failures, [], None, {})
        assert result.mode == "hard"
        assert "步骤" in result.content


# ===========================================================================
# F4 Fix: trace field bounded, sync path writes, no dead from_dict
# ===========================================================================


class TestF4TraceFieldFix:
    """Trace field should be bounded and written from both paths."""

    def test_trace_file_bounded(self, tmp_path):
        from ai_coding.core.subagent import MAX_TRACE_ENTRIES, _write_trace_note
        # Write more than the limit
        for i in range(MAX_TRACE_ENTRIES + 10):
            result = SubagentResult(
                status="completed",
                notes=f"Note {i}",
                ledger_path=str(tmp_path / f"test-{i}.json"),
            )
            _write_trace_note(result, tmp_path)
        trace_file = tmp_path / "transcripts" / "skill-traces" / "trace-field.md"
        if trace_file.exists():
            content = trace_file.read_text()
            # Should not contain the oldest notes
            assert "Note 0" not in content
            assert f"Note {MAX_TRACE_ENTRIES + 5}" in content

    def test_subagent_result_no_from_dict(self):
        """SubagentResult should NOT have from_dict (removed as dead code)."""
        assert not hasattr(SubagentResult, 'from_dict') or SubagentResult.from_dict is None

    def test_extract_notes_handles_markdown(self, tmp_path):
        """Notes extraction should handle markdown-formatted output."""
        from ai_coding.core.subagent import _extract_notes_and_confidence
        conclusion = "Some output\n\n**置信度: low**\n原因: not sure\n给后来者的笔记: Use bash for external paths"
        notes, confidence = _extract_notes_and_confidence(conclusion)
        assert confidence == "low"
        assert "bash" in notes
