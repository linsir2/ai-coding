"""C-3b: subagent_list -- the model can read the background sub-agent table.

The table is the runner's (``task_list()``); before this its only reader was
the human ``/tasks`` command, so a model that launched background runs could
not ask what was still running -- the completion notice arrives once, and a
compact can take it away.  The listing mirrors job_list's shape: running rows
always, the most recent settled rows, a count line for the rest, and the
ledger pointer on settled rows so the full packet stays reachable by ``read``.
"""

import asyncio

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.core.subagent import SubAgentRunner
from ai_coding.domain.loop import StepResult
from ai_coding.domain.subagent import BackgroundTask
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.subagent_tool import SubAgentListTool


class _FakeRunner:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def task_list(self):
        return list(self.rows)


def _row(
    run_id,
    status="completed",
    *,
    name="probe",
    started=100.0,
    ended=104.6,
    error=None,
    usage=None,
    ledger_path=None,
):
    return BackgroundTask(
        run_id=run_id,
        name=name,
        task="do it",
        status=status,
        started=started,
        ended=ended,
        error=error,
        usage=usage,
        ledger_path=ledger_path,
    )


@pytest.mark.asyncio
async def test_the_list_renders_running_and_settled_rows():
    rows = [
        _row("aaaa111122223333", status="running", started=100.0, ended=None),
        _row(
            "bbbb444455556666",
            started=102.0,
            usage={"total_tokens": 1234},
            ledger_path="/ws/transcripts/subagents/x.json",
        ),
        _row("cccc777788889999", status="error", started=104.0, error="boom"),
    ]
    result = await SubAgentListTool(_FakeRunner(rows)).execute({})

    assert result.success is True
    assert "aaaa11112222 probe running" in result.output
    assert (
        "bbbb44445555 probe completed 3s · 1234 tok · 完整回包 "
        "/ws/transcripts/subagents/x.json"
    ) in result.output
    assert "cccc77778888 probe error 1s · boom" in result.output
    # The running row leads; settled rows are newest first.
    assert result.output.index("running") < result.output.index("cccc")
    assert result.output.index("cccc") < result.output.index("bbbb")
    assert [row["id"] for row in result.data["runs"]] == [
        "aaaa111122223333",
        "cccc777788889999",
        "bbbb444455556666",
    ]


@pytest.mark.asyncio
async def test_empty_table_says_so():
    result = await SubAgentListTool(_FakeRunner()).execute({})
    assert result.success is True
    assert result.output == "no background sub-agent run"


@pytest.mark.asyncio
async def test_older_settled_rows_collapse_to_a_count():
    rows = [_row(f"gone{i:08d}zz", started=float(i)) for i in range(5)]
    rows.append(_row("live00000001", status="running", started=200.0, ended=None))
    rows += [_row(f"recent{i:06d}", started=300.0 + i) for i in range(20)]

    result = await SubAgentListTool(_FakeRunner(rows)).execute({})

    assert "(还有 5 条更早的已结算 run 未显示)" in result.output
    assert "gone0" not in result.output
    assert "recent000019" in result.output
    assert result.data["shown"] == 21  # 1 running + 20 recent
    assert result.data["older_settled"] == 5


class _Engine(ChatEngine):
    def __init__(self, text="did it"):
        self._text = text

    async def step(self, messages, *, tools, instructions=None, on_delta=None):
        return StepResult(text=self._text, finish_reason="stop", usage=None)

    async def aclose(self):
        return None


def _runner(engine, **over):
    kwargs = {
        "app_config": object(),
        "engine_factory": lambda: engine,
        "build_tools": lambda wd: ToolRegistry(),
        "timeout_seconds": 600.0,
    }
    kwargs.update(over)
    return SubAgentRunner(**kwargs)


@pytest.mark.asyncio
async def test_a_settled_run_carries_its_ledger_pointer_to_the_list(tmp_path):
    runner = _runner(_Engine(), ledger_root=str(tmp_path))
    run_id = runner.spawn_background("do it", name="probe", emit=lambda _e: None)
    for _ in range(200):
        rows = runner.task_list()
        if rows and rows[0].status != "running":
            break
        await asyncio.sleep(0.02)

    result = await SubAgentListTool(runner).execute({})

    assert run_id[:12] in result.output
    assert "completed" in result.output
    row = result.data["runs"][0]
    assert row["ledger_path"]
    assert row["ledger_path"].endswith(".json")
    assert (tmp_path / "transcripts" / "subagents").exists()
