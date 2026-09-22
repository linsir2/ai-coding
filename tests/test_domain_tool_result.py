"""TDD (R1a-1): structured tool results + the <tool_output> envelope."""

import json

from ai_coding.domain.message import ToolArtifact, ToolOutput, ToolResult
from ai_coding.domain.run import tool_message, wrap_tool_output


def test_tool_result_defaults_keep_old_shape():
    """The M0 form must stay valid: ToolResult(success, output)."""
    r = ToolResult(True, "x")
    assert r.success is True and r.output == "x"
    assert r.data is None
    assert r.artifacts == ()
    assert r.error is None


def test_tool_result_carries_data_and_artifacts():
    art = ToolArtifact(path="a.py", bytes=12, description="created")
    r = ToolResult(success=True, output="ok", data={"n": 1}, artifacts=(art,))
    assert r.data == {"n": 1}
    assert r.artifacts[0].path == "a.py"


def test_tool_artifact_to_dict_from_dict_roundtrip():
    art = ToolArtifact(path="a.py", kind="file", bytes=3, description="d")
    restored = ToolArtifact.from_dict(art.to_dict())
    assert restored == art
    minimal = ToolArtifact.from_dict({"path": "b"})
    assert minimal.kind == "file" and minimal.bytes is None


def test_wrap_tool_output_omits_data_block_by_default():
    """Legacy shape must be byte-identical — this is what keeps R1a incremental."""
    assert wrap_tool_output("read", "body") == '<tool_output tool="read">\nbody\n</tool_output>'


def test_wrap_tool_output_includes_data_block_when_present():
    out = wrap_tool_output("glob", "", data={"matches": ["a.py"]})
    assert out.startswith('<tool_output tool="glob">\n<data>\n')
    assert json.loads(out.split("<data>\n", 1)[1].split("\n</data>", 1)[0]) == {
        "matches": ["a.py"]
    }


def test_wrap_tool_output_data_then_text():
    out = wrap_tool_output("read", "line 1", data={"lines": 1})
    assert out.index("</data>") < out.index("line 1")


def test_wrap_tool_output_marks_failure_with_ok_false():
    out = wrap_tool_output("bash", "boom", success=False)
    assert out == '<tool_output tool="bash" ok="false">\nboom\n</tool_output>'


def test_tool_output_carries_structured_payload():
    out = ToolOutput(call_id="c1", tool_name="glob", output="", data={"total": 0})
    assert out.success is True
    assert out.data == {"total": 0}


def test_tool_msg_envelope_contains_data_block():
    out = ToolOutput(
        call_id="c1",
        tool_name="glob",
        output="2 matches",
        data={"total": 2},
        artifacts=(ToolArtifact(path="a.py"),),
    )
    msg = tool_message(out, "2026-01-01T00:00:00Z")
    assert msg.role == "tool"
    assert msg.tool_call_id == "c1"
    assert "<data>" in (msg.content or "")
    assert "2 matches" in (msg.content or "")


def test_tool_msg_envelope_unchanged_without_data():
    out = ToolOutput(call_id="c1", tool_name="read", output="body")
    msg = tool_message(out, "ts")
    assert msg.content == '<tool_output tool="read">\nbody\n</tool_output>'
