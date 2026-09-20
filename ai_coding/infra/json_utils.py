"""JSON helpers mirroring the original ``JsonUtils``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar, cast

T = TypeVar("T")


def to_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def to_json_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def from_json(text: str, as_type: type[T] | None = None) -> T:
    """Parse JSON and cast to ``as_type`` (or leave untyped when omitted)."""
    del as_type  # cast is decided by the caller's annotation
    return cast(T, json.loads(text))


def pretty_print(text: str) -> str:
    return json.dumps(json.loads(text), ensure_ascii=False, indent=2)


def is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except (ValueError, TypeError):
        return False


def to_json_file(path: str | Path, obj: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(to_json_pretty(obj), encoding="utf-8")


def from_json_file(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
