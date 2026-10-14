"""Parity and single-truth-source tests for r18/F10+F15.

Two things are being pinned here, and both are about *agreement* rather than
about one function's behaviour:

1. ``core/workspace_files`` is the only place that decides what exists in a
   workspace -- which directories are skipped, which files ``.gitignore``
   hides.  grep's Python fallback and the composer's ``files.find`` both walk
   through it, because two hand-written copies of the same list is exactly how
   they came to disagree.

2. grep answers the same question the same way whether or not ``rg`` is
   installed.  The expected values in these tests were measured against
   ripgrep 15.1.0, not inferred from its documentation.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_coding.security.sandbox import Sandbox
from ai_coding.tools.grep_tool import GrepTool


def _tree(root: Path) -> Path:
    """A workspace whose shape exercises every rule under test."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("target\n", encoding="utf-8")
    (root / "x.log").write_text("target\n", encoding="utf-8")
    (root / "notes.txt").write_text("target\n", encoding="utf-8")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "c.txt").write_text("target\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("target\n", encoding="utf-8")
    (root / "deep" / "nested").mkdir(parents=True, exist_ok=True)
    (root / "deep" / "nested" / "x.py").write_text("target\n", encoding="utf-8")
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules" / "dep.js").write_text("target\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    return root


def _fallback(root: Path) -> GrepTool:
    return GrepTool(Sandbox(root), use_ripgrep=False)


@pytest.mark.asyncio
async def _matches(tool: GrepTool, **params) -> set[str]:
    result = await tool.execute({"pattern": "target", **params})
    assert result.success, result.output
    if result.output.startswith("(no matches)"):
        return set()
    return {
        line.rsplit(":", 2)[0]
        for line in result.output.splitlines()
        if line and not line.startswith("...")
    }


# ------------------------------------------------- one home for the file list


def test_the_ignore_list_has_exactly_one_home():
    """grep's fallback and files.find used to carry byte-identical copies."""
    from ai_coding.core import workspace_files

    assert "node_modules" in workspace_files.SKIP_DIRS
    source = Path("ai_coding/tools/grep_tool.py").read_text(encoding="utf-8")
    assert "_SKIP_DIRS" not in source, "grep still defines its own copy"
    handlers = Path("ai_coding/server/handlers.py").read_text(encoding="utf-8")
    assert "node_modules" not in handlers, "files.find still defines its own copy"


@pytest.mark.asyncio
async def test_the_walk_prunes_rather_than_filtering(tmp_path):
    """``rglob`` cannot prune: it descends into .git and node_modules and only
    then throws the paths away.  A tree with 3000 hidden files must cost almost
    nothing to walk."""
    from ai_coding.core.workspace_files import walk_workspace

    root = _tree(tmp_path / "ws")
    hidden = root / ".git" / "objects"
    hidden.mkdir(parents=True)
    for i in range(3000):
        (hidden / f"{i}.pack").write_text("x", encoding="utf-8")

    entries = list(walk_workspace(root))

    assert len(entries) < 30, f"walked {len(entries)} entries -- that is filtering, not pruning"


# ------------------------------------------------------- .gitignore handling


@pytest.mark.asyncio
async def test_the_fallback_respects_gitignore(tmp_path):
    """r17's fallback searched everything the directory held, ignored files
    included -- so it found matches rg never would."""
    root = _tree(tmp_path / "ws")

    found = await _matches(_fallback(root))

    assert "x.log" not in found, "a gitignored file was searched"
    assert "node_modules/dep.js" not in found
    assert {"a.py", "sub/b.py", "sub/c.txt"} <= found


@pytest.mark.asyncio
async def test_a_nested_gitignore_is_applied(tmp_path):
    """git applies the nearest .gitignore per directory, and so does rg."""
    root = _tree(tmp_path / "ws")
    (root / "sub" / ".gitignore").write_text("c.txt\n", encoding="utf-8")

    found = await _matches(_fallback(root))

    assert "sub/c.txt" not in found, "a nested ignore rule was not applied"
    assert "sub/b.py" in found


# ---------------------------------------------------------- include semantics
# Expected sets measured against ripgrep 15.1.0 on this exact tree.


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.py", {"a.py", "sub/b.py", "deep/nested/x.py"}),
        ("sub/*.py", {"sub/b.py"}),
        ("**/*.py", {"a.py", "sub/b.py", "deep/nested/x.py"}),
        ("*.log", set()),
    ],
)
@pytest.mark.asyncio
async def test_include_globs_mean_what_rg_means(tmp_path, pattern, expected):
    root = _tree(tmp_path / "ws")

    assert await _matches(_fallback(root), include=pattern) == expected


# --------------------------------------------------------- ordering and limit


@pytest.mark.asyncio
async def test_the_limit_picks_the_same_rows_either_way(tmp_path):
    """r17 kept ripgrep's parallel-walk order and the fallback's sorted order,
    so ``limit`` returned different rows -- and a total that lied about both."""
    root = _tree(tmp_path / "ws")
    lines = await _matches(_fallback(root))

    # The fallback renders in (path, line) order; the rg path must be sorted
    # into the same order before the limit applies.
    async def runner(argv, *, cwd, timeout):  # noqa: ARG001
        records = [
            {
                "type": "match",
                "data": {"path": {"text": path}, "line_number": 1,
                         "lines": {"text": "target\n"}},
            }
            # deliberately reversed: this is what a parallel walk emits
            for path in sorted(lines, reverse=True)
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(r) for r in records),
            stderr="", timed_out=False, error=None,
        )

    rg_tool = GrepTool(Sandbox(root), use_ripgrep=True, rg_path="/bin/rg", runner=runner)

    fallback_rows = (await _fallback(root).execute({"pattern": "target", "limit": 2})).output
    rg_rows = (await rg_tool.execute({"pattern": "target", "limit": 2})).output

    assert rg_rows == fallback_rows, (
        f"the two runners returned different rows:\n{rg_rows!r}\nvs\n{fallback_rows!r}"
    )


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep is not installed")
@pytest.mark.asyncio
async def test_both_runners_answer_the_same_question(tmp_path):
    """The real comparison: one tree, two genuinely different search engines."""
    root = _tree(tmp_path / "ws")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    for params in (
        {"pattern": "target"},
        {"pattern": "target", "include": "*.py"},
        {"pattern": "target", "include": "**/*.py"},
        {"pattern": "target", "limit": 2},
    ):
        fallback = await _fallback(root).execute(params)
        real = await GrepTool(Sandbox(root)).execute(params)
        assert real.output == fallback.output, (
            f"{params} diverged:\nrg: {real.output!r}\npy: {fallback.output!r}"
        )


# --------------------------------------------------- scoped searches (r19/F1)
#
# Every fallback test above omits ``path``, and with the target defaulting to
# the workspace two mistakes are invisible: joining the target-relative path
# onto the workspace, and resolving a ripgrep path against the *server's* cwd.
# Both need a scoped search to show up.


@pytest.mark.asyncio
async def test_the_fallback_searches_the_directory_it_was_pointed_at(tmp_path):
    root = _tree(tmp_path / "ws")

    result = await _fallback(root).execute({"pattern": "target", "path": "sub"})

    assert result.success, result.output
    assert "sub/b.py:1" in result.output, (
        f"a scoped search reported nothing it should have found: {result.output!r}"
    )


@pytest.mark.asyncio
async def test_a_scoped_search_reports_workspace_relative_paths(tmp_path):
    """Whatever the search was pointed at, the model is told where the file is
    in the *workspace* -- that is the only frame it can act on."""
    root = _tree(tmp_path / "ws")
    payload = json.dumps(
        {
            "type": "match",
            "data": {"path": {"text": "c.txt"}, "line_number": 1,
                     "lines": {"text": "target\n"}},
        }
    )

    async def runner(argv, *, cwd, timeout):  # noqa: ARG001
        return SimpleNamespace(returncode=0, stdout=payload, stderr="",
                               timed_out=False, error=None)

    tool = GrepTool(Sandbox(root), use_ripgrep=True, rg_path="/bin/rg", runner=runner)

    result = await tool.execute({"pattern": "target", "path": "sub"})

    assert "sub/c.txt:1" in result.output, (
        f"the ripgrep path was not anchored to the workspace: {result.output!r}"
    )


@pytest.mark.skipif(not shutil.which("rg"), reason="ripgrep is not installed")
@pytest.mark.asyncio
async def test_both_runners_agree_on_a_scoped_search(tmp_path):
    root = _tree(tmp_path / "ws")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    cases = (
        {"pattern": "target", "path": "sub"},
        {"pattern": "target", "path": "sub", "include": "*.py"},
    )
    for params in cases:
        fallback = await _fallback(root).execute(params)
        real = await GrepTool(Sandbox(root)).execute(params)
        assert fallback.output == real.output, (
            f"{params} diverged:\nrg: {real.output!r}\npy: {fallback.output!r}"
        )
