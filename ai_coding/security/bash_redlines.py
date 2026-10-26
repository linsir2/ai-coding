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

**Where the two readings meet.**  The grammar is the first reading, but it
is not the only one: a line it cannot finish is judged by the *text* reading
that existed before it -- a substring table and a flat word list.  That
fallback used to live in ``permission_gate``, which meant the same red line
(``rm -rf /``, ``chmod 777``) was decided in two places that only an oral
agreement kept in step.  It lives here now, and ``bash_denied`` delegates to
:func:`bash_red_line_with_fallback`.  ``bash_red_line`` keeps its contract --
the pure grammar layer, ``""`` for a line it cannot read -- so a caller that
needs to tell "the grammar read this" from "it did not" still can.

The fallback is the conservative direction and nothing more: it refuses what
the old table refused and never less.  It is a second reading of the same
question, not a second opinion about it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from ai_coding.security.credentials import credential_match
from ai_coding.shell_parse import (
    CommandView,
    command_shape,
    command_views,
    command_words,
    parses_cleanly,
)

__all__ = ["bash_red_line", "bash_red_line_with_fallback"]

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

    ``command`` is a shell command line.  This is the **grammar layer only**:
    a command the grammar cannot read is not judged here -- it returns ``""``
    and leaves the line to :func:`bash_red_line_with_fallback`, which reads
    the text the way it did before the grammar existed.  A caller that needs
    to tell the two apart (``_grammar_reads``) can, and a caller that does
    not should call the ``with_fallback`` function.

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


#: Hard deny patterns for bash commands, the text reading's substring table.
#:
#: Matched against the *normalized* command (:func:`_normalize_command`), not
#: the raw text.  The table used to be applied with ``pattern in command``,
#: and a space was enough to walk past it: ``rm  -rf /``, ``rm -r -f /``,
#: ``RM -RF /`` and ``chmod  777`` all returned WARN.  A red line a model
#: can defeat by respelling it is worse than no red line, because the first
#: refusal teaches the spelling that works.
_BASH_HARD_DENY_PATTERNS: list[tuple[str, str]] = [
    ("rm -rf /", "recursive root deletion"),
    ("sudo ", "sudo escalation"),
    ("shutdown", "system shutdown"),
    ("reboot", "system reboot"),
    ("mkfs", "filesystem format"),
    ("dd if=", "disk write"),
    ("> /dev/sd", "raw disk write"),
    ("git push --force", "force git push"),
    ("git push -f ", "force git push (short)"),
    ("chmod 777", "world-writable permissions"),
]

#: Shell metacharacters that end a word; a path token is read between them.
_SHELL_SPLIT = re.compile(r"[\s;|&<>()'\"`]+")


def _normalize_command(command: str) -> str:
    """Collapse the spellings that let a red line be walked past.

    Runs of whitespace become one space, so ``rm  -rf  /`` normalizes to
    ``rm -rf /``.  Nothing else is rewritten: the table is matched against this
    text as a whole, and collapsing whitespace is the only normalization that
    cannot change which command runs.
    """
    return re.sub(r"\s+", " ", command).strip()


def _command_words(command: str) -> list[str]:
    """The command's words, ``~`` expanded, for shape-based matching.

    ``rm -r -f /`` and ``rm -rf /`` are different word lists but the same
    command, so :func:`_dangerous_recursive_root_delete` compares the *set* of
    flags rather than the string.

    Two readings of the same command, and the grammar's wins when it has
    one.  A text split cannot tell a command from its prefix -- in
    ``FOO=bar rm -rf /`` it makes ``FOO=bar`` the program's name, and the
    red line is missed; the grammar knows the assignment is a prefix and the
    command is the rest.  When the grammar has nothing to say (the text is
    not a command at all), the text's words are what there is.
    """
    words = command_words(command)
    if not words:
        words = _SHELL_SPLIT.split(command)
    return [_HOME_PREFIX.sub(str(Path.home()), word) for word in words if word]


def _dangerous_recursive_root_delete(words: list[str]) -> bool:
    """``rm`` with recursive+force aimed at ``/``, whatever the flag spelling.

    Every ordering and every amount of whitespace collapses to the same two
    facts: the program is ``rm``, it carries both ``-r`` and ``-f`` (in any
    grouping), and the target is the filesystem root.
    """
    if not words:
        return False
    program = Path(words[0]).name.lower()
    if program != "rm":
        return False
    flags: set[str] = set()
    targets: list[str] = []
    for word in words[1:]:
        if word.startswith("-") and word != "--":
            flags.update(word[1:].lower())
        elif word in ("--recursive", "--force"):
            flags.update("r" if word == "--recursive" else "f")
        else:
            targets.append(word)
    return "r" in flags and "f" in flags and any(t.rstrip("/") in ("", "/") for t in targets)


def _world_writable_chmod(words: list[str]) -> bool:
    """``chmod 777`` in any spelling, including octal written out long."""
    if not words or Path(words[0]).name.lower() != "chmod":
        return False
    return any(word in ("777", "0777", "a+rwx", "a+w", "+rwx") for word in words[1:])


def _reads_credential_words(words: list[str]) -> str:
    """The credential this command touches, or ``""`` -- the *text* reading.

    Named ``_reads_credential_words`` and not ``_reads_credential`` on
    purpose: the grammar layer's own :func:`_reads_credential` takes a
    ``CommandView``.  Two functions with one name in one module is how the
    grammar layer's rule came to iterate a ``CommandView`` as if it were a
    word list, which would have taken the whole red line set down with it.

    A word is a path if it looks like one (starts at ``/`` or ``~``, or carries
    a suffix); the home directory is expanded first so ``~/.ssh`` and
    ``/home/you/.ssh`` compare equal.  Matching is on the *word*, so
    ``cat .env.example`` is not ``cat .env`` -- the token has to be the file.
    """
    for word in words[1:]:
        secret = credential_match(word)
        if secret:
            return secret
    return ""


def _grammar_reads(command: str) -> bool:
    """Did the shell grammar read this command all the way through?

    When it did, the rules above are the whole verdict -- there is nothing
    left for the text's own reading to add.  When it did not, the text's
    reading is the fallback.

    A line with no command in it -- a comment, a bare assignment -- was
    read all the way through: there was nothing in it to read.  That is
    different from a line the grammar could not finish, and the difference
    matters: ``# rm -rf /`` is a comment, and the old table's verdict on
    it was a refusal of a line that would never run.
    """
    return parses_cleanly(command)


def bash_red_line_with_fallback(command: str) -> str:
    """Why this bash command may not run at all, or ``""`` when it may be asked.

    The one judge for a bash command.  The verdict is the shell grammar's
    (:func:`bash_red_line`): a red line is a property of a *command*, so
    ``a && rm -rf /`` is read as two commands and the second one is the
    dangerous one, and ``echo "rm -rf /"`` is read as a string and is not a
    command at all.

    A command the grammar cannot read falls back to the text reading -- the
    substring table and the flat word list, which live beside the grammar
    rules in this module.  That fallback is the conservative direction: a
    broken line is judged by the rules that existed before, not waved through.

    The dispatcher and the gate share this, so the approval panel and the
    refusal can never disagree about what is forbidden.  It reads no clock
    and no file; the shell grammar is loaded once per process and kept.
    """
    reason = bash_red_line(command)
    if reason:
        return reason
    if _grammar_reads(command):
        return ""
    normalized = _normalize_command(command)
    words = _command_words(normalized)
    if _dangerous_recursive_root_delete(words):
        return "recursive root deletion (rm -rf /)"
    if _world_writable_chmod(words):
        return "world-writable permissions (chmod 777)"
    secret = _reads_credential_words(words)
    if secret:
        return f"credential file ({secret})"
    for pattern, reason in _BASH_HARD_DENY_PATTERNS:
        if pattern in normalized:
            return f"{reason} ('{pattern}')"
    return ""
