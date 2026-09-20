"""TDD (M3-1): token estimation used by the context-compression thresholds."""

from ai_coding.core.context_compression import estimate_tokens


def test_empty_string_zero():
    assert estimate_tokens("") == 0


def test_ascii_counts_a_quarter_per_char():
    # ceil(len/4): "hello" -> ceil(5/4) = 2
    assert estimate_tokens("hello") == 2


def test_cjk_counts_a_half_per_char():
    # ceil(2/2) = 1 for two CJK characters
    assert estimate_tokens("\u4e2d\u6587") == 1  # 中文


def test_mixed_counts():
    # "hi" (2 ascii -> ceil(2/4)=1) + "中" (1 cjk -> ceil(1/2)=1)
    assert estimate_tokens("hi\u4e2d") == 2


def test_with_tool_arguments():
    # a JSON-ish block of ASCII counts via the others rule
    args = '{"path": "src/main.py"}'
    n = len(args)
    assert estimate_tokens(args) == (n + 3) // 4


def test_long_ascii_linear():
    assert estimate_tokens("x" * 100) == 25