"""Markdown frontmatter — one parser, two consumers (M1b).

Skills (``SKILL.md``) and agents (``<id>.md``) are both "a markdown file with
a YAML header".  There used to be one parser; the second consumer was about
to become a second parser, and two parsers for the same file are two answers
to the same question.  This module is the answer: it was moved out of
``skills/registry.py`` **verbatim**, so the behaviour the skill tests already
pin is the behaviour the agent loader gets.

The four functions are:

* ``split_frontmatter`` — ``(header, body)``; anchored at the start, because a
  ``---`` further down the file is body text, not a header.
* ``repair_header`` — quote a value that carries an unquoted second colon
  (``description: Build for AWS: ECS`` is a YAML scanner error), so the file
  can be re-read with the *same* parser rather than a second one.
* ``parse_header`` — ``(data, error)``; repairs once, then reports rather than
  swallowing.
* ``as_str`` — a frontmatter scalar as text, without YAML's quoting.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml

__all__ = ["as_str", "parse_header", "repair_header", "split_frontmatter"]

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
_COLON_VALUE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$")


def split_frontmatter(text: str) -> tuple[str, str]:
    """``(header, body)``. ``("", text)`` when there is no frontmatter block.

    Anchored at the start: a ``---`` further down the file is body text, not a
    header, and treating it as one silently emptied every field.
    """
    match = _FRONTMATTER.search(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def repair_header(header: str) -> str:
    """Quote ``key: value`` lines whose value carries an unquoted second colon.

    `description: Build for AWS: ECS` is how a skill author writes "build for
    AWS ECS" and it is a YAML scanner error. The registry used to survive this
    with a regex fallback; keeping one parser means repairing the value and
    re-reading it with the *same* parser, rather than parsing the file two
    ways.
    """
    lines: list[str] = []
    for line in header.splitlines():
        match = _COLON_VALUE.match(line)
        if match:
            prefix, key, value = match.groups()
            if ":" in value and not value.startswith(("'", '"', "[", "{")):
                line = f'{prefix}{key}: "{value}"'
        lines.append(line)
    return "\n".join(lines)


def parse_header(header: str) -> tuple[Mapping[str, Any] | None, str | None]:
    """``(data, error)``. Repairs once, then reports rather than swallowing."""
    if not header.strip():
        return {}, None
    try:
        data = yaml.safe_load(header)
    except yaml.YAMLError:
        try:
            data = yaml.safe_load(repair_header(header))
        except yaml.YAMLError as exc:
            return None, str(exc).splitlines()[0]
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"frontmatter is {type(data).__name__}, not a mapping"
    return data, None


def as_str(value: object) -> str:
    """A frontmatter scalar as text, without YAML's quoting already stripped."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()
