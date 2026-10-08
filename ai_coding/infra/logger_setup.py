"""Idempotent root logging configuration."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

#: Log directories are per workspace and already ignored, like the traces
#: beside them.
_LOG_DIR = ".ai-coding"
_LOG_NAME = "ai-coding.log"

#: The TUI runs for hours and the HTTP client logs every request; a file that
#: only grows is a leak nobody asked for.
_LOG_MAX_BYTES = 2_000_000
_LOG_BACKUPS = 3

#: Third-party loggers whose INFO output is not worth a line: the HTTP client
#: prints the full URL of every request.  ``httpx2`` is not a typo -- that is
#: the name the logger actually uses, and filtering only ``httpx`` let a 152
#: character record through into the middle of the TUI's frame.
_NOISY = ("httpx", "httpx2", "openai")


def setup_logging(level: int = logging.INFO, fmt: str = _DEFAULT_FORMAT) -> bool:
    """Configure the root logger once. Returns ``True`` if configured now."""
    root = logging.getLogger()
    if root.handlers:
        return False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
    return True


def send_logs_to_file(
    workspace: str | Path,
    *,
    fmt: str = _DEFAULT_FORMAT,
    max_bytes: int = _LOG_MAX_BYTES,
    backups: int = _LOG_BACKUPS,
) -> Path | None:
    """Point the root logger at a file, for when a TUI owns the terminal.

    Under the TUI, stderr *is* the screen Textual draws on, so a log record is
    written into the middle of the frame at whatever cursor position happens to
    be current.  Textual repaints only the regions it believes changed, so
    fragments of the record survive on screen until something forces a full
    redraw -- measured as a stray two characters at the right edge of the input
    row after every turn.  The CLI paths keep stderr: they are line-oriented, so
    a log line interleaves rather than corrupting anything.

    Returns the file it opened, or ``None`` when the workspace cannot be written
    to -- a read-only checkout must still start, and losing logs beats losing
    the screen they were about to corrupt.
    """
    root = logging.getLogger()
    path = Path(workspace).expanduser().resolve() / _LOG_DIR / _LOG_NAME
    handler: logging.Handler
    result: Path | None = path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max(1, int(max_bytes)),
            backupCount=max(0, int(backups)),
            encoding="utf-8",
        )
    except OSError:
        handler = logging.NullHandler()
        result = None
    handler.setFormatter(logging.Formatter(fmt))
    for existing in list(root.handlers):
        root.removeHandler(existing)
        try:
            existing.close()
        except Exception:  # noqa: BLE001 -- replacing a handler must not raise
            pass
    root.addHandler(handler)
    return result


__all__ = ["send_logs_to_file", "setup_logging"]
