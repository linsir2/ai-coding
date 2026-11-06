"""Filesystem helpers mirroring the original ``FileUtils``. UTF-8 throughout."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path


class FileConflict(Exception):
    """The file changed after the caller computed its new content.

    Raised by :func:`write_text_atomic` when an ``expected_version`` no longer
    matches the file on disk.  Distinct from ``OSError`` so a tool can answer
    ``stale_read`` instead of reporting a plain write failure.
    """


def read_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_file(path: str | Path, content: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def write_text_atomic(
    path: str | Path, content: str, *, expected_version: str | None = None
) -> None:
    """Replace ``path`` with ``content`` without ever exposing a partial file.

    The bytes go to a temp file in the same directory (so ``os.replace`` is an
    atomic rename), get flushed and fsynced, and inherit the original mode --
    ``mkstemp`` creates 0600, which would silently strip a script's executable
    bit.  ``expected_version`` closes the read-then-write window: a file that
    changed since the caller computed ``content`` raises :class:`FileConflict`
    and the temp file is discarded, leaving the other writer's bytes intact.

    The only trace a crash can leave is a stray ``.<name>.*.tmp`` beside the
    target; the target itself is always either the old file or the new one.
    """
    target = Path(path)
    try:
        mode_bits = stat.S_IMODE(target.stat().st_mode)
    except OSError:
        mode_bits = 0o644
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode_bits)
        if expected_version is not None and file_version(target) != expected_version:
            raise FileConflict(f"{target} changed while it was being edited")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def append_to_file(path: str | Path, content: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")


def exists(path: str | Path) -> bool:
    return Path(path).exists()


def create_directories(path: str | Path) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def list_files(path: str | Path) -> list[Path]:
    p = Path(path)
    return [x for x in p.iterdir() if x.is_file()] if p.is_dir() else []


def list_files_recursive(path: str | Path) -> list[Path]:
    p = Path(path)
    return [x for x in p.rglob("*") if x.is_file()] if p.is_dir() else []


def delete_recursive(path: str | Path) -> None:
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def copy(src: str | Path, dst: str | Path) -> None:
    source, target = Path(src), Path(dst)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def get_file_size(path: str | Path) -> int:
    return Path(path).stat().st_size


def file_version(path: str | Path) -> str:
    """A cheap file identity: ``mtime_ns:size:inode``; ``""`` when stat fails."""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"


def get_file_extension(name: str) -> str:
    return Path(name).suffix.lstrip(".")


def file_name_without_extension(name: str) -> str:
    return Path(name).stem
