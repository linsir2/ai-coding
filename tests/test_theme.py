"""Dark theme colors."""

from ai_coding.ui.theme import STYLE


def _style_keys() -> set[str]:
    return {name for name, _ in STYLE.style_rules}


def test_style_has_dark_background():
    rules = dict(STYLE.style_rules)
    body_fg = rules.get("body", "")
    assert body_fg.startswith("#")


def test_style_has_header_colors():
    keys = _style_keys()
    assert "header" in keys
    assert "header.border" in keys


def test_style_has_footer_colors():
    keys = _style_keys()
    assert "footer" in keys
    assert "footer.text" in keys


def test_style_has_palette_colors():
    keys = _style_keys()
    assert "palette.border" in keys
    assert "palette.selected" in keys


def test_style_has_input_colors():
    keys = _style_keys()
    assert "input" in keys
    assert "input.prefix" in keys
