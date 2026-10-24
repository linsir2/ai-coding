"""Shared fixtures and helpers for the test suite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def refused(monkeypatch):
    """Make one path's OS call raise EACCES -- portably.

    ``chmod`` cannot produce a refusal for root, and root is the normal
    container user, so the refusal is injected at the boundary the code
    handles instead of at the filesystem.  Path-guarded, so nothing else's
    unlink/write_text is affected; ``monkeypatch`` undoes it at test end.

    Usage::

        def test_x(tmp_path, refused):
            with refused(locked / "kept.txt"):
                ...
    """

    @contextmanager
    def _refused(target: Path, attr: str = "unlink") -> Iterator[None]:
        cls = type(target)
        real = getattr(cls, attr)

        def refused(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            if self == target:
                raise PermissionError(13, "Permission denied")
            return real(self, *a, **kw)

        monkeypatch.setattr(cls, attr, refused)
        yield

    return _refused
