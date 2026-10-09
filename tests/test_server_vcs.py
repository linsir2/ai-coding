"""TDD (M1): workspace VCS facts — the git branch shown in the home footer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_coding.server.vcs import git_branch

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git is not installed")


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        [_GIT, *args], cwd=cwd, check=True,
        capture_output=True,
        # Deterministic identity for the commit used by the detached test.
        env={"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def test_branch_name_in_a_repository(tmp_path: Path):
    _git(["init", "-b", "main"], tmp_path)
    assert git_branch(tmp_path) == "main"


def test_custom_branch_name(tmp_path: Path):
    _git(["init", "-b", "main"], tmp_path)
    _git(["checkout", "-b", "feature/x"], tmp_path)
    assert git_branch(tmp_path) == "feature/x"


def test_detached_head_reports_empty(tmp_path: Path):
    _git(["init", "-b", "main"], tmp_path)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    _git(["add", "f.txt"], tmp_path)
    _git(["commit", "-m", "c"], tmp_path)
    _git(["checkout", "--detach"], tmp_path)
    assert git_branch(tmp_path) == ""


def test_non_repository_returns_empty(tmp_path: Path):
    assert git_branch(tmp_path) == ""


def test_missing_directory_returns_empty(tmp_path: Path):
    assert git_branch(tmp_path / "does-not-exist") == ""


def test_git_missing_returns_empty(tmp_path: Path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("ai_coding.server.vcs.subprocess.run", boom)
    assert git_branch(tmp_path) == ""


def test_timeout_returns_empty(tmp_path: Path, monkeypatch):
    def slow(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=3.0)

    monkeypatch.setattr("ai_coding.server.vcs.subprocess.run", slow)
    assert git_branch(tmp_path) == ""
