"""TDD (r17/T2): grep, ripgrep-first with a pure-Python fallback.

The machine this runs on has no ``rg`` (checked), so the fallback is not a
courtesy path -- it is the path a fresh install takes.  Both are pinned: the
fallback against real files, the rg path against an injected runner (the same
seam production passes ``run_process_async`` through).
"""

from __future__ import annotations

import pytest

from ai_coding.infra.subprocess_runner import ProcessOutcome
from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.grep_tool import GrepTool


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "notes.md").write_text("# not code\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.py").write_text("target\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("target\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_python_fallback_finds_matches_and_skips_ignored_dirs(ws):
    tool = GrepTool(Sandbox(ws), use_ripgrep=False)

    result = await tool.execute({"pattern": "target"})

    assert result.success
    assert "pkg/mod.py:1: def target():" in result.output
    assert ".git/ignored.py" not in result.output
    assert "node_modules/dep.js" not in result.output


@pytest.mark.asyncio
async def test_the_rg_path_is_taken_when_available(ws):
    calls: list[tuple[str, ...]] = []

    async def runner(argv, *, cwd, timeout):
        calls.append(tuple(argv))
        return ProcessOutcome(
            returncode=0,
            stdout=(
                '{"type":"match","data":{"path":{"text":"pkg/mod.py"},'
                '"line_number":1,"lines":{"text":"def target():\\n"}}}\n'
            ),
        )

    tool = GrepTool(Sandbox(ws), use_ripgrep=True, runner=runner, rg_path="/usr/bin/rg")

    result = await tool.execute({"pattern": "target"})

    assert calls and calls[0][0] == "/usr/bin/rg" and "--json" in calls[0]
    assert "pkg/mod.py:1: def target():" in result.output


@pytest.mark.asyncio
async def test_include_literal_and_case_sensitivity(ws):
    tool = GrepTool(Sandbox(ws), use_ripgrep=False)

    by_include = await tool.execute({"pattern": "code", "include": "*.md"})
    assert "pkg/notes.md" in by_include.output and "pkg/mod.py" not in by_include.output

    literal = await tool.execute({"pattern": "def target():", "literal": True})
    assert "pkg/mod.py:1" in literal.output

    sensitive = await tool.execute({"pattern": "TARGET"})
    assert sensitive.output.strip() == "(no matches)", "case-sensitive by default"


@pytest.mark.asyncio
async def test_the_limit_is_reported_when_hit(ws):
    for n in range(5):
        (ws / f"f{n}.txt").write_text("needle\n", encoding="utf-8")
    tool = GrepTool(Sandbox(ws), use_ripgrep=False, max_matches=3)

    result = await tool.execute({"pattern": "needle"})

    assert result.output.count(":1: needle") == 3
    assert "showing 3" in result.output


@pytest.mark.asyncio
async def test_an_escaping_path_is_refused(ws):
    tool = GrepTool(Sandbox(ws), use_ripgrep=False)

    result = await tool.execute({"pattern": "x", "path": "../outside"})

    assert result.success is False
