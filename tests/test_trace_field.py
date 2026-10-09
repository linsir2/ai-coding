"""TDD (F4): Sub-agent trace field + uncertainty protocol."""

from __future__ import annotations

from ai_coding.domain.subagent import SubagentResult

# ---------------------------------------------------------------------------
# SubagentResult new fields
# ---------------------------------------------------------------------------


class TestSubagentResultFields:
    """SubagentResult should have notes and confidence fields."""

    def test_default_values(self):
        result = SubagentResult()
        assert result.notes == ""
        assert result.confidence == "high"

    def test_custom_values(self):
        result = SubagentResult(
            status="completed",
            notes="Bash heredoc is the fix for sandbox issues",
            confidence="medium",
        )
        assert result.notes == "Bash heredoc is the fix for sandbox issues"
        assert result.confidence == "medium"

    def test_to_dict_includes_new_fields(self):
        result = SubagentResult(notes="test note", confidence="low")
        data = result.to_dict()
        assert data["notes"] == "test note"
        assert data["confidence"] == "low"


# ---------------------------------------------------------------------------
# Trace field file
# ---------------------------------------------------------------------------


class TestTraceField:
    """Sub-agent trace notes are written to a shared file."""

    def test_trace_file_written(self, tmp_path):
        from ai_coding.core.subagent import _write_trace_note
        result = SubagentResult(
            status="completed",
            notes="Use bash heredoc for sandbox issues",
            ledger_path=str(tmp_path / "test.json"),
        )
        _write_trace_note(result, tmp_path)
        trace_file = tmp_path / "transcripts" / "skill-traces" / "trace-field.md"
        assert trace_file.exists()
        content = trace_file.read_text()
        assert "bash heredoc" in content
        assert "[completed]" in content

    def test_no_write_when_no_notes(self, tmp_path):
        from ai_coding.core.subagent import _write_trace_note
        result = SubagentResult(status="completed", notes="")
        _write_trace_note(result, tmp_path)
        trace_file = tmp_path / "transcripts" / "skill-traces" / "trace-field.md"
        assert not trace_file.exists()

    def test_appends_multiple_notes(self, tmp_path):
        from ai_coding.core.subagent import _write_trace_note
        for i in range(3):
            result = SubagentResult(
                status="completed",
                notes=f"Note {i}",
                ledger_path=str(tmp_path / f"test-{i}.json"),
            )
            _write_trace_note(result, tmp_path)
        trace_file = tmp_path / "transcripts" / "skill-traces" / "trace-field.md"
        content = trace_file.read_text()
        assert "Note 0" in content
        assert "Note 1" in content
        assert "Note 2" in content


# ---------------------------------------------------------------------------
# Uncertainty protocol
# ---------------------------------------------------------------------------


class TestUncertaintyProtocol:
    """Sub-agent system prompt should include uncertainty protocol."""

    def test_protocol_text_exists(self, tmp_path):
        """The uncertainty protocol constant should exist in the module."""
        from ai_coding.core.subagent import _UNCERTAINTY_PROTOCOL
        assert "不确定协议" in _UNCERTAINTY_PROTOCOL
        assert "置信度" in _UNCERTAINTY_PROTOCOL
        assert "给后来者的笔记" in _UNCERTAINTY_PROTOCOL


# ---------------------------------------------------------------------------
# Integration: trace field in session context
# ---------------------------------------------------------------------------


class TestTraceFieldIntegration:
    """Trace field content should be readable for skill recommendations."""

    def test_trace_content_readable(self, tmp_path):
        """Trace notes should be readable for future skill routing."""
        trace_dir = tmp_path / "transcripts" / "skill-traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_file = trace_dir / "trace-field.md"
        trace_file.write_text(
            "\n---\n[completed] test.json\n"
            "When write fails with 'resolves outside workspace', use bash heredoc\n"
            "\n---\n[timeout] test2.json\n"
            "Task took too long, consider breaking into smaller steps\n"
        )
        content = trace_file.read_text()
        assert "bash heredoc" in content
        assert "breaking into smaller steps" in content
