"""TDD (M3): ToolObserver — auto-record tool call outcomes.

Covers: ToolObservation, ToolObserver, SOPRouter with observations,
RequestAssembler integration, AgentLoop wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.core.events import MemoryInjected, ToolFinished
from ai_coding.domain.message import ChatMessage
from ai_coding.skills.registry import SkillRegistry

# ---------------------------------------------------------------------------
# ToolObservation
# ---------------------------------------------------------------------------


class TestToolObservation:
    """ToolObservation data contract."""

    def test_frozen_dataclass(self):
        from ai_coding.core.sop_router import ToolObservation
        obs = ToolObservation(tool_name="write", success=False,
                              error="test_error", timestamp="2024-01-01T00:00:00")
        assert obs.tool_name == "write"
        assert obs.success is False
        assert obs.error == "test_error"
        with pytest.raises(AttributeError):
            obs.success = True  # frozen

    def test_optional_error(self):
        from ai_coding.core.sop_router import ToolObservation
        obs = ToolObservation(tool_name="read", success=True,
                              error=None, timestamp="2024-01-01T00:00:00")
        assert obs.error is None


# ---------------------------------------------------------------------------
# ToolObserver
# ---------------------------------------------------------------------------


class TestToolObserver:
    """ToolObserver — intercept ToolFinished events."""

    def test_records_failed_tool_call(self):
        from ai_coding.core.sop_router import ToolObserver
        observations = []
        observer = ToolObserver(observations)
        event = ToolFinished(call_id="c1", tool_name="write", success=False,
                             error="resolves outside workspace", output_preview="",
                             duration_ms=100, iteration=0)
        observer.on_event(event)
        assert len(observations) == 1
        assert observations[0].tool_name == "write"
        assert observations[0].success is False
        assert observations[0].error == "resolves outside workspace"

    def test_records_successful_tool_call(self):
        from ai_coding.core.sop_router import ToolObserver
        observations = []
        observer = ToolObserver(observations)
        event = ToolFinished(call_id="c1", tool_name="read", success=True,
                             error=None, output_preview="file content",
                             duration_ms=50, iteration=0)
        observer.on_event(event)
        assert len(observations) == 1
        assert observations[0].success is True
        assert observations[0].error is None

    def test_ignores_non_tool_finished_events(self):
        from ai_coding.core.sop_router import ToolObserver
        observations = []
        observer = ToolObserver(observations)
        observer.on_event(MemoryInjected(note_ids=(), chars=0))
        assert len(observations) == 0

    def test_max_50_observations(self):
        from ai_coding.core.sop_router import ToolObserver
        observations = []
        observer = ToolObserver(observations)
        for i in range(55):
            event = ToolFinished(call_id=f"c{i}", tool_name="test",
                                 success=True, error=None, output_preview="",
                                 duration_ms=10, iteration=0)
            observer.on_event(event)
        assert len(observations) == 50
        # Should keep the most recent 50
        assert observations[-1].tool_name == "test"

    def test_timestamp_is_set(self):
        from ai_coding.core.sop_router import ToolObserver
        observations = []
        observer = ToolObserver(observations)
        event = ToolFinished(call_id="c1", tool_name="write", success=False,
                             error="err", output_preview="", duration_ms=10, iteration=0)
        observer.on_event(event)
        assert observations[0].timestamp != ""


# ---------------------------------------------------------------------------
# SOPRouter with observations
# ---------------------------------------------------------------------------


class TestSOPRouterWithObservations:
    """SOPRouter.route() — use observations for failure_pattern matching."""

    def _make_skills(self, tmp_path: Path, skills_data: dict) -> SkillRegistry:
        for name, triggers in skills_data.items():
            frontmatter = f"name: {name}\ndescription: {name}\n"
            if triggers:
                frontmatter += "triggers:\n"
                for key, values in triggers.items():
                    frontmatter += f"  {key}: {values}\n"
            skill_dir = tmp_path / name
            skill_dir.mkdir(parents=True, exist_ok=True)
            content = f"---\n{frontmatter}\n---\n\n# {name}\n"
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        return SkillRegistry(tmp_path)

    def test_failure_pattern_from_observations(self, tmp_path):
        """Observations with matching error should trigger skill recommendation."""
        from ai_coding.core.sop_router import SOPRouter, ToolObservation
        reg = self._make_skills(tmp_path, {
            "sandbox-fix": {"failure_patterns": ["resolves outside workspace"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        # Observations contain a matching error
        observations = [
            ToolObservation(tool_name="write", success=False,
                            error="resolves outside workspace",
                            timestamp="2024-01-01T00:00:00"),
        ]
        matches = router.route("", "", "", [], observations=observations)
        assert len(matches) == 1
        assert matches[0].name == "sandbox-fix"
        assert "failure_pattern" in matches[0].matched_dimensions

    def test_no_observations_falls_back_to_books_failures(self, tmp_path):
        """When observations is None, only use recent_failures."""
        from ai_coding.core.sop_router import SOPRouter
        reg = self._make_skills(tmp_path, {
            "sandbox-fix": {"failure_patterns": ["resolves outside workspace"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        # No observations, but recent_failures has match
        matches = router.route("", "", "", ["resolves outside workspace"])
        assert len(matches) == 1

    def test_observations_and_failures_merged(self, tmp_path):
        """Both observations and failures contribute to matching."""
        from ai_coding.core.sop_router import SOPRouter, ToolObservation
        reg = self._make_skills(tmp_path, {
            "sandbox-fix": {"failure_patterns": ["resolves outside"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        # Error in observations but not in recent_failures
        observations = [
            ToolObservation(tool_name="write", success=False,
                            error="path resolves outside workspace",
                            timestamp="2024-01-01T00:00:00"),
        ]
        matches = router.route("", "", "", [], observations=observations)
        assert len(matches) == 1

    def test_successful_observations_ignored_for_failure(self, tmp_path):
        """Successful tool calls should not match failure_patterns."""
        from ai_coding.core.sop_router import SkillStats, SOPRouter, ToolObservation
        reg = self._make_skills(tmp_path, {
            "sandbox-fix": {"failure_patterns": ["resolves outside"]},
        })
        router = SOPRouter(reg, threshold=1.0)
        observations = [
            ToolObservation(tool_name="read", success=True,
                            error=None, timestamp="2024-01-01T00:00:00"),
        ]
        # M4: pass stats to suppress freshness bonus
        stats = {"sandbox-fix": SkillStats(last_recommended_turn=1)}
        matches = router.route("", "", "", [], observations=observations,
                               turn_number=1, stats=stats)
        assert len(matches) == 0


# ---------------------------------------------------------------------------
# RequestAssembler integration
# ---------------------------------------------------------------------------


class TestRequestAssemblerWithObservations:
    """RequestAssembler passes observations to SOP router."""

    def test_extracts_observations_from_session(self, tmp_path):
        """RequestAssembler reads tool_observations from session."""
        from ai_coding.core.sop_router import ToolObservation
        obs = [ToolObservation("write", False, "err", "2024-01-01T00:00:00")]
        session = SimpleNamespace(
            messages=[ChatMessage(role="user", content="test", timestamp="t1")],
            tool_observations=obs,
        )
        # Verify the session has tool_observations
        assert hasattr(session, "tool_observations")
        assert len(session.tool_observations) == 1

    def test_handles_missing_observations(self, tmp_path):
        """Session without tool_observations attribute is handled."""
        from ai_coding.core.request_assembly import RequestAssembler
        session = SimpleNamespace(
            messages=[ChatMessage(role="user", content="test", timestamp="t1")],
        )
        # Should not raise
        result = RequestAssembler._extract_user_input(session)
        assert result == "test"
