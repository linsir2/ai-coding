"""TDD (M5-2): slash command parsing for the REPL.

Commands are the ``直接命令`` reserved in M4.  Unknown slash-prefixed input and
plain text both return ``None`` (so they go to the model).  All pure and offline.
"""

from ai_coding.ui.slash import SlashCommand, parse_slash


def test_plain_text_is_none():
    assert parse_slash("hello world") is None


def test_empty_is_none():
    assert parse_slash("") is None
    assert parse_slash("   ") is None


def test_quit_aliases():
    for raw in ("/quit", "/exit", "/q"):
        cmd = parse_slash(raw)
        assert isinstance(cmd, SlashCommand)
        assert cmd.name == "quit"


def test_clear():
    cmd = parse_slash("  /clear  ")
    assert cmd is not None
    assert cmd.name == "clear"


def test_help_aliases():
    for raw in ("/help", "/h"):
        cmd = parse_slash(raw)
        assert cmd is not None
        assert cmd.name == "help"


def test_status():
    cmd = parse_slash("/status")
    assert cmd is not None
    assert cmd.name == "status"


def test_unknown_slash_is_none():
    # unknown commands fall back to plain text (sent to the model)
    assert parse_slash("/frobnicate") is None


def test_bare_slash_is_none():
    assert parse_slash("/") is None
