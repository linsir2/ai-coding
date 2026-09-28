from ai_coding.ui.palette import CommandPalette, PaletteEntry


def _entries():
    return [
        PaletteEntry("plan", "Create and refine an implementation plan", "📝"),
        PaletteEntry("review", "Review current changes and surface issues", "🔍"),
        PaletteEntry("model", "Change model and reasoning effort", "🤖"),
    ]


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
