from prompt_toolkit.styles import Style

from ai_coding.ui.theme import STYLE


def test_style_is_style_instance():
    assert isinstance(STYLE, Style)


def test_style_has_header_class():
    assert any("header" in str(rule) for rule in STYLE.style_rules)


def test_style_has_footer_class():
    assert any("footer" in str(rule) for rule in STYLE.style_rules)
