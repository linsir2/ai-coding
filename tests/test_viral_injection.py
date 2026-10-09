"""TDD (F3): Viral pattern injection + phage hijack — inject skill patterns on repeated failures."""

from __future__ import annotations

from pathlib import Path

from ai_coding.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# Frontmatter: pattern_dna field
# ---------------------------------------------------------------------------


class TestPatternDnaFrontmatter:
    """SkillRegistry.meta() should parse pattern_dna field."""

    def test_parses_pattern_dna(self, tmp_path):
        skill_dir = tmp_path / "sandbox-fix"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: sandbox-fix\n"
            "description: Fix sandbox issues\n"
            'pattern_dna: "写外部路径被拒 → 改用 bash heredoc: cat > path << EOF"\n'
            "---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        meta = reg.meta("sandbox-fix")
        assert "bash heredoc" in meta["pattern_dna"]

    def test_no_pattern_dna(self, tmp_path):
        skill_dir = tmp_path / "simple"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: simple\ndescription: test\n---\n\n# Skill\n"
        )
        reg = SkillRegistry(tmp_path)
        meta = reg.meta("simple")
        assert meta["pattern_dna"] == ""


# ---------------------------------------------------------------------------
# SOPRouter.intervene()
# ---------------------------------------------------------------------------


class TestIntervene:
    """SOPRouter.intervene() — decide intervention level based on failures."""

    def _make_reg(self, tmp_path: Path, skills_data: dict) -> SkillRegistry:
        for name, data in skills_data.items():
            frontmatter = f"name: {name}\ndescription: {name}\n"
            if data.get("pattern_dna"):
                frontmatter += f"pattern_dna: {data['pattern_dna']}\n"
            if data.get("triggers"):
                frontmatter += "triggers:\n"
                for key, values in data["triggers"].items():
                    frontmatter += f"  {key}: {values}\n"
            skill_dir = tmp_path / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                f"---\n{frontmatter}\n---\n\n# {name}\n## 使用场景\n...\n## 步骤\n1. ...\n## 验证\n...\n"
            )
        return SkillRegistry(tmp_path)

    def test_no_intervention_when_no_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "sandbox-fix": {
                "pattern_dna": "Use bash heredoc",
                "triggers": {"failure_patterns": ["resolves outside"]},
            },
        })
        router = SOPRouter(reg)
        result = router.intervene([], [], None, {})
        assert result.mode == "none"

    def test_soft_intervention_on_first_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "sandbox-fix": {
                "pattern_dna": "Use bash heredoc for external paths",
                "triggers": {"failure_patterns": ["resolves outside"]},
            },
        })
        router = SOPRouter(reg)
        failures = ["Error: path resolves outside workspace"]
        observations = []
        result = router.intervene(failures, observations, None, {})
        assert result.mode == "soft"
        assert result.skill_name == "sandbox-fix"
        assert "bash heredoc" in result.content

    def test_hard_intervention_on_repeated_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "sandbox-fix": {
                "pattern_dna": "Use bash heredoc",
                "triggers": {"failure_patterns": ["resolves outside"]},
            },
        })
        router = SOPRouter(reg)
        failures = [
            "Error: path resolves outside workspace",
            "Error: path resolves outside workspace",
        ]
        observations = []
        result = router.intervene(failures, observations, None, {})
        assert result.mode == "hard"
        assert result.skill_name == "sandbox-fix"

    def test_hard_intervention_via_observations(self, tmp_path):
        """Observations (M3) also count toward intervention threshold."""
        from ai_coding.core.sop_router import SOPRouter, ToolObservation
        reg = self._make_reg(tmp_path, {
            "sandbox-fix": {
                "pattern_dna": "Use bash heredoc",
                "triggers": {"failure_patterns": ["resolves outside"]},
            },
        })
        router = SOPRouter(reg)
        # One failure in books + one in observations = 2 matches → hard
        failures = ["Error: path resolves outside workspace"]
        observations = [
            ToolObservation(tool_name="write", success=False,
                            error="resolves outside workspace", timestamp="t1"),
        ]
        result = router.intervene(failures, observations, None, {})
        assert result.mode == "hard"

    def test_intervention_loads_full_steps_for_hard(self, tmp_path):
        """Hard intervention should include the skill's steps section."""
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_reg(tmp_path, {
            "sandbox-fix": {
                "pattern_dna": "Use bash heredoc",
                "triggers": {"failure_patterns": ["resolves outside"]},
            },
        })
        router = SOPRouter(reg)
        failures = ["resolves outside"] * 2
        result = router.intervene(failures, [], None, {})
        assert result.mode == "hard"
        # Should contain the steps section
        assert "步骤" in result.content or "steps" in result.content.lower()
