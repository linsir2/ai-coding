"""The bash red lines, read as commands (W2/T).

A red line is a property of a *command*, not of a line of text.  The
reading this replaces had two units, and neither was the command:

* a substring table over the whole text -- so ``echo shutdown`` was a
  shutdown, and ``echo x >/dev/sda`` was not a disk write because the
  pattern wanted a space;
* a shape check over a flat word list -- so only the *first* command in
  ``a && rm -rf /`` was ever looked at, and the rest survived on the
  substring table's luck.

The shell grammar has the right unit: a command's program name, its
arguments, and the targets of its redirections.  Every rule below says
which of those it reads.

Two rules are about the *program* (sudo, shutdown, reboot, mkfs), three
about the program *and* its arguments (rm, chmod, dd), one about a
*redirection target* (the raw disk), one about a *subcommand and its
flags* (git push --force), and one about the *paths* the command touches
(the credentials).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ai_coding.security.credentials import credential_match
from ai_coding.shell_parse import CommandView, command_shape, command_views

__all__ = ["bash_red_line"]

#: The wrapper commands that run another program.  ``env sudo ls`` is a
#: sudo, and so is ``nohup sudo ls``; the program the shell runs is the
#: one after the wrapper.
_WRAPPERS = frozenset(
    {
        "command",
        "doas",
        "env",
        "exec",
        "ionice",
        "nice",
        "nohup",
        "setsid",
        "stdbuf",
        "time",
        "timeout",
        "xargs",
    }
)

#: The top-level directories that belong to the system rather than to the
#: work.  A recursive delete aimed at one of them is a red line: the
#: operator did not mean to empty ``/etc``.
_SYSTEM_ROOTS = frozenset(
    {
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/opt",
        "/root",
        "/sbin",
        "/usr",
        "/var",
    }
)

#: The block devices a write to is a red line.  ``/dev/zero`` and
#: ``/dev/null`` are not among them: they are character devices, and
#: reading from one or writing to a file is ordinary work.
_BLOCK_DEVICE_RE = re.compile(
    r"^/dev/(sd[a-z]*|hd[a-z]*|vd[a-z]*|xvd[a-z]*|nvme\d+n\d+|rdisk\d+|disk\d+|mmcblk\d+|mapper/)"
)

#: The git options that take a value, and so push the subcommand further
#: along: ``git -C /tmp push --force`` is a force push.
_GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--config-env", "--exec-path", "--git-dir", "--namespace", "--work-tree"}
)

#: A leading ``~`` is the home directory wherever it is written.
_HOME_PREFIX = re.compile(r"^~(?=/|$)")


def _basename(program: str) -> str:
    """The program's name without its directory (``/bin/rm`` is ``rm``)."""
    return Path(program).name.lower()


def _programs(view: CommandView) -> list[str]:
    """The programs the command runs, wrappers peeled off.

    ``env sudo ls`` runs sudo; ``sudo`` is a wrapper here and ``ls`` is
    not, so the chain stops at the first name that is not a wrapper.
    """
    names: list[str] = []
    for word in (view.program, *view.arguments):
        name = _basename(word)
        if not name:
            continue
        names.append(name)
        if name not in _WRAPPERS:
            break
    return names


def _runs(view: CommandView, program: str) -> bool:
    """Does this command run ``program``?"""
    return program in _programs(view)


def _flags(view: CommandView) -> set[str]:
    """The flag letters the command carries.

    ``-r`` and ``--recursive`` both mean recursive; a long flag
    contributes its first letter, which is how the spelling of a flag
    stops mattering.
    """
    flags: set[str] = set()
    for argument in view.arguments:
        if argument == "--":
            continue
        if argument.startswith("--") and len(argument) > 2:
            flags.add(argument[2].lower())
        elif argument.startswith("-") and len(argument) > 1:
            flags.update(argument[1:].lower())
    return flags


def _targets(view: CommandView) -> list[str]:
    """The paths the command names, and what it deletes.

    Only the words that look like paths count: ``rm -rf ./build`` names
    one, and ``rm -rf mydir`` names a relative directory the shell would
    resolve against the workspace.
    """
    paths: list[str] = []
    for argument in view.arguments:
        if argument.startswith("-"):
            continue
        paths.append(argument)
    return paths


def _is_root_target(path: str) -> bool:
    """Is this path the filesystem root, or a top-level system directory?"""
    normalized = path.rstrip("/") or "/"
    if normalized in ("/", "/*"):
        return True
    return normalized in _SYSTEM_ROOTS


def _is_block_device(path: str) -> bool:
    """Is this a block device -- a disk, not a character device?"""
    return bool(_BLOCK_DEVICE_RE.match(path))


def _reads_credential(view: CommandView) -> str:
    """The credential this command touches, or ``""``.

    A word is a path if it looks like one; the home directory is expanded
    first, so ``~/.ssh`` and ``/home/you/.ssh`` compare equal.  Matching
    is on the *word*, so ``cat .env.example`` is not ``cat .env``.
    """
    for word in view.arguments:
        candidate = Path(_HOME_PREFIX.sub(str(Path.home()), word))
        secret = credential_match(candidate)
        if secret:
            return secret
    return ""


def _git_push_is_forced(view: CommandView) -> bool:
    """Is this a ``git push`` with a force flag?

    The subcommand is not always the first argument: ``git -C /tmp push
    --force`` has an option that takes a value in front of it, and taking
    it as the subcommand would read the force push as an ordinary push.
    """
    words = view.arguments
    index = 0
    while index < len(words):
        word = words[index]
        if word in _GIT_VALUE_OPTIONS:
            index += 2
            continue
        if word.startswith("-"):
            index += 1
            continue
        break
    if index >= len(words) or words[index] != "push":
        return False
    rest = words[index + 1 :]
    return any(word in ("--force", "-f") for word in rest)


def _dd_touches_a_disk(view: CommandView) -> bool:
    """Does this ``dd`` read or write a block device?

    ``dd``'s arguments are words that carry their own name
    (``if=/dev/zero``); there is no syntax for the name, so it is read
    off the word.
    """
    for argument in view.arguments:
        name, _, value = argument.partition("=")
        if name not in ("if", "of"):
            continue
        if _is_block_device(value):
            return True
    return False


#: The rules, in the order they are read.  Each one answers "may this
#: command not run at all?", and the first that says yes is the reason.
#:
#: The reason names the shape that triggered it, the way the old table
#: did: the operator reads the refusal and has to see *what* was refused.
_RULES: tuple[tuple[str, Callable[[CommandView], bool]], ...] = (
    (
        "recursive root deletion",
        lambda view: _runs(view, "rm")
        and {"r", "f"} <= _flags(view)
        and any(_is_root_target(path) for path in _targets(view)),
    ),
    (
        "world-writable permissions",
        lambda view: _runs(view, "chmod")
        and any(
            word in ("777", "0777", "a+rwx", "a+w", "+rwx") for word in view.arguments
        ),
    ),
    (
        "sudo escalation",
        lambda view: _runs(view, "sudo"),
    ),
    (
        "system shutdown",
        lambda view: _runs(view, "shutdown"),
    ),
    (
        "system reboot",
        lambda view: _runs(view, "reboot"),
    ),
    (
        "filesystem format",
        lambda view: any(
            name == "mkfs" or name.startswith("mkfs.") for name in _programs(view)
        ),
    ),
    (
        "disk write",
        lambda view: _runs(view, "dd") and _dd_touches_a_disk(view),
    ),
    (
        "raw disk write",
        lambda view: any(_is_block_device(target) for target in view.redirects),
    ),
    (
        "force git push",
        lambda view: _runs(view, "git") and _git_push_is_forced(view),
    ),
)


def bash_red_line(command: str) -> str:
    """Why this command may not run at all, or ``""`` when it may be asked.

    ``command`` is a shell command line.  A command the grammar cannot
    read is not judged here -- the caller falls back to the text's own
    reading, so a broken line is judged by the old table rather than
    waved through.

    The reason names the shape that triggered it, the way the old table
    did: the operator reads the refusal and has to see *what* was refused.
    """
    views = command_views(command)
    if not views or any(view.broken for view in views):
        return ""
    for view in views:
        secret = _reads_credential(view)
        if secret:
            return f"credential file ({secret})"
        for reason, rule in _RULES:
            if rule(view):
                return f"{reason} ({command_shape(view)})"
    return ""
