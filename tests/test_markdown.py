"""Strip Markdown decoration so the terminal shows the text, not the markup."""

from ai_coding.ui.markdown import strip_inline, strip_leading


def test_bold_markers_removed():
    assert strip_inline("**ai-coding** 项目") == "ai-coding 项目"


def test_double_underscore_bold_removed():
    assert strip_inline("__bold__") == "bold"


def test_inline_code_markers_removed():
    assert strip_inline("用 `uv` 管理") == "用 uv 管理"


def test_plain_text_untouched():
    assert strip_inline("普通文本") == "普通文本"


def test_unpaired_marker_survives_for_next_token():
    """Streaming splits markers; a lone '*' must not eat the rest of the line."""
    assert strip_inline("a * b") == "a * b"
    assert strip_inline("价格是 *5 元") == "价格是 *5 元"


def test_heading_hashes_removed():
    assert strip_leading("### 顶层文件") == "顶层文件"


def test_top_level_heading_removed():
    assert strip_leading("# Title") == "Title"


def test_list_marker_kept_as_bullet():
    assert strip_leading("- 项目") == "• 项目"


def test_non_heading_untouched():
    assert strip_leading("普通行") == "普通行"
    assert strip_leading("1. 第一项") == "1. 第一项"


def test_fence_marker_removed():
    assert strip_leading("```python") == ""
