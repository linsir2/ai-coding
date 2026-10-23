"""r17 regression: credential globs must not resurrect gitignored files.

Root cause (C1): ``credential_exclude_globs`` started with ``**/*``.  Under
rg 15.1.0 last-match-wins, that bare include re-opens everything gitignore
had hidden (``x.log`` reappears).  The fix is pure-``!`` exclusion rules;
this test pins both runners to the same answer on a gitignored tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.grep_tool import GrepTool


def _tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("target\n", encoding="utf-8")
    (root / "x.log").write_text("target\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_gitignored_log_stays_excluded_with_credential_globs(tmp_path):
    """``*.log`` ignored by git stays out even when credential globs apply."""
    root = _tree(tmp_path / "ws")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    for tool in (
        GrepTool(Sandbox(root), use_ripgrep=False),
        GrepTool(Sandbox(root), use_ripgrep=True),
    ):
        result = await tool.execute({"pattern": "target"})
        assert result.success, result.output
        assert "x.log" not in result.output, (
            f"gitignored file resurrected by {tool!r}: {result.output!r}"
        )
        assert "a.py:1:" in result.output
