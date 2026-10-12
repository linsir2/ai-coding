"""End-to-end integration tests for F1+F2+F3 adaptive learning features.

Simulates real-world scenarios to verify the three features work together.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ai_coding.core.dispatch import DispatchOutcome
from ai_coding.core.sop_router import (
    DendriticIndex,
    SkillStats,
    SOPRouter,
    StructuralSignature,
)
from ai_coding.core.turn_books import TurnBooks
from ai_coding.core.turn_policy import TurnAction, TurnPolicy, TurnPolicyConfig, TurnSnapshot
from ai_coding.domain.message import ToolCall, ToolResult
from ai_coding.memory.note import new_note
from ai_coding.memory.store import MemoryStore
from ai_coding.skills.registry import SkillRegistry


def _outcome(name="bash", *, success=True, error=None, output=""):
    return DispatchOutcome(
        call=ToolCall(id=f"c-{name}", name=name),
        result=ToolResult(success=success, output=output, error=error),
        denied=not success,
        halted=False,
    )


def _write_skill(tmp_path, name, frontmatter, body="# Skill\n"):
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    content = f"---\n{frontmatter}\n---\n\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# Scenario 1: Skill Market — Price convergence after repeated use
# ---------------------------------------------------------------------------


class TestSkillMarketE2E:
    """F1: Skill price converges to true success rate."""

    def test_price_converges_to_success_rate(self):
        """After 50 uses with 80% success, price should be ~0.8."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}
        import random
        random.seed(42)
        for _ in range(50):
            success = random.random() < 0.8
            router.record_outcome("my-skill", success, stats)
        # After 50 uses, price should be roughly 0.8
        assert 0.65 <= stats["my-skill"].price <= 0.9

    def test_price_drops_after_consecutive_failures(self):
        """3 consecutive failures should drop price."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {"my-skill": SkillStats(price=0.8)}
        for _ in range(3):
            router.record_outcome("my-skill", False, stats)
        # With α=0.1: 0.8 → 0.72 → 0.648 → 0.5832
        assert stats["my-skill"].price < 0.65

    def test_price_affects_routing(self):
        """Higher-priced skill wins when base scores are equal."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
            assert len(matches) == 2
            skill_a = next(m for m in matches if m.name == "skill-a")
            skill_b = next(m for m in matches if m.name == "skill-b")
            assert skill_a.score > skill_b.score

    def test_memory_stake_affects_eviction(self):
        """High-stake memories survive eviction, low-stake ones don't."""
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp) / "memory.json")
            # Add 3 notes with different stakes
            id1 = store.add_note(new_note("critical security rule", importance=3))
            id2 = store.add_note(new_note("minor preference", importance=3))
            id3 = store.add_note(new_note("important decision", importance=3))
            store.update_stake(id1, 5.0)   # high stake
            store.update_stake(id2, 0.1)   # low stake
            store.update_stake(id3, 3.0)   # medium stake
            # Evict to 1
            evicted = store.evict(1)
            assert id2 in evicted  # lowest stake evicted
            assert id1 not in evicted  # highest stake survives


# ---------------------------------------------------------------------------
# Scenario 2: Fatigue System — Error accumulation triggers graceful exit
# ---------------------------------------------------------------------------


class TestFatigueSystemE2E:
    """F2: Accumulated errors trigger fatigue gate."""

    def test_fatigue_builds_up_over_multiple_iterations(self):
        """Errors accumulate across iterations within a turn."""
        books = TurnBooks()
        policy = TurnPolicy(TurnPolicyConfig(
            max_iterations=10,
            failed_streak_threshold=5,  # high threshold so fatigue fires first
            stall_iterations=0,  # disable stall
        ))

        # Simulate 4 iterations with errors
        for i in range(4):
            books.note_outcomes([_outcome("bash", success=False, error="permission denied")])
            books.note_iteration(i + 1)
            snapshot = books.snapshot(iteration=i + 1, tokens_spent=0)
            decision = policy.decide_next_iteration(snapshot)
            if i < 3:
                assert decision.action == TurnAction.ITERATE
            else:
                # After 4 severity-3 errors: 4 × 9 = 36 > 30 → fatigue gate
                assert decision.action == TurnAction.NUDGE
                assert "fatigue" in decision.reason

    def test_fatigue_with_mixed_severity(self):
        """Mixed severity errors accumulate correctly."""
        books = TurnBooks()
        # severity-5 (timeout): 25, severity-2 (not found): 4, severity-1: 1
        books.note_outcomes([_outcome("bash", success=False, error="timeout")])
        books.note_outcomes([_outcome("read", success=False, error="file not found")])
        books.note_outcomes([_outcome("edit", success=False, error="unknown")])
        # fatigue = 25 + 4 + 1 = 30 → at threshold
        assert books.fatigue_index == 30.0

        policy = TurnPolicy(TurnPolicyConfig(
            max_iterations=10,
            failed_streak_threshold=5,
            stall_iterations=0,
        ))
        snapshot = books.snapshot(iteration=1, tokens_spent=0)
        decision = policy.decide_next_iteration(snapshot)
        assert decision.action == TurnAction.NUDGE

    def test_fatigue_resets_each_turn(self):
        """New turn starts with zero fatigue."""
        books1 = TurnBooks()
        books1.note_outcomes([_outcome("bash", success=False, error="timeout")])
        assert books1.fatigue_index == 25.0

        # New turn = new TurnBooks
        books2 = TurnBooks()
        assert books2.fatigue_index == 0.0

    def test_fatigue_gate_only_fires_once(self):
        """Fatigue nudge only injected once per turn."""
        policy = TurnPolicy(TurnPolicyConfig(
            max_iterations=10,
            failed_streak_threshold=5,
            stall_iterations=0,
        ))
        snapshot = TurnSnapshot(
            iteration=1,
            fatigue_index=35.0,
            fatigue_nudges=1,  # already nudged
        )
        decision = policy.decide_next_iteration(snapshot)
        # Should NOT fire fatigue again
        assert decision.action == TurnAction.ITERATE


# ---------------------------------------------------------------------------
# Scenario 3: Dendritic Index — Cross-domain skill recommendation
# ---------------------------------------------------------------------------


class TestDendriticIndexE2E:
    """F3: Past successful skill contexts boost future recommendations."""

    def test_cross_domain_recommendation(self):
        """Signature from one domain helps recommend skill in another."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Setup: create a skill
            # `keywords` is indented under `triggers:`. It was not, which made
            # it a *top-level* key: the old regex fallback read it anyway and hid
            # the mistake, so the fixture "worked" against a broken skill.
            _write_skill(tmp_path, "sandbox-fix", (
                "name: sandbox-fix\n"
                "description: Fix sandbox/permission issues\n"
                "triggers:\n"
                "  keywords: [\"sandbox\"]\n"
            ))

            # Record a past successful execution
            idx = DendriticIndex(tmp_path / "dendritic.json")
            idx.record(StructuralSignature(
                tools=frozenset(["bash", "read"]),
                error_keywords=frozenset(["permission", "denied", "sandbox"]),
                task_keywords=frozenset(["fix", "sandbox", "workspace"]),
                skill_name="sandbox-fix",
                timestamp="2024-01-01T00:00:00Z",
            ))

            # New session with similar context but different task
            reg = SkillRegistry(tmp_path)
            router = SOPRouter(reg, threshold=0.0,
                              dendritic_path=tmp_path / "dendritic.json")

            matches = router.route(
                user_input="fix permission issue",
                recent_tools=["bash", "read"],
                task_goal="fix permission denied",
                recent_failures=["permission denied by workspace"],
                turn_number=1,
            )

            assert len(matches) == 1
            assert matches[0].name == "sandbox-fix"
            # Score should include dendritic boost
            # base: keyword "sandbox" not in user_input → 0
            #        task_type: no match → 0
            #        failure_pattern: no match → 0
            # dendritic: tools={bash,read} match, errors match, task partial
            # Jaccard(tools) = 2/2 = 1.0
            # Jaccard(errors) = 2/3 ≈ 0.67 (permission, denied match; workspace doesn't)
            # Jaccard(task) = 2/3 ≈ 0.67
            # (fix matches, permission/denied don't match sandbox/workspace)
            # Combined: 0.4*1.0 + 0.3*0.67 + 0.3*0.67 = 0.4 + 0.2 + 0.2 = 0.8
            # Boost: 0.8 * 2.0 = 1.6
            # Total: 0 + 1.6 = 1.6
            assert matches[0].score > 1.0  # Boosted by dendritic

    def test_dendritic_persists_across_sessions(self):
        """Signatures survive process restart."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dendritic_path = tmp_path / "dendritic.json"

            # Session 1: record signature
            idx1 = DendriticIndex(dendritic_path)
            idx1.record(StructuralSignature(
                tools=frozenset(["bash"]),
                error_keywords=frozenset(["denied"]),
                task_keywords=frozenset(["fix"]),
                skill_name="my-skill",
                timestamp="2024-01-01T00:00:00Z",
            ))
            assert dendritic_path.exists()

            # Session 2: reload and match
            idx2 = DendriticIndex(dendritic_path)
            matches = idx2.match(
                tools={"bash"},
                errors={"permission denied"},
                task_keywords={"fix"},
            )
            assert len(matches) == 1
            assert matches[0][0] == "my-skill"

    def test_dendritic_no_match_for_dissimilar_context(self):
        """Dissimilar context doesn't produce false matches."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            idx = DendriticIndex(tmp_path / "dendritic.json")
            idx.record(StructuralSignature(
                tools=frozenset(["bash", "read"]),
                error_keywords=frozenset(["permission", "denied"]),
                task_keywords=frozenset(["fix", "sandbox"]),
                skill_name="sandbox-fix",
                timestamp="2024-01-01T00:00:00Z",
            ))

            # Completely different context
            matches = idx.match(
                tools={"python", "node", "npm"},
                errors={"syntax error", "import error"},
                task_keywords={"deploy", "kubernetes", "docker"},
            )
            assert len(matches) == 0


# ---------------------------------------------------------------------------
# Scenario 4: All three features working together
# ---------------------------------------------------------------------------


class TestAllFeaturesTogether:
    """F1 + F2 + F3 working in the same turn."""

    def test_full_turn_with_all_features(self):
        """A turn with skill loading, error accumulation, and dendritic matching."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Setup skills
            _write_skill(tmp_path, "debug-skill", (
                "name: debug-skill\n"
                "description: Debug issues\n"
                "triggers:\n"
                "  keywords: [\"debug\"]\n"
                "  failure_patterns: [\"error\"]\n"
            ))

            # Setup dendritic index with past success
            idx = DendriticIndex(tmp_path / "dendritic.json")
            idx.record(StructuralSignature(
                tools=frozenset(["bash", "read"]),
                error_keywords=frozenset(["error", "failed"]),
                task_keywords=frozenset(["debug", "fix"]),
                skill_name="debug-skill",
                timestamp="2024-01-01T00:00:00Z",
            ))

            # Setup router
            reg = SkillRegistry(tmp_path)
            router = SOPRouter(reg, threshold=0.0,
                              dendritic_path=tmp_path / "dendritic.json")

            # Route with matching context
            stats = {"debug-skill": SkillStats(price=0.8)}
            matches = router.route(
                user_input="debug this error",
                recent_tools=["bash", "read"],
                task_goal="debug the issue",
                recent_failures=["something failed"],
                turn_number=1,
                stats=stats,
            )

            assert len(matches) == 1
            skill = matches[0]
            # base: keyword "debug" = 3.0, failure_pattern "error" matches "failed"? No.
            # Actually failure_pattern is "error" and the failure is "something failed"
            # "error" in "something failed" → No
            # So base = 3.0 (keyword only)
            # price boost: 3.0 * (0.5 + 0.8) = 3.0 * 1.3 = 3.9
            # dendritic boost: tools match, errors partial, task match
            # Jaccard(tools) = 2/2 = 1.0
            # Jaccard(errors) = {something, failed} vs {error, failed} = 1/3 ≈ 0.33
            # Jaccard(task) = {debug, the, issue} vs {debug, fix} = 1/4 = 0.25
            # Combined: 0.4*1.0 + 0.3*0.33 + 0.3*0.25 = 0.4 + 0.1 + 0.075 = 0.575
            # Boost: 0.575 * 2.0 = 1.15
            # Total: 3.9 + 1.15 = 5.05
            assert skill.score > 3.0  # Boosted by both price and dendritic

    def test_fatigue_prevents_wasted_iterations(self):
        """Fatigue gate stops a turn that's accumulating errors."""
        books = TurnBooks()
        policy = TurnPolicy(TurnPolicyConfig(
            max_iterations=20,
            failed_streak_threshold=10,  # very high
            stall_iterations=0,  # disable
        ))

        # Simulate a turn with accumulating errors
        for i in range(5):
            books.note_outcomes([_outcome("bash", success=False, error="permission denied")])
            books.note_iteration(i + 1)
            snapshot = books.snapshot(iteration=i + 1, tokens_spent=0)
            decision = policy.decide_next_iteration(snapshot)

            if i < 3:
                # fatigue = (i+1) * 9 = 9, 18, 27 → below 30
                assert decision.action == TurnAction.ITERATE
            else:
                # fatigue = 36, 45 → above 30 → fatigue gate
                assert decision.action == TurnAction.NUDGE
                break

    def test_skill_price_updates_after_turn(self):
        """Skill price is recorded after turn completion."""
        router = SOPRouter(SkillRegistry(Path("/nonexistent")))
        stats = {}

        # Simulate successful skill use
        router.record_outcome("my-skill", True, stats)
        assert stats["my-skill"].price > 0.5

        # Simulate failed skill use
        router.record_outcome("my-skill", False, stats)
        assert stats["my-skill"].price < 0.6
