"""TDD (F2): Task phase locking — skills declare their phase, router filters by current phase."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# SkillRegistry frontmatter: phase field
# ---------------------------------------------------------------------------


class TestPhaseFrontmatter:
    """SkillRegistry.meta() should parse phase field."""

    def test_parses_phase(self, tmp_path):
        skill_dir = tmp_path / "debug-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: debug-skill\n"
            "description: Debug issues\n"
            "phase: debugging\n"
            "---\n\n# Debug Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        meta = reg.meta("debug-skill")
        assert meta["phase"] == "debugging"

    def test_no_phase_field(self, tmp_path):
        skill_dir = tmp_path / "no-phase"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: no-phase\ndescription: test\n---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        meta = reg.meta("no-phase")
        assert meta["phase"] == ""

    def test_catalog_includes_phase_hint(self, tmp_path):
        skill_dir = tmp_path / "debug-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: debug-skill\n"
            "description: Debug issues\n"
            "phase: debugging\n"
            "---\n\n# Debug Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        text = reg.catalog_text()
        assert "debug-skill" in text
        assert "[phase: debugging]" in text


# ---------------------------------------------------------------------------
# Phase computation
# ---------------------------------------------------------------------------


class TestPhaseComputation:
    """RequestAssembler._compute_phase() should determine current phase."""

    def test_debugging_phase_on_failure(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        books = SimpleNamespace(failed_streak=3)
        task = TaskState(goal="test")
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = RequestAssembler._compute_phase(session, books, task)
        assert result == "debugging"

    def test_executing_phase_with_items(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        books = SimpleNamespace(failed_streak=0)
        task = TaskState(goal="test")
        task.items = [SimpleNamespace(status="in_progress", verify="")]
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = RequestAssembler._compute_phase(session, books, task)
        assert result == "executing"

    def test_reviewing_phase_with_verify(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        books = SimpleNamespace(failed_streak=0)
        task = TaskState(goal="test")
        task.items = [SimpleNamespace(status="in_progress", verify="pytest")]
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = RequestAssembler._compute_phase(session, books, task)
        assert result == "reviewing"

    def test_planning_phase_default(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        books = SimpleNamespace(failed_streak=0)
        task = None
        session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
        result = RequestAssembler._compute_phase(session, books, task)
        assert result == "planning"


# ---------------------------------------------------------------------------
# SOPRouter phase filtering
# ---------------------------------------------------------------------------


class TestSOPRouterPhaseFiltering:
    """SOPRouter.route() should filter skills by phase."""

    def _make_reg(self, tmp_path: Path, skills_data: dict) -> SkillRegistry:
        for name, data in skills_data.items():
            frontmatter = f"name: {name}\ndescription: {name}\n"
            if data.get("phase"):
                frontmatter += f"phase: {data['phase']}\n"
            if data.get("triggers"):
                frontmatter += "triggers:\n"
                for key, values in data["triggers"].items():
                    frontmatter += f"  {key}: {values}\n"
            skill_dir = tmp_path / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\n{frontmatter}\n---\n\n# {name}\n"
            )
        return SkillRegistry(tmp_path)

    def test_filters_by_phase(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "plan-skill": {"phase": "planning", "triggers": {"keywords": ["计划"]}},
            "debug-skill": {"phase": "debugging", "triggers": {"keywords": ["错误"]}},
        })
        router = SOPRouter(reg, threshold=1.0)
        # In planning phase, only plan-skill should match
        matches = router.route("计划任务", [], "", [], phase="planning")
        assert len(matches) == 1
        assert matches[0].name == "plan-skill"

    def test_no_phase_filter_when_none(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "plan-skill": {"phase": "planning", "triggers": {"keywords": ["计划"]}},
            "debug-skill": {"phase": "debugging", "triggers": {"keywords": ["错误"]}},
        })
        router = SOPRouter(reg, threshold=1.0)
        # No phase filter — both can match their keywords
        matches = router.route("计划错误", [], "", [], phase=None)
        assert len(matches) == 2

    def test_skill_without_phase_always_matches(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "universal-skill": {"phase": "", "triggers": {"keywords": ["测试"]}},
            "debug-skill": {"phase": "debugging", "triggers": {"keywords": ["测试"]}},
        })
        router = SOPRouter(reg, threshold=1.0)
        # universal-skill has no phase lock, should match in any phase
        matches = router.route("测试", [], "", [], phase="planning")
        names = [m.name for m in matches]
        assert "universal-skill" in names
