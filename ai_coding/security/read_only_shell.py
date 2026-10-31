"""The read-only shell subset plan mode may run.

Plan mode needs `git log` / `find` / `wc`; ``read/grep/glob`` cannot answer those.
Opening ``bash`` would open every command, so this module admits **exactly one
simple argv command** from a small allowlist, with no shell syntax around it:

* no redirection, pipe, `;`, `&&`, `||`, background `&`, command substitution
  (``$()``/backticks), variable expansion or assignment, ``~``;
* the program must be one of a read-only set (``git`` subcommands are further
  restricted; ``find`` cannot delete/exec);
* anything not understood is refused (default deny).

The tool still runs through the same process sandbox as ``bash`` and rejects
absolute/home paths outside the workspace.
"""

from __future__ import annotations

import re

from ai_coding.shell_parse import command_views, parses_cleanly

__all__ = ["read_only_problem"]

#: Programs that only read.  Deliberately small: content readers (`cat`,
#: `grep`, `sed`, `awk`) are absent because they would bypass the read tool's
#: credential rules; plan mode already has read/grep for content.
_ALLOWED = frozenset(
    {
        "git",
        "ls",
        "pwd",
        "wc",
        "find",
        "stat",
        "file",
        "tree",
        "du",
        "df",
    }
)

_GIT_SUBCOMMANDS = frozenset(
    {"status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame"}
)
#: Options that write, change refs, or point git at another repository.
_GIT_DENY_FLAGS = frozenset(
    {
        "-c",
        "-C",
        "--git-dir",
        "--work-tree",
        "--output",
        "--textconv",
        "--ext-diff",
        "--exec-path",
        "--upload-pack",
        "--receive-pack",
        "--config",
    }
)
_GIT_BRANCH_FLAGS = frozenset({"-a", "-r", "-v", "--all", "--remotes", "--list"})
_GIT_BRANCH_WRITERS = frozenset({"-d", "-D", "-m", "-M", "-f", "--force"})
_FIND_DENY_FLAGS = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
)
_OUTPUT_FLAGS = frozenset({"-o", "--output", "-O"})


_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def read_only_problem(command: str) -> str:
    """Why ``command`` is not a read-only simple command (``""`` = allowed)."""
    text = (command or "").strip()
    if not text:
        return "empty command"
    if "\n" in text or "\r" in text:
        return "only one line is allowed"
    if _ASSIGNMENT_RE.match(text):
        return "variable assignment is not allowed"
    if any(marker in text for marker in ("$", "`", "~")):
        return "variable/command expansion is not allowed"
    if any(op in text for op in (">", "<", "|", ";", "&&", "&")):
        return "shell operators (pipe/redirection/chaining) are not allowed"
    if not parses_cleanly(text):
        return "this is not a shell command the grammar can read"
    views = command_views(text)
    if len(views) != 1:
        return "exactly one simple command is allowed"
    view = views[0]
    if view.redirects:
        return "redirection is not allowed"
    program = (view.program or "").strip()
    if program not in _ALLOWED:
        return f"'{program or '?'}' is not a read-only command"
    arguments = tuple(str(argument) for argument in view.arguments)
    if "../" in text or any(
        argument == ".." or argument.startswith("../") for argument in arguments
    ):
        return "relative parent paths are not allowed"
    if program == "git":
        return _git_problem(arguments)
    if program == "find":
        return _find_problem(arguments)
    if program == "tree":
        return "tree output files are not allowed" if any(
            flag in arguments for flag in _OUTPUT_FLAGS
        ) else ""
    return ""


def _git_problem(arguments: tuple[str, ...]) -> str:
    if not arguments:
        return "git needs a read-only subcommand"
    subcommand = arguments[0]
    if subcommand not in _GIT_SUBCOMMANDS:
        return f"git {subcommand} is not read-only"
    for argument in arguments[1:]:
        if argument in _GIT_DENY_FLAGS:
            return f"git option {argument} can write or redirect"
        if any(
            flag.startswith("--") and argument.startswith(flag + "=")
            for flag in _GIT_DENY_FLAGS
        ):
            return f"git option {argument} can write or redirect"
        if subcommand == "branch" and argument in _GIT_BRANCH_WRITERS:
            return "git branch may not create, move or delete branches"
    if subcommand == "branch":
        # Listing (no argument) or filtering flags only; a positional argument
        # creates a branch.
        if any(not argument.startswith("-") for argument in arguments[1:]):
            return "git branch with a name would create it"
        if any(argument not in _GIT_BRANCH_FLAGS for argument in arguments[1:]):
            return "that git branch option is not a listing option"
    return ""


def _find_problem(arguments: tuple[str, ...]) -> str:
    for argument in arguments:
        if argument in _FIND_DENY_FLAGS:
            return f"find {argument} can write or run a command"
    return ""
