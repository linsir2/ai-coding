"""R7e-2: where a line typed while the turn is running waits."""

from __future__ import annotations

from ai_coding.core.steering import SteeringQueue


def test_put_then_drain_returns_the_lines_in_order():
    queue = SteeringQueue()
    queue.put("第一句")
    queue.put("第二句")
    assert queue.drain() == ["第一句", "第二句"]


def test_drain_is_idempotent():
    queue = SteeringQueue()
    queue.put("一次")
    assert queue.drain() == ["一次"]
    assert queue.drain() == []


def test_pending_counts_without_draining():
    queue = SteeringQueue()
    queue.put("a")
    queue.put("b")
    assert queue.pending == 2
    assert queue.pending == 2  # 看一眼不该消费


def test_blank_lines_are_dropped():
    queue = SteeringQueue()
    assert queue.put("   ") is False
    assert queue.put("") is False
    assert queue.pending == 0


def test_a_long_line_is_truncated_with_a_marker():
    queue = SteeringQueue(max_chars=10)
    queue.put("x" * 50)
    (line,) = queue.drain()
    assert line.endswith("…")
    assert len(line) == 11


def test_at_most_max_messages_come_out_and_the_rest_stay_queued():
    queue = SteeringQueue(max_messages=2)
    for index in range(5):
        queue.put(f"第 {index} 句")
    assert queue.drain() == ["第 0 句", "第 1 句"]
    assert queue.pending == 3  # 不许悄悄丢掉用户的输入


def test_whitespace_is_trimmed_but_inner_text_is_kept():
    queue = SteeringQueue()
    queue.put("  改用 uv， 不要 pip  ")
    assert queue.drain() == ["改用 uv， 不要 pip"]


def test_a_slash_line_is_not_steering():
    """Q6：命令不是指令；put 拒绝它，调用方（reader）留到回合结束再交回 REPL。"""
    queue = SteeringQueue()
    assert queue.put("/status") is False
    assert queue.pending == 0
