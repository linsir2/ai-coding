"""TDD (M2): SOP routing — match context against skill triggers.

Covers: skill frontmatter parsing, SOPRouter scoring/threshold/format,
RequestAssembler integration, and AgentLoop wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill(tmp_path: Path, name: str, frontmatter: str, body: str = "# Skill\n") -> Path:
    """Write a SKILL.md with given frontmatter."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter}\n---\n\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# skill frontmatter -> triggers
# ---------------------------------------------------------------------------


class TestSkillRegistryTriggers:
    """triggers from frontmatter."""

    def test_parses_full_triggers(self, tmp_path):
        _write_skill(tmp_path, "test-skill", (
            "name: test-skill\n"
            "description: A test skill\n"
            "triggers:\n"
            "  keywords: [\"沙箱\", \"workspace\"]\n"
            "  tools: [\"write\", \"edit\"]\n"
            "  task_types: [\"file_creation\"]\n"
            "  failure_patterns: [\"resolves outside\"]\n"
        ))
        reg = SkillRegistry(tmp_path)
        triggers = reg.get("test-skill").triggers
        assert triggers is not None
        assert triggers.keywords == ("沙箱", "workspace")
        assert triggers.tools == ("write", "edit")
        assert triggers.task_types == ("file_creation",)
        assert triggers.failure_patterns == ("resolves outside",)

    def test_returns_none_when_no_triggers(self, tmp_path):
        _write_skill(tmp_path, "no-triggers", "name: no-triggers\ndescription: test\n")
        reg = SkillRegistry(tmp_path)
        assert not reg.get("no-triggers").triggers

    def test_returns_none_when_no_frontmatter(self, tmp_path):
        skill_dir = tmp_path / "no-fm"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Just a title\n")
        reg = SkillRegistry(tmp_path)
        assert not reg.get("no-fm").triggers

    def test_returns_none_when_file_missing(self, tmp_path):
        reg = SkillRegistry(tmp_path)
        assert reg.get("nonexistent") is None

    def test_handles_string_values(self, tmp_path):
        """YAML allows `keywords: "single"` instead of arrays."""
        _write_skill(tmp_path, "string-triggers", (
            "name: string-triggers\n"
            "triggers:\n"
            "  keywords: \"沙箱\"\n"
            "  tools: \"write\"\n"
        ))
        reg = SkillRegistry(tmp_path)
        triggers = reg.get("string-triggers").triggers
        assert triggers.keywords == ("沙箱",)
        assert triggers.tools == ("write",)

    def test_handles_empty_triggers(self, tmp_path):
        _write_skill(tmp_path, "empty-triggers", (
            "name: empty-triggers\n"
            "triggers:\n"
        ))
        reg = SkillRegistry(tmp_path)
        triggers = reg.get("empty-triggers").triggers
        # present but empty, which is different from absent
        assert not triggers

    def test_handles_malformed_yaml(self, tmp_path):
        """The old answer was an empty dict indistinguishable from "no triggers
        declared". Now the skill says it could not be parsed, which is the fact."""
        skill_dir = tmp_path / "bad-yaml"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: bad\ntriggers: [invalid yaml: :\n---\n\n# Body\n"
        )
        reg = SkillRegistry(tmp_path)
        result = reg.get("bad-yaml")
        assert result.error is not None
        assert not result.triggers


# ---------------------------------------------------------------------------
# SOPRouter scoring
# ---------------------------------------------------------------------------


class TestSOPRouterScoring:
    """SOPRouter.route() — scoring logic."""

    def _make_skills(self, tmp_path: Path, skills_data: dict) -> SkillRegistry:
        """Create a registry with multiple skills."""
        for name, triggers in skills_data.items():
            frontmatter = f"name: name\ndescription: {name}\n"
            if triggers:
                frontmatter += "triggers:\n"
                for key, values in triggers.items():
                    frontmatter += f"  {key}: {values}\n"
            _write_skill(tmp_path, name, frontmatter)
        return SkillRegistry(tmp_path)

    def test_keyword_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "sandbox-fix": {"keywords": ["沙箱", "workspace"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("沙箱越界了", [], "", [])
        assert len(matches) == 1
        assert matches[0].name == "sandbox-fix"
        assert "keyword" in matches[0].matched_dimensions

    def test_tool_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "write-helper": {"tools": ["write", "edit"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("", ["write"], "", [])
        assert len(matches) == 1
        assert "tool" in matches[0].matched_dimensions

    def test_failure_pattern_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "sandbox-fix": {"failure_patterns": ["resolves outside workspace"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("", "", "", ["Error: resolves outside workspace"])
        assert len(matches) == 1
        assert "failure_pattern" in matches[0].matched_dimensions

    def test_task_type_match(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "file-creator": {"task_types": ["file_creation"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("", "", "file_creation task", [])
        assert len(matches) == 1
        assert "task_type" in matches[0].matched_dimensions

    def test_multiple_matches_sum(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "multi-match": {
                "keywords": ["沙箱"],
                "tools": ["write"],
                "failure_patterns": ["resolves outside"],
            },
        })
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("沙箱问题", ["write"], "", ["resolves outside workspace"])
        assert len(matches) == 1
        assert matches[0].score >= 3.0 + 2.0 + 4.0  # keyword + tool + failure
        assert set(matches[0].matched_dimensions) == {"keyword", "tool", "failure_pattern"}

    def test_no_match(self, tmp_path):
        from ai_coding.core.sop_router import SkillStats, SOPRouter
        reg = self._make_skills(tmp_path, {
            "unrelated": {"keywords": ["数据库"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        # M4: pass stats to suppress freshness bonus
        stats = {"unrelated": SkillStats(last_recommended_turn=1)}
        matches = router.route("前端页面布局", ["bash"], "", [],
                               turn_number=1, stats=stats)
        assert len(matches) == 0

    def test_threshold_filters_low_scores(self, tmp_path):
        from ai_coding.core.sop_router import SkillStats, SOPRouter
        reg = self._make_skills(tmp_path, {
            "weak-match": {"tools": ["write"]},  # score = 2.0
        })
        router = SOPRouter(reg, threshold=3.0)
        # M4: pass stats to suppress freshness bonus
        stats = {"weak-match": SkillStats(last_recommended_turn=1)}
        matches = router.route("", ["write"], "", [],
                               turn_number=1, stats=stats)
        assert len(matches) == 0  # 2.0 < 3.0 threshold

    def test_max_matches_limits_results(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        skills = {f"skill-{i}": {"keywords": ["测试"]} for i in range(10)}
        reg = self._make_skills(tmp_path, skills)
        router = SOPRouter(reg, threshold=1.0, max_matches=3)
        matches = router.route("测试任务", [], "", [])
        assert len(matches) == 3

    def test_skills_without_triggers_skipped(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        _write_skill(tmp_path, "no-triggers", "name: no-triggers\ndescription: test\n")
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("任何输入", [], "", [])
        assert len(matches) == 0

    def test_empty_skills_directory(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("输入", ["tool"], "goal", ["error"])
        assert len(matches) == 0

    def test_sorted_by_score_descending(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "strong": {"failure_patterns": ["error"], "keywords": ["测试"]},  # 4+3=7
            "medium": {"keywords": ["测试"]},  # 3
            "weak": {"tools": ["bash"]},  # 2
        })
        router = SOPRouter(reg, threshold=1.0)
        matches = router.route("测试", ["bash"], "", ["error occurred"])
        assert len(matches) == 3
        assert matches[0].name == "strong"
        assert matches[1].name == "medium"
        assert matches[2].name == "weak"


# ---------------------------------------------------------------------------
# SOPRouter.format_block
# ---------------------------------------------------------------------------


class TestSOPRouterFormatBlock:
    """SOPRouter.format_block() — format matches as tail block."""

    def test_formats_matches(self):
        from ai_coding.core.sop_router import SkillMatch, SOPRouter
        matches = [
            SkillMatch(name="sandbox-fix", description="Fix sandbox issues", score=7.0,
                       matched_dimensions=("keyword", "failure_pattern")),
            SkillMatch(name="write-helper", description="Help with writes", score=2.0,
                       matched_dimensions=("tool",)),
        ]
        block = SOPRouter.format_block(matches)
        assert "<recommended-skills>" in block
        assert "sandbox-fix" in block
        assert "Fix sandbox issues" in block
        # The call must name a parameter the tool actually has. It used to say
        # `id=`, which is not in {action, name}, so the block instructed the model
        # to make a call it could not make.
        assert 'skill(action="read", name="sandbox-fix")' in block
        assert "id=" not in block
        assert "keyword, failure_pattern" in block
        assert "</recommended-skills>" in block

    def test_empty_matches_returns_empty(self):
        from ai_coding.core.sop_router import SOPRouter
        assert SOPRouter.format_block([]) == ""


# ---------------------------------------------------------------------------
# RequestAssembler integration
# ---------------------------------------------------------------------------


class TestRequestAssemblerIntegration:
    """RequestAssembler injects recommended skills into tail."""

    def test_injects_recommended_skills_block(self, tmp_path):
        """When skills match, <recommended-skills> block is in the request."""
        from ai_coding.core.sop_router import SOPRouter
        from ai_coding.skills.registry import SkillRegistry

        # Create a skill with triggers
        _write_skill(tmp_path, "sandbox-fix", (
            "name: sandbox-fix\n"
            "description: Fix sandbox issues\n"
            "triggers:\n"
            "  keywords: [\"沙箱\"]\n"
        ))
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=1.0)

        # Verify router works
        matches = router.route("沙箱越界", [], "", [])
        assert len(matches) == 1

        block = SOPRouter.format_block(matches)
        assert "<recommended-skills>" in block
        assert "sandbox-fix" in block

    def test_no_injection_when_no_match(self, tmp_path):
        """When no skills match, no block is injected."""
        from ai_coding.core.sop_router import SkillStats, SOPRouter
        from ai_coding.skills.registry import SkillRegistry

        _write_skill(tmp_path, "unrelated", (
            "name: unrelated\n"
            "description: Unrelated\n"
            "triggers:\n"
            "  keywords: [\"数据库\"]\n"
        ))
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=1.0)

        # M4: pass stats to suppress freshness bonus
        stats = {"unrelated": SkillStats(last_recommended_turn=1)}
        matches = router.route("前端页面", [], "", [],
                               turn_number=1, stats=stats)
        assert len(matches) == 0

        block = SOPRouter.format_block(matches)
        assert block == ""


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Test helper functions for extracting context."""

    def test_extract_user_input(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SimpleNamespace(messages=[
            ChatMessage(role="user", content="第一条消息", timestamp="t1"),
            ChatMessage(role="assistant", content="回复", timestamp="t2"),
            ChatMessage(role="user", content="第二条消息", timestamp="t3"),
        ])
        result = RequestAssembler._extract_user_input(session)
        assert result == "第二条消息"

    def test_extract_user_input_no_user_messages(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SimpleNamespace(messages=[
            ChatMessage(role="assistant", content="回复", timestamp="t1"),
        ])
        result = RequestAssembler._extract_user_input(session)
        assert result == ""

    def test_extract_user_input_empty_messages(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SimpleNamespace(messages=[])
        result = RequestAssembler._extract_user_input(session)
        assert result == ""

    def test_extract_recent_tools(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SimpleNamespace(messages=[
            ChatMessage(role="assistant", content="", timestamp="t1", tool_calls=[
                SimpleNamespace(name="read"),
                SimpleNamespace(name="write"),
            ]),
        ])
        books = SimpleNamespace(tool_success={"bash"}, failures=[])
        result = RequestAssembler._extract_recent_tools(session, books)
        assert set(result) == {"read", "write", "bash"}

    def test_extract_recent_tools_empty(self, tmp_path):
        from ai_coding.core.request_assembly import RequestAssembler
        session = SimpleNamespace(messages=[])
        books = SimpleNamespace(tool_success=set(), failures=[])
        result = RequestAssembler._extract_recent_tools(session, books)
        assert result == []


def test_a_disabled_skill_is_not_recommended(tmp_path):
    from ai_coding.core.sop_router import SOPRouter

    _write_skill(
        tmp_path,
        "quiet-fix",
        "name: quiet-fix\ndescription: d\ndisable-model-invocation: true\n"
        "triggers:\n  keywords: ['沙箱']\n",
    )
    router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

    assert router.route("沙箱越界了", [], "", []) == []


# ---------------------------------------------------------------------------
# SKL-01: an operator deny applies to every automatic injection face
# ---------------------------------------------------------------------------


def _deniable_skill(tmp_path, name: str) -> None:
    _write_skill(
        tmp_path,
        name,
        f"name: {name}\ndescription: d\npattern_dna: 用{name}修复\n"
        "triggers:\n"
        "  keywords: ['沙箱']\n"
        "  failure_patterns: ['boom']\n",
    )


class TestDeniedSkills:
    def test_a_denied_skill_is_not_routed(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter

        _deniable_skill(tmp_path, "sandbox-fix")
        router = SOPRouter(
            SkillRegistry(tmp_path), threshold=1.0,
            denied=frozenset({"sandbox-fix"}),
        )

        assert router.route("沙箱越界了", [], "", ["boom"]) == []

    def test_a_denied_skill_does_not_intervene(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter

        _deniable_skill(tmp_path, "sandbox-fix")
        router = SOPRouter(
            SkillRegistry(tmp_path), threshold=1.0,
            denied=frozenset({"sandbox-fix"}),
        )

        assert router.intervene(["boom", "boom"], None, None, None).mode == "none"

    def test_a_denied_skill_does_not_recover(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter

        _deniable_skill(tmp_path, "sandbox-fix")
        router = SOPRouter(
            SkillRegistry(tmp_path), threshold=1.0,
            denied=frozenset({"sandbox-fix"}),
        )

        assert router.check_recovery(["boom"], None) is None

    def test_denial_does_not_leak_to_other_skills(self, tmp_path):
        """Deny one id, not the whole tail."""
        from ai_coding.core.sop_router import SOPRouter

        _deniable_skill(tmp_path, "sandbox-fix")
        _deniable_skill(tmp_path, "other-fix")
        router = SOPRouter(
            SkillRegistry(tmp_path), threshold=1.0,
            denied=frozenset({"sandbox-fix"}),
        )

        names = [m.name for m in router.route("沙箱越界了", [], "", ["boom"])]

        assert "other-fix" in names
        assert "sandbox-fix" not in names


# ---------------------------------------------------------------------------
# SKL-03: injected text has a budget
# ---------------------------------------------------------------------------


class TestInjectionBudget:
    def test_format_block_shortens_a_long_description(self):
        from ai_coding.core.sop_router import SkillMatch, SOPRouter

        match = SkillMatch(
            name="verbose", description="d" * 400, score=3.0,
            matched_dimensions=("keyword",),
        )

        block = SOPRouter.format_block([match])

        assert "d" * 400 not in block
        assert "d" * 200 + "…" in block

    def test_format_block_keeps_a_short_description(self):
        from ai_coding.core.sop_router import SkillMatch, SOPRouter

        match = SkillMatch(
            name="short", description="fix sandbox issues", score=3.0,
            matched_dimensions=("keyword",),
        )

        assert "fix sandbox issues" in SOPRouter.format_block([match])

    def test_hard_intervention_steps_are_capped(self, tmp_path):
        from ai_coding.core.sop_router import MAX_INTERVENTION_STEPS_CHARS, SOPRouter

        _write_skill(
            tmp_path,
            "verbose-fix",
            "name: verbose-fix\ndescription: d\n"
            "triggers:\n  failure_patterns: ['boom']\n",
            body="## 步骤\n" + ("x" * 2_000),
        )
        router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

        result = router.intervene(["boom", "boom"], None, None, None)

        assert result.mode == "hard"
        assert len(result.content) <= MAX_INTERVENTION_STEPS_CHARS + 1
        assert result.content.endswith("…")

    def test_soft_pattern_dna_is_capped(self, tmp_path):
        from ai_coding.core.sop_router import MAX_INTERVENTION_STEPS_CHARS, SOPRouter

        _write_skill(
            tmp_path,
            "verbose-dna",
            "name: verbose-dna\ndescription: d\n"
            f"pattern_dna: {'y' * 2_000}\n"
            "triggers:\n  failure_patterns: ['boom']\n",
        )
        router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

        result = router.intervene(["boom"], None, None, None)

        assert result.mode == "soft"
        assert len(result.content) <= MAX_INTERVENTION_STEPS_CHARS + 1

    def test_recovery_pattern_dna_is_capped(self, tmp_path):
        from ai_coding.core.sop_router import MAX_INTERVENTION_STEPS_CHARS, SOPRouter

        _write_skill(
            tmp_path,
            "verbose-dna",
            "name: verbose-dna\ndescription: d\n"
            f"pattern_dna: {'y' * 2_000}\n"
            "triggers:\n  failure_patterns: ['boom']\n",
        )
        router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

        recovery = router.check_recovery(["boom"], None)

        assert recovery is not None
        assert len(recovery) <= MAX_INTERVENTION_STEPS_CHARS + 1

    def test_a_body_without_a_steps_section_says_that_it_was_cut(self, tmp_path):
        """The fallback sliced ``body[:limit]`` and then clipped, so the cut was
        silent: the model read a sentence that stopped mid-word with no sign
        that anything was missing."""
        from ai_coding.core.sop_router import MAX_INTERVENTION_STEPS_CHARS, SOPRouter

        _write_skill(
            tmp_path,
            "long-body",
            "name: long-body\ndescription: d\n"
            "triggers:\n  failure_patterns: ['boom']\n",
            body="z" * 2_000,
        )
        router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

        result = router.intervene(["boom", "boom"], None, None, None)

        assert result.mode == "hard"
        assert result.content.endswith("…")
        assert len(result.content) <= MAX_INTERVENTION_STEPS_CHARS + 1


# ---------------------------------------------------------------------------
# SKL-06: Dendritic index robustness and phase validation
# ---------------------------------------------------------------------------


class TestDendriticAndPhase:
    def test_match_once_per_route(self, tmp_path, monkeypatch):
        from ai_coding.core import sop_router as sr

        calls: list[int] = []

        def fake_match(self, tools, errors, task_keywords):
            calls.append(1)
            return []

        monkeypatch.setattr(sr.DendriticIndex, "match", fake_match)
        for name in ("a", "b", "c"):
            _write_skill(
                tmp_path, name,
                f"name: {name}\ndescription: d\ntriggers:\n  keywords: ['x']\n",
            )
        router = sr.SOPRouter(
            SkillRegistry(tmp_path), threshold=1.0,
            dendritic_path=tmp_path / "index.json",
        )

        router.route("x", [], "", [])

        assert len(calls) == 1

    def test_a_corrupt_signatures_field_is_ignored(self, tmp_path):
        """A hand-edited index must not crash every request once wired in."""
        from ai_coding.core.sop_router import DendriticIndex

        path = tmp_path / "index.json"
        path.write_text('{"version": 1, "signatures": "not-a-list"}', encoding="utf-8")
        idx = DendriticIndex(path)

        assert idx.match(tools={"bash"}, errors=set(), task_keywords=set()) == []

    def test_an_empty_query_matches_nothing(self, tmp_path):
        """∅∩∅ is 1.0 in Jaccard; without a guard that boosts every skill."""
        from ai_coding.core.sop_router import (
            DendriticIndex,
            StructuralSignature,
        )

        idx = DendriticIndex(tmp_path / "index.json")
        idx.record(StructuralSignature(
            tools=frozenset(),
            error_keywords=frozenset(),
            task_keywords=frozenset(),
            skill_name="anything",
            timestamp="2024-01-01T00:00:00Z",
        ))

        assert idx.match(tools=set(), errors=set(), task_keywords=set()) == []

    def test_a_dimension_empty_on_both_sides_is_skipped(self, tmp_path):
        """The query guard above only covers a wholly empty context. Here the
        tools match but neither side carries an error or a task keyword --
        ``_jaccard(∅, ∅) == 1.0`` used to add 0.3 + 0.3 of free similarity."""
        from ai_coding.core.sop_router import (
            WEIGHT_TOOLS,
            DendriticIndex,
            StructuralSignature,
        )

        idx = DendriticIndex(tmp_path / "index.json")
        idx.record(StructuralSignature(
            tools=frozenset({"bash"}),
            error_keywords=frozenset(),
            task_keywords=frozenset(),
            skill_name="shell-fix",
            timestamp="2024-01-01T00:00:00Z",
        ))

        matches = idx.match(tools={"bash"}, errors=set(), task_keywords=set())

        assert [name for name, _ in matches] == ["shell-fix"]
        assert matches[0][1] == pytest.approx(WEIGHT_TOOLS)

    def test_an_unknown_phase_value_is_not_a_permanent_lock(self, tmp_path):
        """A typo (``debuging``) loads as no lock, so it still routes."""
        from ai_coding.core.sop_router import SOPRouter

        _write_skill(
            tmp_path,
            "typo-skill",
            "name: typo-skill\ndescription: d\nphase: debuging\n"
            "triggers:\n  keywords: ['沙箱']\n",
        )
        router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

        names = [m.name for m in router.route("沙箱越界", [], "", [], phase="debugging")]

        assert names == ["typo-skill"]

    def test_a_known_mismatched_phase_is_still_filtered(self, tmp_path):
        from ai_coding.core.sop_router import SOPRouter

        _write_skill(
            tmp_path,
            "plan-skill",
            "name: plan-skill\ndescription: d\nphase: planning\n"
            "triggers:\n  keywords: ['沙箱']\n",
        )
        router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

        assert router.route("沙箱越界", [], "", [], phase="debugging") == []
        assert [m.name for m in router.route("沙箱越界", [], "", [], phase="planning")] == [
            "plan-skill"
        ]

