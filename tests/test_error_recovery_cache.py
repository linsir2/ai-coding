"""TDD (F1): Error recovery via skill pattern_dna (no separate cache)."""

from __future__ import annotations

from ai_coding.domain.session import SessionData
from ai_coding.skills.registry import SkillRegistry


class TestCheckRecovery:
    """SOPRouter.check_recovery() uses skill pattern_dna as fallback."""

    def test_returns_none_when_no_failures(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = router.check_recovery([], session)
        assert result is None

    def test_falls_back_to_skill_pattern_dna(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        skill_dir = tmp_path / "sandbox-fix"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: sandbox-fix\ndescription: Fix sandbox\n"
            "pattern_dna: Use bash heredoc for external paths\n"
            "triggers:\n  failure_patterns: ['resolves outside']\n"
            "---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = router.check_recovery(["Error: path resolves outside workspace"], session)
        assert result is not None
        assert "bash heredoc" in result

    def test_returns_none_when_no_skill_matches(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = router.check_recovery(["completely unknown error"], session)
        assert result is None
