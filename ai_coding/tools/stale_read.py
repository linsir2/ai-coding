"""One pair of recovery sentences for the stale-read refusals.

``write`` and ``edit`` both refuse an existing file the model has not read, and
both refuse a file that changed after it was read.  The codes
(``must_read_first`` / ``stale_read``) are what the UI reads; the text is what
the model reads, so it must say what to do next instead of only what failed.
"""

from __future__ import annotations

__all__ = ["must_read_message", "stale_message"]


def must_read_message(verb: str, path: str) -> str:
    """The file exists and this session has no read for it."""
    if verb == "write":
        return (
            f"cannot write to '{path}': you must read the file first "
            "(stale-read protection). Read it now; a new file needs no read, "
            "and edit is better for a targeted change."
        )
    return (
        f"cannot {verb} '{path}': you must read the file first "
        "(stale-read protection). Read it now, then retry."
    )


def stale_message(verb: str, path: str) -> str:
    """The file changed after the read the model is working from."""
    return (
        f"cannot {verb} '{path}': file has been modified externally; "
        "re-read it first (the copy in context is from an older version)."
    )
