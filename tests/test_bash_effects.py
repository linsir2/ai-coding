"""bash effects: candidate paths + per-call stat, not a whole-workspace scan."""

from __future__ import annotations

import asyncio

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
    assert kw["confidence"] == "observed"


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
    assert seen[0][1]["confidence"] == "observed"


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


def test_literal_cd_moves_relative_candidates(tmp_path):
    (tmp_path / "sub").mkdir()
    paths = effect_path_candidates(
        "cd sub && touch f", workspace=str(tmp_path)
    )
    assert str(tmp_path / "sub" / "f") in paths
    assert str(tmp_path / "f") not in paths


def test_subshell_cd_does_not_leak(tmp_path):
    (tmp_path / "sub").mkdir()
    paths = effect_path_candidates(
        "(cd sub; touch f); touch g", workspace=str(tmp_path)
    )
    assert str(tmp_path / "sub" / "f") in paths
    assert str(tmp_path / "g") in paths
    assert str(tmp_path / "sub" / "g") not in paths


def test_dynamic_cd_skips_relative_candidates(tmp_path):
    paths = effect_path_candidates(
        'cd "$D"; touch f', workspace=str(tmp_path)
    )
    assert str(tmp_path / "f") not in paths


def test_group_redirect_uses_the_wrapper_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    paths = effect_path_candidates(
        "{ cd sub; touch f; } > out", workspace=str(tmp_path)
    )
    assert str(tmp_path / "out") in paths
    assert str(tmp_path / "sub" / "f") in paths


def test_pipeline_redirect_and_fd_targets(tmp_path):
    paths = effect_path_candidates(
        "cat a | tee b > c; touch d 2>&1; cat < in",
        workspace=str(tmp_path),
    )
    assert str(tmp_path / "c") in paths
    assert str(tmp_path / "d") in paths
    assert str(tmp_path / "in") not in paths
    assert str(tmp_path / "1") not in paths


def test_fd_prefixed_and_amp_redirects_are_output_candidates(tmp_path):
    paths = effect_path_candidates(
        "echo hi 2>err.txt; echo hi &>all.txt; echo hi 1>out.txt",
        workspace=str(tmp_path),
    )
    assert str(tmp_path / "err.txt") in paths
    assert str(tmp_path / "all.txt") in paths
    assert str(tmp_path / "out.txt") in paths


def test_command_and_process_substitutions_are_traversed(tmp_path):
    paths = effect_path_candidates(
        "echo $(printf x > sub.txt); diff <(touch e.txt) <(touch f.txt)",
        workspace=str(tmp_path),
    )
    assert str(tmp_path / "sub.txt") in paths
    assert str(tmp_path / "e.txt") in paths
    assert str(tmp_path / "f.txt") in paths


def test_background_does_not_leak_cd(tmp_path):
    paths = effect_path_candidates(
        "cd sub & touch f", workspace=str(tmp_path)
    )
    assert str(tmp_path / "f") in paths
    assert str(tmp_path / "sub" / "f") not in paths


def test_absolute_cd_recovers_after_dynamic_cd(tmp_path):
    paths = effect_path_candidates(
        'cd "$D"; cd /tmp; touch f', workspace=str(tmp_path)
    )
    assert "/tmp/f" in paths


def test_substitutions_nested_in_strings_and_process_destinations(tmp_path):
    (tmp_path / "sub").mkdir()
    paths = effect_path_candidates(
        'echo "$(touch f)"; echo hi > >(tee log); FOO=$(cd sub; touch g) touch h',
        workspace=str(tmp_path),
    )
    assert str(tmp_path / "f") in paths
    assert str(tmp_path / "log") in paths
    assert str(tmp_path / "sub" / "g") in paths
    assert str(tmp_path / "h") in paths


def test_background_resets_to_the_preceding_cwd_not_the_program_entry(tmp_path):
    (tmp_path / "sub").mkdir()
    paths = effect_path_candidates(
        "cd sub; touch a & touch b",
        workspace=str(tmp_path),
    )
    assert str(tmp_path / "sub" / "a") in paths
    assert str(tmp_path / "sub" / "b") in paths
    group = effect_path_candidates(
        "{ cd sub & touch c; }", workspace=str(tmp_path)
    )
    assert str(tmp_path / "c") in group


def test_ambiguous_or_branch_does_not_guess(tmp_path):
    paths = effect_path_candidates(
        "cd sub || cd other; touch c", workspace=str(tmp_path)
    )
    assert str(tmp_path / "sub" / "c") not in paths
    assert str(tmp_path / "other" / "c") not in paths


@pytest.mark.asyncio
async def test_a_background_bash_write_settles_into_the_ledger(tmp_path):
    from ai_coding.core.jobs import JobRegistry
    from ai_coding.domain.session import SessionData

    session = SessionData(session_id="s", title="", created_time="t", last_access_time="t")
    jobs = JobRegistry(tmp_path / "jobs")
    seen = []

    def settle(session_id, rows):
        for row in rows:
            session.record_effect(
                row["path"],
                row["version"],
                tool="bash",
                call_id=row["call_id"],
                kind=row["kind"],
                confidence="observed",
                seq=row["seq"],
            )
            seen.append(row)

    tool = BashTool(timeout_seconds=5)
    ctx = ToolContext(
        workspace=str(tmp_path),
        session_id="s",
        jobs=jobs,
        effects=lambda: session.effects,
        settle_effect=settle,
        note_effect=lambda *row, **kw: None,
        call_id="c1",
    )
    result = await tool.execute(
        {"command": "printf 'hello\\n' > bg.txt", "run_in_background": True},
        ctx,
    )
    assert result.success is True
    job_id = result.data["job_id"]
    job = jobs.get(job_id)
    await jobs.wait(job, timeout=10)
    await asyncio.sleep(0.05)

    assert seen
    assert seen[0]["path"] == "bg.txt"
    assert seen[0]["kind"] == "write"
    receipt = session.effects.receipt_for("bg.txt")
    assert receipt is not None
    assert receipt.confidence == "observed"
