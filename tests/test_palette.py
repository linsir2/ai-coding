from ai_coding.ui.palette import (
    PALETTE_INNER_WIDTH,
    CommandPalette,
    PaletteEntry,
    _display_width,
)


def _entries():
    return [
        PaletteEntry("plan", "Create and refine an implementation plan", "📝"),
        PaletteEntry("review", "Review current changes and surface issues", "🔍"),
        PaletteEntry("model", "Change model and reasoning effort", "🤖"),
    ]


def _rows(p):
    """Each rendered row without its trailing newline."""
    return [text.rstrip("\n") for _, text in p.render()]


def test_palette_open_close():
    p = CommandPalette(_entries())
    assert not p.is_open
    p.open()
    assert p.is_open
    p.close()
    assert not p.is_open


def test_palette_navigate_down():
    p = CommandPalette(_entries())
    p.open()
    p.handle_key("down")
    assert p.selected_index == 1
    p.handle_key("down")
    assert p.selected_index == 2
    p.handle_key("down")
    assert p.selected_index == 0


def test_palette_navigate_up():
    p = CommandPalette(_entries())
    p.open()
    p.handle_key("up")
    assert p.selected_index == 2


def test_palette_select():
    p = CommandPalette(_entries())
    p.open()
    p.handle_key("down")
    selected = p.select()
    assert selected.name == "review"
    assert not p.is_open


def test_palette_render():
    p = CommandPalette(_entries())
    p.open()
    text = p.render()
    flat = "".join(t for _, t in text)
    assert "plan" in flat
    assert "review" in flat
    assert "model" in flat


def test_palette_has_a_border_and_hints():
    p = CommandPalette(_entries())
    p.open()
    rows = _rows(p)
    assert rows[0].startswith("╭") and rows[0].endswith("╮")
    assert rows[-1].startswith("╰") and rows[-1].endswith("╯")
    assert "navigate" in rows[-2]


def test_every_row_is_exactly_the_panel_width():
    """A ragged border is the classic TUI tell: emoji are two columns wide."""
    p = CommandPalette(_entries())
    p.open()
    for row in _rows(p):
        assert _display_width(row) == PALETTE_INNER_WIDTH + 2, row


def test_the_selected_row_is_marked():
    p = CommandPalette(_entries())
    p.open()
    assert "❯" in _rows(p)[1]
    p.handle_key("down")
    assert "❯" in _rows(p)[2] and "❯" not in _rows(p)[1]
