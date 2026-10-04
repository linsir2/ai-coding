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


def test_model():
    cmd = parse_slash("/model")
    assert cmd is not None and cmd.name == "model"


def test_resume():
    cmd = parse_slash("/resume")
    assert cmd is not None and cmd.name == "resume"


def test_permissions():
    cmd = parse_slash("/permissions")
    assert cmd is not None and cmd.name == "permissions"


def test_todo():
    cmd = parse_slash("/todo")
    assert cmd is not None and cmd.name == "todo"


def test_skills():
    cmd = parse_slash("/skills")
    assert cmd is not None and cmd.name == "skills"


def test_plan():
    cmd = parse_slash("/plan")
    assert cmd is not None and cmd.name == "plan"


def test_review():
    cmd = parse_slash("/review")
    assert cmd is not None and cmd.name == "review"


def test_config():
    cmd = parse_slash("/config")
    assert cmd is not None and cmd.name == "config"


def test_plan_run_was_absorbed_into_plan():
    """M1-A: /plan-run no longer exists — /plan plans and executes."""
    assert parse_slash("/plan-run") is None
    assert parse_slash("/plan").name == "plan"


def test_tasks():
    cmd = parse_slash("/tasks")
    assert cmd is not None and cmd.name == "tasks"
