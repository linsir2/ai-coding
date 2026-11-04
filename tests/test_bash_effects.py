"""bash effects: candidate paths + per-call stat, not a whole-workspace scan."""

from __future__ import annotations

import pytest

from ai_coding.domain.effects import EffectLedger
from ai_coding.shell_parse import effect_path_candidates
from ai_coding.tools.base import ToolContext
from ai_coding.tools.bash_tool import BashTool


def test_effect_candidates_include_relative_redirections_and_sed_targets(tmp_path):
    paths = effect_path_candidates(
        "printf x > a.py; sed -i s/x/y/ src/b.tsx; cat >> 'docs/c.md'",
        workspace=str(tmp_path),
    )

    assert str(tmp_path / "a.py") in paths
    assert str(tmp_path / "src" / "b.tsx") in paths
    assert str(tmp_path / "docs" / "c.md") in paths


def test_effect_candidates_ignore_plain_arguments_and_options(tmp_path):
    assert effect_path_candidates("ls -la; echo hello", workspace=str(tmp_path)) == []


@pytest.mark.asyncio
async def test_a_foreground_bash_write_records_an_effect(tmp_path):
    seen = []
    ledger = EffectLedger()
    tool = BashTool(timeout_seconds=5)

    result = await tool.execute(
        {"command": "printf 'hello\\n' > a.py"},
        ToolContext(
            workspace=str(tmp_path),
            effects=lambda: ledger,
            note_effect=lambda *row, **kw: seen.append((row, kw)),
            call_id="c1",
        ),
    )

    assert result.success is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "hello\n"
    assert seen
    row, kw = seen[0]
    assert row[0] == "a.py"
    assert row[2:] == ("bash", "write")
    assert kw["call_id"] == "c1"


@pytest.mark.asyncio
async def test_a_failed_bash_command_still_records_the_file_it_changed(tmp_path):
    seen = []
    tool = BashTool(timeout_seconds=5)

    result = await tool.execute(
        {"command": "printf 'hello\\n' > a.py; false"},
        ToolContext(
            workspace=str(tmp_path),
            effects=lambda: EffectLedger(),
            note_effect=lambda *row, **kw: seen.append((row, kw)),
            call_id="c1",
        ),
    )

    assert result.success is False
    assert (tmp_path / "a.py").exists()
    assert seen


@pytest.mark.asyncio
async def test_a_read_only_bash_command_records_no_effect(tmp_path):
    (tmp_path / "a.py").write_text("hello\n", encoding="utf-8")
    seen = []
    tool = BashTool(timeout_seconds=5)

    result = await tool.execute(
        {"command": "ls a.py"},
        ToolContext(
            workspace=str(tmp_path),
            effects=lambda: EffectLedger(),
            note_effect=lambda *row, **kw: seen.append((row, kw)),
            call_id="c1",
        ),
    )

    assert result.success is True
    assert seen == []


def test_extensionless_redirect_and_writer_paths_are_candidates(tmp_path):
    paths = effect_path_candidates(
        "printf x > Makefile; touch README; cp a b",
        workspace=str(tmp_path),
    )

    assert str(tmp_path / "Makefile") in paths
    assert str(tmp_path / "README") in paths
    assert str(tmp_path / "b") in paths
