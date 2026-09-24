"""Turn digests: one harness-derived line per turn, and the un-finished handoff (R6a)."""

from __future__ import annotations

from ai_coding.domain.loop import TurnOutcome, TurnStatus
from ai_coding.domain.message import ToolArtifact
from ai_coding.domain.session import SessionData
from ai_coding.domain.task import TaskState
from ai_coding.domain.turn_digest import (
    TurnDigest,
    append_digest,
    build_digest,
    one_line,
    render_handoff,
    render_working_memory,
)


def _outcome(
    status: TurnStatus = TurnStatus.COMPLETED,
    text: str | None = "改好了",
    reason: str = "model finished",
    iterations: int = 2,
) -> TurnOutcome:
    return TurnOutcome(status=status, text=text, iterations=iterations, reason=reason)


def _artifact(path: str) -> ToolArtifact:
    return ToolArtifact(path=path, kind="write", bytes=10, description="")


def _task() -> TaskState:
    task = TaskState(goal="让 make test 通过")
    task.add("创建 Makefile")
    task.add("修复 calc.py")
    task.update("1", status="completed")
    return task


def _session() -> SessionData:
    return SessionData(session_id="s", title="", created_time="", last_access_time="")


def test_one_line_collapses_whitespace_and_cuts():
    out = one_line("第一行\n第二行   第三行" + "尾" * 200, 20)
    assert "\n" not in out
    assert len(out) <= 20
    assert out.endswith("…")


def test_digest_request_is_one_short_line():
    digest = build_digest(
        turn=3,
        user_input="请" + "很长" * 500,
        outcome=_outcome(),
        request_chars=60,
    )
    assert "\n" not in digest.request
    assert len(digest.request) <= 60


def test_digest_records_status_files_and_turn():
    digest = build_digest(
        turn=7,
        user_input="让 make test 通过",
        outcome=_outcome(),
        artifacts=[_artifact("Makefile"), _artifact("Makefile"), _artifact("src/calc.py")],
        task=_task(),
    )
    assert digest.turn == 7
    assert digest.status == "completed"
    assert digest.files == ("Makefile", "src/calc.py")  # 去重、保序
    assert digest.open_items == 1
    assert "Makefile" in digest.render()


def test_digest_falls_back_to_the_status_when_the_model_said_nothing():
    digest = build_digest(
        turn=5,
        user_input="继续",
        outcome=_outcome(
            status=TurnStatus.MAX_ITERATIONS, text=None, reason="iteration budget exhausted"
        ),
    )
    assert "迭代预算用尽" in digest.outcome
    assert digest.status == "max_iterations"


def test_append_digest_keeps_a_bounded_ring():
    session = _session()
    for turn in range(1, 31):
        append_digest(
            session,
            build_digest(turn=turn, user_input=f"请求{turn}", outcome=_outcome()),
            keep=12,
        )
    assert len(session.turn_digests) == 12
    assert [digest.turn for digest in session.turn_digests] == list(range(19, 31))


def test_append_digest_survives_a_hostile_session():
    append_digest(object(), TurnDigest(turn=1, request="r", outcome="o"), keep=3)  # 不抛


def test_digest_survives_a_session_round_trip():
    session = _session()
    session.turn_count = 4
    append_digest(
        session,
        build_digest(
            turn=4,
            user_input="改 calc",
            outcome=_outcome(),
            artifacts=[_artifact("src/calc.py")],
            task=_task(),
        ),
        keep=12,
    )
    data = session.to_dict()
    assert data["turnCount"] == 4
    assert data["turnDigests"][0]["turn"] == 4
    back = SessionData.from_dict(data)
    assert back.turn_count == 4
    assert back.turn_digests == session.turn_digests


def test_broken_digest_rows_are_ignored():
    session = SessionData.from_dict(
        {
            "sessionId": "s",
            "title": "",
            "createdTime": "",
            "lastAccessTime": "",
            "messages": [],
            "turnDigests": [{"turn": "x"}, {}, None, {"turn": 2, "request": "ok"}],
        }
    )
    assert [digest.turn for digest in session.turn_digests] == [2]


def test_working_memory_is_empty_when_not_compacted():
    digests = [TurnDigest(turn=1, request="a", outcome="b")]
    assert render_working_memory(digests, current_turn=2, compacted=False) == ""
    assert render_working_memory([], current_turn=2) == ""


def test_working_memory_names_the_current_turn_and_stays_bounded():
    digests = [
        TurnDigest(turn=n, request=f"第{n}个请求" + "字" * 40, outcome=f"第{n}轮结果")
        for n in range(1, 21)
    ]
    block = render_working_memory(digests, current_turn=21, max_chars=600)
    assert block.startswith("<working-memory>")
    assert block.endswith("</working-memory>")
    assert "第 21 轮" in block
    assert len(block) <= 600
    assert "第20个请求" in block  # 最新的一定在
    assert "第1个请求" not in block  # 最旧的先被丢
    assert "memory_search" in block  # 指路，而不是让对方整读会话文件


def test_handoff_names_goal_open_items_and_failure():
    text = render_handoff(
        outcome=_outcome(
            status=TurnStatus.MAX_ITERATIONS,
            text=None,
            reason="iteration budget exhausted",
            iterations=5,
        ),
        task=_task(),
        failures=[("bash", "nonzero_exit", "make: *** No rule to make target 'test'.")],
        artifacts=[_artifact("Makefile")],
        trace_path=".ai-coding/traces/abc/turn-1.jsonl",
    )
    assert "回合未完成" in text
    assert "目标: 让 make test 通过" in text
    assert "已完成: 1. 创建 Makefile" in text
    assert "未完成: 2. 修复 calc.py" in text
    assert "bash → nonzero_exit" in text
    assert "Makefile" in text
    assert "turn-1.jsonl" in text


def test_handoff_is_empty_when_the_model_spoke():
    assert render_handoff(outcome=_outcome()) == ""
    # 空白 == 没说话，但回合是「正常结束」：不编造「未完成」
    assert render_handoff(outcome=_outcome(text="   ")) == ""
    # 未完成的回合才需要交接
    assert "[回合未完成" in render_handoff(
        outcome=_outcome(status=TurnStatus.TIMEOUT, text="   ", reason="turn deadline exceeded")
    )
