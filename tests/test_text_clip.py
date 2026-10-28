"""``clip_head_tail``: both ends kept, the omission stated in the middle.

Measured in eval-08: a head-only 50k bash clip lost pytest's ``FAILED`` list,
a tail-only 1.2k driver clip lost the first traceback.  These cases pin the
contract the two call sites now share.
"""

from __future__ import annotations

from ai_coding.infra.text_clip import clip_head_tail


def test_a_short_text_is_returned_unchanged():
    assert clip_head_tail("hello", 100) == "hello"


def test_a_zero_limit_disables_clipping():
    assert clip_head_tail("hello", 0) == "hello"


def test_both_ends_survive_and_the_middle_says_how_much_was_lost():
    text = "HEAD" + ("x" * 10_000) + "TAIL"
    out = clip_head_tail(text, 1_000)

    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "省略" in out
    # 1,000 kept + the marker, not the 10,008 original
    assert len(out) < 1_200


def test_the_marker_reports_the_real_omission_count():
    text = "a" * 500 + "b" * 500
    out = clip_head_tail(text, 200, head_ratio=0.5)

    omitted = 1_000 - 200
    assert f"省略 {omitted} 字符" in out


def test_a_note_says_where_the_whole_text_lives():
    out = clip_head_tail("x" * 1_000, 100, note="全文见 /tmp/job.log")

    assert "全文见 /tmp/job.log" in out


def test_the_tail_keeps_the_verdict_of_a_test_run():
    """The concrete failure: the last line is the only place ``N failed`` lives."""
    body = "\n".join(f"FAILED tests/test_{i}.py::test_x" for i in range(400))
    text = f"{body}\n18 failed, 84 warnings in 11.52s"
    out = clip_head_tail(text, 2_000)

    assert out.rstrip().endswith("18 failed, 84 warnings in 11.52s")
    assert "FAILED tests/test_0.py::test_x" in out
