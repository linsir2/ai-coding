"""Credential-path vocabulary shared by every reader and tool.

The same file can be reached through Bash, ``read``, or ``grep``.  When those
three paths keep separate lists, a file that is a secret on one path is an
ordinary read on another.  This module owns the names, suffixes, and path
shapes once; callers decide whether the answer is a hard refusal, a question,
or a filter.

The vocabulary is about *files*, not command syntax.  ``.env.example`` is an
explicit exception because it is documentation, not a secret container.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_coding.security.permissions import Rule, Ruleset

__all__ = [
    "CREDENTIAL_DIRS",
    "CREDENTIAL_NAMES",
    "CREDENTIAL_PATTERNS",
    "CREDENTIAL_SUFFIXES",
    "credential_match",
    "credential_exclude_globs",
    "credential_rules",
    "is_credential_path",
]

#: Exact basenames that are credentials wherever they appear.
CREDENTIAL_NAMES: frozenset[str] = frozenset(
    {".env", ".netrc", ".npmrc", ".pypirc", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
)

#: Suffixes that are credentials wherever they appear.
CREDENTIAL_SUFFIXES: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".keystore")

#: Directory markers whose whole subtree is credential-bearing.
CREDENTIAL_DIRS: frozenset[str] = frozenset({".ssh", ".aws", ".gnupg", ".kube"})

#: Path shapes that are credentials even when their basename is ordinary.
CREDENTIAL_PATTERNS: tuple[str, ...] = (
    "/.config/gh/",
    "/.docker/config.json",
    "/etc/shadow",
    "/etc/sudoers",
)


def _normalise(path: str | Path) -> str:
    return posixpath.normpath(str(path).replace("\\", "/"))


def credential_match(path: str | Path, *, workspace: str | Path | None = None) -> str:
    """The credential shape this path names, or ``""``.

    ``workspace`` is optional for callers that already hold an absolute or
    already-resolved path.  When supplied, a relative path is resolved against
    it as well; this is what catches ``config.txt -> .env`` before a grant is
    offered.

    A path that cannot be resolved is treated as a credential (fail-closed):
    ``grep``'s ``_is_credential_entry`` already refuses on ``OSError``, and an
    unresolvable name must not become readable by being unresolvable.
    """
    text = _normalise(path)
    if not text:
        return ""
    name = posixpath.basename(text)
    if name.endswith(".env.example"):
        return ""
    if name in CREDENTIAL_NAMES or name.endswith(".env") or name.startswith(".env."):
        return name
    if any(name.endswith(suffix) for suffix in CREDENTIAL_SUFFIXES):
        return name

    absolute_shape = "/" + text.lstrip("/")
    for pattern in CREDENTIAL_PATTERNS:
        if pattern in absolute_shape:
            return pattern.strip("/")
    if any(part in CREDENTIAL_DIRS for part in absolute_shape.split("/")):
        return next(part for part in absolute_shape.split("/") if part in CREDENTIAL_DIRS)

    if workspace is not None:
        base = Path(workspace).expanduser()
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            return text or "unresolvable"
        if resolved != candidate:
            return credential_match(resolved)
    return ""


def is_credential_path(path: str | Path, *, workspace: str | Path | None = None) -> bool:
    """Whether ``path`` names a credential under the shared vocabulary."""
    return bool(credential_match(path, workspace=workspace))


def credential_exclude_globs() -> tuple[str, ...]:
    """Ripgrep glob rules that keep credential files out of the walk.

    Pure-``!`` exclusions only: no ``**/*`` base (under rg 15.1.0
    last-match-wins that bare include re-opens what ``.gitignore`` hid,
    resurrecting e.g. ``x.log``) and no ``**/.env.example`` re-include.
    ``.env.example`` stays searchable through an explicit per-file rescue in
    ``grep_tool`` (tree search walks each ``.env.example`` without ``--glob``,
    so the whitelist pattern never engages).

    The caller still filters returned paths through :func:`is_credential_path`,
    because a symlink alias can carry an ordinary visible name.  A direct
    search of an explicit non-credential file (notably ``.env.example``)
    skips these tree globs; an explicit credential file already needs human
    approval before reaching the runner.
    """
    globs = [f"!**/{name}" for name in sorted(CREDENTIAL_NAMES)]
    globs += [
        "!**/*.env",
        "!**/.env*",
        *[f"!**/*{suffix}" for suffix in CREDENTIAL_SUFFIXES],
        *[f"!**/{directory}/**" for directory in sorted(CREDENTIAL_DIRS)],
        "!**/.config/gh/*",
        # Exact file: the old ``*`` suffix over-matched (``config.json.bak``).
        "!**/.docker/config.json",
        # Exact files: trailing ``*`` would also hide ``shadow.bak`` etc.
        "!**/etc/shadow",
        "!**/etc/sudoers",
    ]
    return tuple(globs)


def credential_rules(actions: tuple[str, ...]) -> Ruleset:
    """ASK rules for file-tool actions over the shared credential vocabulary.

    ``.env.example`` is appended last because rule evaluation is
    last-match-wins and it has to stay readable.
    """
    from ai_coding.security.permissions import Effect, Rule

    resources = [f"*{name}" for name in sorted(CREDENTIAL_NAMES)] + [
        "*.env",
        "*.env.*",
        *[f"*{suffix}" for suffix in CREDENTIAL_SUFFIXES],
    ]
    resources.extend(f"*{directory}/*" for directory in sorted(CREDENTIAL_DIRS))
    resources.extend(f"*{directory}" for directory in sorted(CREDENTIAL_DIRS))
    resources.extend(f"*{pattern.lstrip('/')}" for pattern in CREDENTIAL_PATTERNS)
    resources.extend(
        f"*{pattern.lstrip('/')}*" for pattern in CREDENTIAL_PATTERNS if pattern.endswith("/")
    )

    rules: list[Rule] = []
    for action in actions:
        rules.extend(Rule(action=action, resource=value, effect=Effect.ASK) for value in resources)
        rules.append(Rule(action=action, resource="*.env.example", effect=Effect.ALLOW))
    return tuple(rules)
