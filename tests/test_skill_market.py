"""TDD (F1): Skill Market — dynamic pricing, memory staking, co-activation graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.core.sop_router import (
    PRICE_ALPHA,
    PRICE_MAX,
    PRICE_MIN,
    SOPRouter,
    SkillMatch,
    SkillStats,
)
from ai_coding.memory.note import MemoryNote, new_note
from ai_coding.memory.store import MemoryStore
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
# 1. Dynamic Pricing Tests
# ---------------------------------------------------------------------------


class TestDynamicPricing:
    """Skill price via Bayesian updating."""

    def test_price_initial_value(self):
        """New skill starts at price=0.5 (max uncertainty)."""
        stats = SkillStats()
        assert stats.price == 0.5

    def test_price_increases_on_success(self):
        """After a successful use, price increases."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        router.record_outcome("test-skill", True, stats)
        assert stats["test-skill"].price > 0.5

    def test_price_decreases_on_failure(self):
        """After a failed use, price decreases."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        router.record_outcome("test-skill", False, stats)
        assert stats["test-skill"].price < 0.5

    def test_price_clamped_min(self):
        """Price never goes below PRICE_MIN."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        for _ in range(20):
            router.record_outcome("test-skill", False, stats)
        assert stats["test-skill"].price >= PRICE_MIN

    def test_price_clamped_max(self):
        """Price never exceeds PRICE_MAX."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        for _ in range(20):
            router.record_outcome("test-skill", True, stats)
        assert stats["test-skill"].price <= PRICE_MAX

    def test_price_weighted_scoring(self):
        """Two skills with same base score but different prices — higher price wins."""
        tmp_path = Path("/tmp/test_pricing")
        tmp_path.mkdir(exist_ok=True)
        _write_skill(tmp_path, "skill-a", (
            "name: skill-a\n"
            "description: Skill A\n"
            "triggers:\n"
            "  keywords: [\"test\"]\n"
        ))
        _write_skill(tmp_path, "skill-b", (
            "name: skill-b\n"
            "description: Skill B\n"
            "triggers:\n"
            "  keywords: [\"test\"]\n"
        ))
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=0.0)

        stats = {
            "skill-a": SkillStats(price=0.9),
            "skill-b": SkillStats(price=0.3),
        }
        matches = router.route(
            user_input="test something",
            recent_tools=[],
            task_goal="",
            recent_failures=[],
            turn_number=1,
            stats=stats,
        )
        # Both should match, but skill-a should have higher total score
        assert len(matches) == 2
        skill_a = next(m for m in matches if m.name == "skill-a")
        skill_b = next(m for m in matches if m.name == "skill-b")
        assert skill_a.score > skill_b.score

    def test_price_display_in_match(self):
        """SkillMatch includes price for display."""
        tmp_path = Path("/tmp/test_pricing_display")
        tmp_path.mkdir(exist_ok=True)
        _write_skill(tmp_path, "skill-a", (
            "name: skill-a\n"
            "description: Skill A\n"
            "triggers:\n"
            "  keywords: [\"test\"]\n"
        ))
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=0.0)
        stats = {"skill-a": SkillStats(price=0.8)}
        matches = router.route(
            user_input="test",
            recent_tools=[],
            task_goal="",
            recent_failures=[],
            turn_number=1,
            stats=stats,
        )
        assert len(matches) == 1
        assert matches[0].price == 0.8

    def test_price_convergence(self):
        """After many uses, price converges to true success rate."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        # Simulate a skill with 70% success rate
        import random
        random.seed(42)
        for _ in range(100):
            success = random.random() < 0.7
            router.record_outcome("test-skill", success, stats)
        # Price should be roughly 0.7 (within tolerance)
        assert 0.6 < stats["test-skill"].price < 0.8

    def test_record_outcome_empty_skill_name(self):
        """record_outcome with empty skill name is a no-op."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        router.record_outcome("", True, stats)
        assert len(stats) == 0


# ---------------------------------------------------------------------------
# 2. Memory Staking Tests
# ---------------------------------------------------------------------------


class TestMemoryStaking:
    """Memory stake increases on successful recall, decreases on contradiction."""

    def test_stake_initial_value(self):
        """New note has stake=1.0."""
        note = new_note("test memory")
        assert note.stake == 1.0

    def test_stake_increases_on_recall(self):
        """After successful recall, stake increases."""
        store = MemoryStore(Path("/tmp/test_stake_increase.json"))
        note_id = store.add_note(new_note("test memory"))
        note = store.get(note_id)
        assert note is not None
        old_stake = note.stake
        store.update_stake(note_id, old_stake * 1.2)
        new_note_obj = store.get(note_id)
        assert new_note_obj is not None
        assert new_note_obj.stake > old_stake

    def test_stake_slash_on_supersede(self):
        """When a note is contradicted, its stake is slashed."""
        store = MemoryStore(Path("/tmp/test_stake_slash.json"))
        old_id = store.add_note(new_note("old fact"))
        old_note = store.get(old_id)
        assert old_note is not None
        # Slash the old note's stake
        store.update_stake(old_id, old_note.stake * 0.7)
        slashed_note = store.get(old_id)
        assert slashed_note is not None
        assert slashed_note.stake < old_note.stake

    def test_stake_weighted_eviction(self):
        """Low-stake note is evicted before high-stake note."""
        store = MemoryStore(Path("/tmp/test_stake_eviction.json"))
        # Add two notes with different stakes
        low_id = store.add_note(new_note("low value", importance=1))
        high_id = store.add_note(new_note("high value", importance=1))
        # Set different stakes
        store.update_stake(low_id, 0.1)
        store.update_stake(high_id, 3.0)
        # Evict to 1 note
        evicted = store.evict(1)
        assert low_id in evicted
        assert high_id not in evicted

    def test_stake_serialization(self):
        """stake survives to_dict/from_dict round-trip."""
        note = new_note("test memory")
        data = note.to_dict()
        # stake=1.0 is default, so it should NOT be in dict
        assert "stake" not in data
        # Create with non-default stake
        note2 = new_note("test memory 2").with_stake(2.5)
        data2 = note2.to_dict()
        assert "stake" in data2
        assert data2["stake"] == 2.5
        # Round-trip
        note3 = MemoryNote.from_dict(data2)
        assert note3.stake == 2.5

    def test_stake_clamped_min(self):
        """stake never goes below 0."""
        note = new_note("test memory")
        new_stake = note.with_stake(-1.0)
        assert new_stake.stake == 0.0

    def test_update_stake_nonexistent_note(self):
        """update_stake returns False for nonexistent note."""
        store = MemoryStore(Path("/tmp/test_stake_nonexistent.json"))
        assert store.update_stake("nonexistent-id", 2.0) is False


# ---------------------------------------------------------------------------
# 3. Co-activation Graph Tests
# ---------------------------------------------------------------------------


class TestCoactivationGraph:
    """Skill co-activation tracking."""

    def test_coactivation_tracking(self):
        """Using skill A then skill B records an edge."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        graph = {}
        router.record_coactivation("skill-a", "skill-b", graph)
        assert "skill-a" in graph
        assert graph["skill-a"]["skill-b"] == 1

    def test_coactivation_accumulates(self):
        """Multiple co-activations accumulate."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        graph = {}
        for _ in range(3):
            router.record_coactivation("skill-a", "skill-b", graph)
        assert graph["skill-a"]["skill-b"] == 3

    def test_coactivation_self_loop_ignored(self):
        """Self-loop (A→A) is ignored."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        graph = {}
        router.record_coactivation("skill-a", "skill-a", graph)
        assert len(graph) == 0

    def test_coactivation_empty_skill_ignored(self):
        """Empty skill name is ignored."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        graph = {}
        router.record_coactivation("", "skill-b", graph)
        router.record_coactivation("skill-a", "", graph)
        assert len(graph) == 0

    def test_followup_preload(self):
        """High-frequency followup skills are identified."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        graph = {"skill-a": {"skill-b": 3, "skill-c": 1}}
        followups = router.get_followup_skills("skill-a", graph)
        assert "skill-b" in followups  # count=3 >= threshold=2
        assert "skill-c" not in followups  # count=1 < threshold=2

    def test_followup_no_edges(self):
        """No edges returns empty list."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        followups = router.get_followup_skills("nonexistent", {})
        assert followups == []


# ---------------------------------------------------------------------------
# 4. Integration Tests
# ---------------------------------------------------------------------------


class TestSkillMarketIntegration:
    """End-to-end workflow tests."""

    def test_full_workflow_pricing(self):
        """Skill recommended → used → outcome recorded → price updated → next routing affected."""
        tmp_path = Path("/tmp/test_full_workflow")
        tmp_path.mkdir(exist_ok=True)
        _write_skill(tmp_path, "good-skill", (
            "name: good-skill\n"
            "description: A good skill\n"
            "triggers:\n"
            "  keywords: [\"test\"]\n"
        ))
        _write_skill(tmp_path, "bad-skill", (
            "name: bad-skill\n"
            "description: A bad skill\n"
            "triggers:\n"
            "  keywords: [\"test\"]\n"
        ))
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=0.0)

        # Initial routing — both should match with same base score
        stats = {}
        matches = router.route(
            user_input="test",
            recent_tools=[],
            task_goal="",
            recent_failures=[],
            turn_number=1,
            stats=stats,
        )
        assert len(matches) == 2

        # Simulate: good-skill succeeds, bad-skill fails
        router.record_outcome("good-skill", True, stats)
        router.record_outcome("bad-skill", False, stats)

        # Second routing — good-skill should now have higher score
        matches = router.route(
            user_input="test",
            recent_tools=[],
            task_goal="",
            recent_failures=[],
            turn_number=2,
            stats=stats,
        )
        good = next(m for m in matches if m.name == "good-skill")
        bad = next(m for m in matches if m.name == "bad-skill")
        assert good.score > bad.score

    def test_format_block_shows_confidence(self):
        """format_block includes confidence percentage."""
        match = SkillMatch(
            name="test-skill",
            description="A test skill",
            score=5.0,
            matched_dimensions=("keyword",),
            price=0.85,
        )
        block = SOPRouter.format_block([match])
        assert "confidence: 85%" in block

    def test_memory_stake_affects_eviction_order(self):
        """Stake affects eviction order — high stake survives."""
        store = MemoryStore(Path("/tmp/test_stake_order.json"))
        # Add 3 notes with same importance but different stakes
        id1 = store.add_note(new_note("note 1", importance=2))
        id2 = store.add_note(new_note("note 2", importance=2))
        id3 = store.add_note(new_note("note 3", importance=2))
        store.update_stake(id1, 0.1)  # low stake
        store.update_stake(id2, 1.0)  # default
        store.update_stake(id3, 5.0)  # high stake
        # Evict to 1 note
        evicted = store.evict(1)
        assert id1 in evicted  # lowest stake evicted first
        assert id3 not in evicted  # highest stake survives
