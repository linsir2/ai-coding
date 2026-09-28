"""Body scrolling helpers."""

from unittest.mock import MagicMock

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from ai_coding.ui.scroll import auto_scroll_to_end, scroll_down, scroll_up


def _window() -> Window:
    return Window(content=FormattedTextControl(lambda: [("", "x\n")]))


def test_scroll_up_calls_private_helper():
    win = _window()
    win._scroll_up = MagicMock()
    scroll_up(win)
    win._scroll_up.assert_called_once()


def test_scroll_down_calls_private_helper():
    win = _window()
    win._scroll_down = MagicMock()
    scroll_down(win)
    win._scroll_down.assert_called_once()


def test_scroll_is_a_noop_when_window_not_rendered():
    """render_info is None before the first render — must not raise."""
    win = _window()
    assert win.render_info is None
    scroll_up(win)  # must not raise
    scroll_down(win)  # must not raise


def test_auto_scroll_to_end_tolerates_unrendered_window():
    win = _window()
    auto_scroll_to_end(win)  # must not raise


def test_auto_scroll_to_end_uses_scroll_down():
    """Pinning to the end reuses _scroll_down, which clamps to the cursor."""
    win = _window()
    win._scroll_down = MagicMock()
    win._scroll = MagicMock()
    auto_scroll_to_end(win)
    win._scroll_down.assert_called_once()
    win._scroll.assert_not_called()


def test_app_enables_mouse_support():
    """The Application must opt into mouse reporting for wheel events."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "mouse_support=True" in source


def test_app_binds_wheel_keys():
    """Wheel keys must be bound to the scroll helpers."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "Keys.ScrollUp" in source
    assert "Keys.ScrollDown" in source


def test_app_no_longer_touches_private_vertical_scroll():
    """The private field must be gone — it has no bounds clamping."""
    import inspect

    from ai_coding.ui.app import run_app

    source = inspect.getsource(run_app)
    assert "render_info.vertical_scroll" not in source
