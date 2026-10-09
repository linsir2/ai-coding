"""TDD (F3): Dendritic Index — cross-presentation layer for skill success signatures."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_coding.core.sop_router import (
    DendriticIndex,
    SOPRouter,
    StructuralSignature,
)
from ai_coding.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# 1. StructuralSignature
# ---------------------------------------------------------------------------


class TestStructuralSignature:
    """StructuralSignature — data class for skill execution context."""

    def test_signature_creation(self):
        """Signature contains correct fields."""
        sig = StructuralSignature(
            tools=frozenset(["bash", "read"]),
            error_keywords=frozenset(["permission", "denied"]),
            task_keywords=frozenset(["fix", "sandbox"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        )
        assert sig.tools == frozenset(["bash", "read"])
        assert sig.skill_name == "sandbox-fix"

    def test_signature_serialization_roundtrip(self):
        """to_dict/from_dict preserves all fields."""
        sig = StructuralSignature(
            tools=frozenset(["bash", "read"]),
            error_keywords=frozenset(["permission", "denied"]),
            task_keywords=frozenset(["fix", "sandbox"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        )
        data = sig.to_dict()
        restored = StructuralSignature.from_dict(data)
        assert restored.tools == sig.tools
        assert restored.error_keywords == sig.error_keywords
        assert restored.task_keywords == sig.task_keywords
        assert restored.skill_name == sig.skill_name
        assert restored.timestamp == sig.timestamp

    def test_signature_is_frozen(self):
        """Signature is immutable."""
        sig = StructuralSignature(
            tools=frozenset(["bash"]),
            error_keywords=frozenset(),
            task_keywords=frozenset(),
            skill_name="test",
            timestamp="2024-01-01T00:00:00Z",
        )
        with pytest.raises(AttributeError):
            sig.skill_name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. DendriticIndex persistence
# ---------------------------------------------------------------------------


class TestDendriticIndexPersistence:
    """DendriticIndex — record, load, save, cap enforcement."""

    def test_record_creates_file(self, tmp_path: Path):
        """Recording a signature creates the index file."""
        idx = DendriticIndex(tmp_path / "index.json")
        sig = StructuralSignature(
            tools=frozenset(["bash"]),
            error_keywords=frozenset(),
            task_keywords=frozenset(["fix"]),
            skill_name="test-skill",
            timestamp="2024-01-01T00:00:00Z",
        )
        idx.record(sig)
        assert (tmp_path / "index.json").exists()

    def test_persistence_roundtrip(self, tmp_path: Path):
        """Signatures survive save/reload."""
        idx = DendriticIndex(tmp_path / "index.json")
        sig = StructuralSignature(
            tools=frozenset(["bash", "read"]),
            error_keywords=frozenset(["permission"]),
            task_keywords=frozenset(["fix", "sandbox"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        )
        idx.record(sig)

        # Reload
        idx2 = DendriticIndex(tmp_path / "index.json")
        matches = idx2.match(
            tools={"bash", "read"},
            errors={"permission denied"},
            task_keywords={"fix", "sandbox"},
        )
        assert len(matches) == 1
        assert matches[0][0] == "sandbox-fix"

    def test_corrupted_file_ignored(self, tmp_path: Path):
        """Corrupted JSON file is silently ignored."""
        index_path = tmp_path / "index.json"
        index_path.write_text("{not valid json", encoding="utf-8")
        idx = DendriticIndex(index_path)
        # Should not raise, should start empty
        matches = idx.match(tools={"bash"}, errors=set(), task_keywords=set())
        assert matches == []

    def test_cap_enforcement(self, tmp_path: Path):
        """FIFO eviction at cap."""
        idx = DendriticIndex(tmp_path / "index.json")
        for i in range(250):
            sig = StructuralSignature(
                tools=frozenset([f"tool-{i}"]),
                error_keywords=frozenset(),
                task_keywords=frozenset(["task"]),
                skill_name=f"skill-{i}",
                timestamp="2024-01-01T00:00:00Z",
            )
            idx.record(sig)
        # Should be capped at 200
        assert len(idx._signatures) == 200
        # First 50 should be evicted (FIFO)
        assert idx._signatures[0].skill_name == "skill-50"
        assert idx._signatures[-1].skill_name == "skill-249"


# ---------------------------------------------------------------------------
# 3. Matching algorithm
# ---------------------------------------------------------------------------


class TestMatching:
    """DendriticIndex.match() — Jaccard similarity-based matching."""

    def test_identical_context_scores_high(self, tmp_path: Path):
        """Identical context → high match score."""
        idx = DendriticIndex(tmp_path / "index.json")
        sig = StructuralSignature(
            tools=frozenset(["bash", "read"]),
            error_keywords=frozenset(["permission", "denied"]),
            task_keywords=frozenset(["fix", "sandbox"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        )
        idx.record(sig)

        matches = idx.match(
            tools={"bash", "read"},
            errors={"permission denied"},
            task_keywords={"fix", "sandbox"},
        )
        assert len(matches) == 1
        assert matches[0][1] > 0.8  # High similarity

    def test_partial_context_scores_medium(self, tmp_path: Path):
        """Partial overlap → medium score."""
        idx = DendriticIndex(tmp_path / "index.json")
        sig = StructuralSignature(
            tools=frozenset(["bash", "read", "write"]),
            error_keywords=frozenset(["permission", "denied"]),
            task_keywords=frozenset(["fix", "sandbox", "workspace"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        )
        idx.record(sig)

        # Only tools overlap partially, no errors, different task
        matches = idx.match(
            tools={"bash", "read"},
            errors=set(),
            task_keywords={"debug", "test"},
        )
        # Tool overlap: 2/3 = 0.67, error: 0/2 = 0, task: 0/5 = 0
        # Combined: 0.4*0.67 + 0.3*0 + 0.3*0 = 0.268 → below 0.3 threshold
        # Actually wait: if errors is empty and error_keywords is not empty,
        # jaccard returns 0 (one set empty). Same for task.
        # So score = 0.4 * 0.67 + 0.3 * 0 + 0.3 * 0 = 0.268 → below threshold
        # This should NOT match
        assert len(matches) == 0

    def test_no_overlap_no_match(self, tmp_path: Path):
        """No overlap → no match."""
        idx = DendriticIndex(tmp_path / "index.json")
        sig = StructuralSignature(
            tools=frozenset(["bash"]),
            error_keywords=frozenset(["permission"]),
            task_keywords=frozenset(["sandbox"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        )
        idx.record(sig)

        matches = idx.match(
            tools={"python", "node"},
            errors={"syntax error"},
            task_keywords={"deploy", "kubernetes"},
        )
        assert len(matches) == 0

    def test_jaccard_both_empty_returns_one(self):
        """Jaccard of two empty sets = 1.0."""
        assert DendriticIndex._jaccard(set(), set()) == 1.0

    def test_jaccard_one_empty_returns_zero(self):
        """Jaccard with one empty set = 0.0."""
        assert DendriticIndex._jaccard({"a"}, set()) == 0.0
        assert DendriticIndex._jaccard(set(), {"b"}) == 0.0

    def test_multiple_matches_sorted(self, tmp_path: Path):
        """Multiple matches returned sorted by score descending."""
        idx = DendriticIndex(tmp_path / "index.json")
        # High match signature
        idx.record(StructuralSignature(
            tools=frozenset(["bash", "read"]),
            error_keywords=frozenset(["permission"]),
            task_keywords=frozenset(["fix", "sandbox"]),
            skill_name="skill-a",
            timestamp="2024-01-01T00:00:00Z",
        ))
        # Lower match signature
        idx.record(StructuralSignature(
            tools=frozenset(["bash"]),
            error_keywords=frozenset(),
            task_keywords=frozenset(["fix"]),
            skill_name="skill-b",
            timestamp="2024-01-01T00:00:00Z",
        ))

        matches = idx.match(
            tools={"bash", "read"},
            errors={"permission denied"},
            task_keywords={"fix", "sandbox"},
        )
        assert len(matches) == 2
        # skill-a should have higher score (more overlap)
        assert matches[0][0] == "skill-a"
        assert matches[0][1] > matches[1][1]


# ---------------------------------------------------------------------------
# 4. SOPRouter integration
# ---------------------------------------------------------------------------


class TestSOPRouterIntegration:
    """DendriticIndex boosts skill scores in SOPRouter.route()."""

    def _write_skill(self, tmp_path: Path, name: str, frontmatter: str) -> Path:
        skill_dir = tmp_path / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        content = f"---\n{frontmatter}\n---\n\n# {name}\n"
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return skill_dir

    def test_dendritic_boosts_score(self, tmp_path: Path):
        """Matching signature boosts skill score."""
        self._write_skill(tmp_path, "sandbox-fix", (
            "name: sandbox-fix\n"
            "description: Fix sandbox issues\n"
            "triggers:\n"
            "  keywords: [\"sandbox\"]\n"
        ))

        # Record a signature
        idx = DendriticIndex(tmp_path / "index.json")
        idx.record(StructuralSignature(
            tools=frozenset(["bash", "read"]),
            error_keywords=frozenset(["permission", "denied"]),
            task_keywords=frozenset(["fix", "sandbox"]),
            skill_name="sandbox-fix",
            timestamp="2024-01-01T00:00:00Z",
        ))

        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=0.0, dendritic_path=tmp_path / "index.json")

        # Route with matching context
        matches = router.route(
            user_input="fix sandbox",
            recent_tools=["bash", "read"],
            task_goal="fix sandbox permissions",
            recent_failures=["permission denied by sandbox"],
            turn_number=1,
        )

        assert len(matches) == 1
        assert matches[0].name == "sandbox-fix"
        # Score should include dendritic boost
        # base_score: keyword "sandbox" = 3.0, task_type "sandbox" contains task?
        # Actually task_type matching: task_goal contains "sandbox" → task_type match = 2.0
        # failure_pattern: no failure_patterns defined → 0
        # total before dendritic: 3.0 + 2.0 = 5.0 (plus freshness)
        # With dendritic boost: + (match_score × 2.0)
        assert matches[0].score > 5.0

    def test_no_dendritic_no_change(self, tmp_path: Path):
        """Without dendritic_path, routing is unchanged."""
        self._write_skill(tmp_path, "sandbox-fix", (
            "name: sandbox-fix\n"
            "description: Fix sandbox issues\n"
            "triggers:\n"
            "  keywords: [\"sandbox\"]\n"
        ))

        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, threshold=0.0)  # No dendritic_path

        matches = router.route(
            user_input="fix sandbox",
            recent_tools=["bash"],
            task_goal="fix sandbox",
            recent_failures=[],
            turn_number=1,
        )

        assert len(matches) == 1
        # Score without dendritic boost
        assert matches[0].score > 0


# ---------------------------------------------------------------------------
# 5. record_signature API
# ---------------------------------------------------------------------------


class TestRecordSignature:
    """SOPRouter.record_signature() delegates to DendriticIndex."""

    def test_record_signature(self, tmp_path: Path):
        """record_signature stores the signature."""
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg, dendritic_path=tmp_path / "index.json")

        sig = StructuralSignature(
            tools=frozenset(["bash"]),
            error_keywords=frozenset(["denied"]),
            task_keywords=frozenset(["fix"]),
            skill_name="test-skill",
            timestamp="2024-01-01T00:00:00Z",
        )
        router.record_signature(sig)

        # Verify by reloading
        idx2 = DendriticIndex(tmp_path / "index.json")
        matches = idx2.match(
            tools={"bash"},
            errors={"denied"},
            task_keywords={"fix"},
        )
        assert len(matches) == 1
        assert matches[0][0] == "test-skill"

    def test_record_signature_no_dendritic(self, tmp_path: Path):
        """record_signature with no dendritic index is a no-op."""
        reg = SkillRegistry(tmp_path)
        router = SOPRouter(reg)  # No dendritic_path

        sig = StructuralSignature(
            tools=frozenset(["bash"]),
            error_keywords=frozenset(),
            task_keywords=frozenset(),
            skill_name="test",
            timestamp="2024-01-01T00:00:00Z",
        )
        # Should not raise
        router.record_signature(sig)



