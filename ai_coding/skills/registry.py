"""Skill registry — one file, one parse, one object.

A skill is a directory holding a ``SKILL.md`` whose frontmatter follows the
[Agent Skills] open standard (Anthropic published it 2025-12-16; Codex and
OpenCode both implement it). This module keeps that standard's ``name`` and
``description`` meaningful while carrying the fields this project added
(``version``/``status``/``phase``/``pattern_dna``/``triggers``).

There used to be three parsers for the same file: five regexes for scalars, a
YAML pass for ``triggers`` with a regex fallback, and ``SkillTool`` re-reading
every file through two of those regexes. Three sources that can disagree, with
no way to tell which is right. :class:`Skill` is now the single answer, and
every consumer — catalog, tool, router, IPC — reads that one object.

[Agent Skills]: https://agentskills.io

Two lessons are baked in rather than rediscovered:

* **Identity is the directory name.**  OpenCode v1 keyed on the frontmatter
  ``name``, so a global and a project skill sharing a name collided silently;
  v2 moved identity to the filesystem.  Ours was already the directory name, so
  it stays — and a *different* declared ``name`` becomes an accepted alias, so a
  skill copied from another agent still loads under either spelling.
* **Unknown fields are kept, not dropped.**  Both Codex and OpenCode ship
  frontmatter their own loader never reads, so the author believes an
  instruction took effect when it did not.  Unrecognised keys land in
  :attr:`Skill.extra` where they can be seen.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_SKILL_FILE = "SKILL.md"
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)

#: The only two states a skill may declare.  Anything else reads as `active`
#: rather than inventing a third state nobody handles downstream.
SKILL_STATUSES = ("active", "archived")
DEFAULT_VERSION = "1"

#: Frontmatter keys this module reads.  Everything else goes to
#: :attr:`Skill.extra` — see the module docstring on why.
_KNOWN_KEYS = frozenset(
    {
        "name",
        "description",
        "license",
        "compatibility",
        "allowed-tools",
        "metadata",
        "disable-model-invocation",
        "version",
        "status",
        "phase",
        "pattern_dna",
        "triggers",
    }
)

#: A ``key: value`` line whose value YAML would choke on because it contains an
#: unquoted colon — `description: Build for AWS: ECS`.  Quoted on repair.
_COLON_VALUE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:\s*(.+?)\s*$")


@dataclass(frozen=True)
class SkillTriggers:
    """The four dimensions :mod:`ai_coding.core.sop_router` scores on."""

    keywords: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    task_types: tuple[str, ...] = ()
    failure_patterns: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return any((self.keywords, self.tools, self.task_types, self.failure_patterns))


@dataclass(frozen=True)
class Skill:
    """One parsed ``SKILL.md``.  Every field exists on every instance.

    That uniformity is the point: the old ``meta()`` returned four keys for a
    missing file and six for a present one, so reading ``phase`` off the answer
    was a ``KeyError`` waiting for the right input.
    """

    #: Identity — the directory name.  What the catalog prints and the tool
    #: takes, so the model is never told to call a name that does not resolve.
    id: str
    #: The body after the frontmatter, i.e. what the model actually reads.
    body: str
    #: Where it came from.
    path: Path
    description: str = ""
    #: A ``name:`` in the frontmatter that differs from the directory name.
    #: Accepted as a second spelling for loading; never a second identity.
    alias: str | None = None
    # Agent Skills standard, read and stored. Nothing acts on these yet; holding
    # them is what lets a skill round-trip between agents.
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: Agent Skills standard: the author's "do not offer this to the model".
    #: Read and acted on (r16/E-4) — the standard field whose behaviour landed.
    disable_model_invocation: bool = False
    # This project's own fields.
    version: str = DEFAULT_VERSION
    status: str = "active"
    phase: str = ""
    pattern_dna: str = ""
    triggers: SkillTriggers = field(default_factory=SkillTriggers)
    # Keys this version does not know, kept whole.
    extra: Mapping[str, Any] = field(default_factory=dict)
    #: Set when the frontmatter could not be parsed even after repair. The skill
    #: is still discovered — vanishing without trace was Codex issue #11385.
    error: str | None = None

    @property
    def name(self) -> str:
        """The one name the model uses. Alias included for tools."""
        return self.id

    @property
    def is_archived(self) -> bool:
        return self.status == "archived"

    @property
    def is_auto_invocable(self) -> bool:
        """Whether the catalog/router may offer this skill on their own.

        False for ``status: archived`` (our lifecycle) and for the standard's
        ``disable-model-invocation`` (the author's). Both stay readable by
        name — what they lose is the proactive offer, not the address.
        """
        return not self.is_archived and not self.disable_model_invocation

    @property
    def is_healthy(self) -> bool:
        return self.error is None

    def matches(self, wanted: str) -> bool:
        """Whether ``wanted`` addresses this skill — by id or by declared name."""
        return wanted == self.id or (self.alias is not None and wanted == self.alias)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """``(header, body)``. ``("", text)`` when there is no frontmatter block.

    Anchored at the start: a ``---`` further down the file is body text, not a
    header, and treating it as one silently emptied every field.
    """
    match = _FRONTMATTER.search(text)
    if not match:
        return "", text
    return match.group(1), text[match.end() :]


def _repair_header(header: str) -> str:
    """Quote ``key: value`` lines whose value carries an unquoted second colon.

    `description: Build for AWS: ECS` is how a skill author writes "build for
    AWS ECS" and is a YAML scanner error. The registry used to survive this with
    a regex fallback; keeping one parser means repairing the value and re-reading
    it with the *same* parser, rather than parsing the file two ways.
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


def _parse_header(header: str) -> tuple[Mapping[str, Any] | None, str | None]:
    """``(data, error)``. Repairs once, then reports rather than swallowing."""
    if not header.strip():
        return {}, None
    try:
        data = yaml.safe_load(header)
    except yaml.YAMLError:
        try:
            data = yaml.safe_load(_repair_header(header))
        except yaml.YAMLError as exc:
            return None, str(exc).splitlines()[0]
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"frontmatter is {type(data).__name__}, not a mapping"
    return data, None


def _as_str(value: object) -> str:
    """A frontmatter scalar as text, without YAML's quoting already stripped."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _as_tuple(value: object) -> tuple[str, ...]:
    """YAML allows ``x: single`` and ``x: [a, b]``; both mean the same here."""
    if not value:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _as_bool(value: object) -> bool:
    """A YAML boolean as a bool; the loose spellings an author may write too.

    YAML gives ``true``/``1`` as real booleans; ``'yes'``/``'on'`` arrive as
    strings and mean the same thing to every runtime that reads this field.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "on", "1"}
    return False


def _parse_triggers(value: object) -> SkillTriggers:
    if not isinstance(value, dict):
        return SkillTriggers()
    return SkillTriggers(
        keywords=_as_tuple(value.get("keywords")),
        tools=_as_tuple(value.get("tools")),
        task_types=_as_tuple(value.get("task_types")),
        failure_patterns=_as_tuple(value.get("failure_patterns")),
    )


def parse_skill(path: Path | str) -> Skill:
    """Parse one ``SKILL.md``. Never raises: a bad file is a skill with an
    ``error``, because a skill that silently disappears cannot be debugged."""
    target = Path(path)
    skill_id = target.parent.name
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return Skill(id=skill_id, body="", path=target, error=str(exc))
    header, body = _split_frontmatter(text)
    data, error = _parse_header(header)
    if data is None:
        return Skill(id=skill_id, body=body, path=target, error=error)

    declared = _as_str(data.get("name"))
    status = (_as_str(data.get("status")) or "").casefold()
    metadata = data.get("metadata")
    return Skill(
        id=skill_id,
        body=body.strip(),
        path=target,
        description=_as_str(data.get("description")),
        alias=declared or None if declared != skill_id else None,
        license=_as_str(data.get("license")) or None,
        compatibility=_as_str(data.get("compatibility")) or None,
        allowed_tools=_as_tuple(data.get("allowed-tools")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
        disable_model_invocation=_as_bool(data.get("disable-model-invocation")),
        version=_as_str(data.get("version")) or DEFAULT_VERSION,
        status=status if status in SKILL_STATUSES else "active",
        phase=_as_str(data.get("phase")),
        pattern_dna=_as_str(data.get("pattern_dna")),
        triggers=_parse_triggers(data.get("triggers")),
        extra={k: v for k, v in data.items() if k not in _KNOWN_KEYS},
        error=error,
    )


class SkillRegistry:
    """A skills directory, parsed once per revision.

    Cached on ``(mtime, size)``: the catalog is rebuilt on every request but
    re-reading every ``SKILL.md`` each time is both slow and, worse, what made
    the "byte-stable within a session" comment in the old ``meta()`` untrue.
    """

    def __init__(
        self, skill_dir: str | Path | Sequence[str | Path] = "skills"
    ) -> None:
        """``skill_dir`` is one root or several, **lowest precedence first**.

        Two roots is the normal shape (M9): the user's global library, then the
        project's. Later wins, which is the ``AGENTS.md`` convention — the
        nearer scope makes the more specific claim. Claude Code inverts this and
        lets the personal layer override the repository's, which would let a
        checked-out repo rewrite the tools and conventions of the person running
        it.
        """
        if isinstance(skill_dir, (str, Path)):
            roots = [Path(skill_dir)]
        else:
            roots = [Path(root) for root in skill_dir]
        self.roots: tuple[Path, ...] = tuple(roots)
        #: The highest-precedence root — where ``write`` goes by default, and
        #: what ``.pending`` is staged under.  A skill you write while working
        #: on a repo belongs to that repo until you deliberately promote it.
        self.root = self.roots[-1] if self.roots else Path("skills")
        self._cache: dict[str, tuple[tuple[float, int], Skill]] = {}
        self._owner: dict[str, int] = {}

    def _locate(self, name: str) -> Path | None:
        """The ``SKILL.md`` for ``name`` from the highest-precedence root."""
        for root in reversed(self.roots):
            candidate = root / name / _SKILL_FILE
            if candidate.is_file():
                return candidate
        return None

    def _reindex(self) -> None:
        """Record which root each name came from, **highest precedence winning**.

        Later roots overwrite earlier ones, which is what ``_locate`` does by
        walking the roots in reverse. The two must agree or ``shadowed()``
        would report the wrong side of every collision.
        """
        self._owner = {}
        for index, root in enumerate(self.roots):
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.name.startswith("."):
                    continue
                if child.is_dir() and (child / _SKILL_FILE).is_file():
                    self._owner[child.name] = index

    # ---- discovery --------------------------------------------------------

    def names(self) -> list[str]:
        """Directory names holding a ``SKILL.md``, sorted, across all roots."""
        self._reindex()
        return sorted(self._owner)

    @property
    def is_empty(self) -> bool:
        return not self.names()

    # ---- access ------------------------------------------------------------

    def shadowed(self) -> list[tuple[str, str]]:
        """``(name, losing_scope)`` for every name defined in more than one root.

        A collision is resolved by precedence, but saying so is the difference
        between a user who can find out why the skill they installed is not the
        one running, and one who cannot. OpenCode v2 resolves the same
        collision with a bare ``map.set()`` and logs nothing.
        """
        self._reindex()
        scopes = (
            ("user", "project")
            if len(self.roots) <= 2
            else tuple(str(root) for root in self.roots)
        )
        found: list[tuple[str, str]] = []
        for root_index, root in enumerate(self.roots):
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.name.startswith("."):
                    continue
                if not (child.is_dir() and (child / _SKILL_FILE).is_file()):
                    continue
                if self._owner.get(child.name) == root_index:
                    continue
                found.append((child.name, scopes[min(root_index, len(scopes) - 1)]))
        return found

    def get(self, name: str) -> Skill | None:
        """One skill by directory name, parsed and cached. ``None`` if absent."""
        target = self._locate(name)
        if target is None:
            self._cache.pop(name, None)
            return None
        try:
            stat = target.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime, stat.st_size)
        cached = self._cache.get(name)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        skill = parse_skill(target)
        self._cache[name] = (stamp, skill)
        return skill

    def resolve(self, wanted: str) -> Skill | None:
        """One skill by id *or* by its declared alias, across every root."""
        direct = self.get(wanted)
        if direct is not None:
            return direct
        for name in self.names():
            skill = self.get(name)
            if skill is not None and skill.matches(wanted):
                return skill
        return None

    def all(self) -> tuple[Skill, ...]:
        """Every skill, in id order. Broken ones are included and say so."""
        found = [self.get(name) for name in self.names()]
        return tuple(skill for skill in found if skill is not None)

    def loadable(self) -> tuple[Skill, ...]:
        """Skills that parse. The ones that do not are broken, not hidden."""
        return tuple(skill for skill in self.all() if skill.is_healthy)

    # ---- write (M1) ---------------------------------------------------------

    def write(self, name: str, content: str, *, root: str | Path | None = None) -> Path | None:
        """Atomically write ``skills/<name>/SKILL.md``.  Returns path or None.

        Refuses names that would escape the skill directory (``/``, ``..``).
        Uses ``os.replace`` so a crash mid-write leaves the old file intact
        instead of a truncated one (same contract as ``MemoryStore._save``).
        """
        if not name or "/" in name or ".." in name or len(name) > 80:
            return None
        base = Path(root) if root is not None else self.root
        target = base / name / _SKILL_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
            self._cache.pop(name, None)
            return target
        except OSError:
            return None


__all__ = [
    "DEFAULT_VERSION",
    "SKILL_STATUSES",
    "Skill",
    "SkillRegistry",
    "SkillTriggers",
    "parse_skill",
]


#: ``@name`` — what the TUI writes into the composer when a skill is picked from
#: the panel, and what OpenCode writes for the same gesture.
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9._-]*)")


def resolve_mentions(text: str, registry: SkillRegistry | None) -> list[str]:
    """The skills a human explicitly pointed at, in the order they wrote them.

    An unknown name is not a reference: ``cc @alice`` is prose, and turning it
    into a tool call would hand the model a name it cannot load. Resolution
    reuses ``registry.get``, which already answers by id *or* declared alias, so
    a skill is reachable by both spellings without a second parser.

    Archived skills resolve. Archived means "do not offer this proactively", and
    naming one is the opposite of proactive.
    """
    if not text or registry is None:
        return []
    found: list[str] = []
    for mention in _MENTION.findall(text):
        skill = registry.resolve(mention)
        if skill is not None and skill.id not in found:
            found.append(skill.id)
    return found
