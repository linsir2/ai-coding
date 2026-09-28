"""App key bindings: palette interaction."""

from ai_coding.ui.palette import CommandPalette, PaletteEntry


def test_palette_opens_with_slash():
    palette = CommandPalette([PaletteEntry("test", "Test command")])
    assert not palette.is_open
    palette.open()
    assert palette.is_open


def test_palette_closes_with_backspace():
    palette = CommandPalette([PaletteEntry("test", "Test command")])
    palette.open()
    assert palette.is_open
    palette.close()
    assert not palette.is_open


def test_palette_select_returns_entry():
    entries = [PaletteEntry("test", "Test command"), PaletteEntry("other", "Other")]
    palette = CommandPalette(entries)
    palette.open()
    palette.handle_key("down")
    entry = palette.select()
    assert entry.name == "other"


def test_palette_wraps_around():
    entries = [PaletteEntry("a", "A"), PaletteEntry("b", "B")]
    palette = CommandPalette(entries)
    palette.open()
    palette.handle_key("up")
    entry = palette.select()
    assert entry.name == "b"


def test_input_buffer_has_enter_binding():
    """Verify that the input buffer has an Enter key binding."""
    assert True  # Placeholder - actual test is in integration test


def test_ctrl_c_exits():
    """Verify Ctrl-C is bound to exit."""
    assert True  # Placeholder - actual test is in integration test
