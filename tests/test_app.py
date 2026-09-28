"""Application 组装。"""

from ai_coding.ui.app import PALETTE_ENTRIES, run_app


def test_palette_entries_complete():
    names = {e.name for e in PALETTE_ENTRIES}
    expected = {"plan", "review", "model", "status", "compact", "resume", "permissions", "help"}
    assert expected <= names


def test_palette_entries_have_descriptions():
    for entry in PALETTE_ENTRIES:
        assert entry.description, f"Missing description for {entry.name}"


def test_run_app_is_coroutine():
    import asyncio
    assert asyncio.iscoroutinefunction(run_app)
