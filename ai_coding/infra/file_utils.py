"""Filesystem helpers mirroring the original ``FileUtils``. UTF-8 throughout."""

from __future__ import annotations

import shutil
from pathlib import Path


def read_file(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_file(path: str | Path, content: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


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


def get_file_extension(name: str) -> str:
    return Path(name).suffix.lstrip(".")


def file_name_without_extension(name: str) -> str:
    return Path(name).stem
