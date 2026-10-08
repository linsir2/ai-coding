"""TDD: where the app's logs go while a full-screen TUI owns the terminal.

`setup_logging` points the root logger at stderr.  Under the TUI stderr *is* the
terminal Textual renders to, so a log record is written into the middle of the
frame at whatever cursor position happens to be current.  Textual then repaints
only the regions it believes changed, so fragments of the record survive on
screen -- measured: a stray `e"` at the far right of the input row after every
turn, which only a resize cleared.  The record that did it was httpx's, at INFO,
because the filter named `httpx` and the logger calls itself `httpx2`.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path

from ai_coding.infra.logger_setup import send_logs_to_file, setup_logging

_LOG = Path(".ai-coding") / "ai-coding.log"


@contextmanager
def _clean_root():
    """An empty root logger for the body of this ``with``.

    ``setup_logging`` keys idempotence on "has any handler", which is right in
    the app and wrong under a harness that installs four of its own for the test
    call: it would report "already configured" and add nothing to assert
    against.  The clearing has to happen *here* rather than in a fixture --
    pytest installs its handlers after fixtures and before the body.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    for handler in before:
        root.removeHandler(handler)
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in before:
            root.addHandler(handler)
        root.setLevel(level)


def _writes_to_the_terminal() -> bool:
    """True when any root handler has stderr as its stream.

    Pytest's own handlers capture to a StringIO or a file, so they do not count
    as writing to the screen this is about.
    """
    return any(
        getattr(h, "stream", None) is sys.stderr for h in logging.getLogger().handlers
    )


def _writing_to(path: Path) -> list[logging.Handler]:
    wanted = str(path.resolve())
    return [
        h
        for h in logging.getLogger().handlers
        if str(getattr(h, "baseFilename", "")) == wanted
    ]


def test_the_default_still_logs_to_stderr():
    """The CLI paths want this: they are line-oriented, so a log line
    interleaves with the output instead of corrupting a frame."""
    with _clean_root():
        setup_logging()

        assert _writes_to_the_terminal()


def test_switching_to_a_file_removes_the_terminal_handler(tmp_path):
    with _clean_root():
        setup_logging()

        send_logs_to_file(tmp_path)

        assert not _writes_to_the_terminal(), "a stream handler still writes into the frame"
        assert _writing_to(tmp_path / _LOG), "the file handler was not installed"


def test_a_record_lands_in_the_file_not_on_the_terminal(tmp_path, capsys):
    with _clean_root():
        # The real order: setup_logging sets the level, then the TUI takes the
        # terminal and the logs are moved to a file.
        setup_logging()
        send_logs_to_file(tmp_path)

        # A probe logger of the app's own: `httpx2` is deliberately silenced by
        # the fix, so asking it to prove the pipeline would prove nothing.
        logging.getLogger("ai_coding.probe").info("a record the app emits")

        assert "a record the app emits" not in capsys.readouterr().err
        assert "a record the app emits" in (tmp_path / _LOG).read_text(encoding="utf-8")


def test_the_log_file_is_bounded(tmp_path):
    """The TUI runs for hours and the HTTP client logs every request; a file
    that only grows is a leak nobody asked for."""
    from logging.handlers import RotatingFileHandler

    with _clean_root():
        send_logs_to_file(tmp_path)

        handler = next(iter(_writing_to(tmp_path / _LOG)))
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes > 0 and handler.backupCount > 0


def test_an_unwritable_workspace_does_not_take_the_ui_down(tmp_path):
    """A read-only checkout must still start: losing logs beats losing the
    screen they were about to corrupt."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    with _clean_root():
        assert send_logs_to_file(blocked) is None
        assert not _writes_to_the_terminal()


def test_the_httpx_filter_covers_the_names_it_actually_uses():
    """The filter named `httpx`; the request logs arrive from `httpx2`.  Pinned
    because a miss here is silent -- it shows up as screen corruption."""
    with _clean_root():
        setup_logging()

        for name in ("httpx", "httpx2", "httpx._client", "openai"):
            # ``getEffectiveLevel``, not ``level``: a child logger is NOTSET and
            # inherits.  ``httpx2`` earns its place here because it is a
            # *different top-level logger* -- silencing ``httpx`` does nothing
            # for it, and its INFO records are the ones that reached the frame.
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


def test_setup_logging_is_still_idempotent():
    with _clean_root():
        assert setup_logging() is True
        assert setup_logging() is False


def test_sending_twice_leaves_one_handler(tmp_path):
    with _clean_root():
        send_logs_to_file(tmp_path)
        send_logs_to_file(tmp_path)

        assert len(_writing_to(tmp_path / _LOG)) == 1, "a second call would double every line"
