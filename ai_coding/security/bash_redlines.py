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

import os
import posixpath
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

#: Wrappers whose *first* word after the name is always a value (``timeout 5
#: sudo``), so the word is skipped whatever it looks like.
_WRAPPER_VALUE_FIRST = frozenset({"timeout"})

#: Wrappers that may carry a numeric first word (``nice 5 cmd`` / ``nice -n 5
#: cmd``) but whose first word is the command when it is not a number
#: (``nice cmd`` is legal).  The value is skipped only when it is a digit.
_WRAPPER_NUMERIC_FIRST = frozenset({"nice"})

#: Flags that take a value, per wrapper.  A value-taking flag pushes the cursor
#: past its value, so ``-i`` in ``env -i sudo`` does not hide the ``sudo``.
#: ``command``'s set is only ``-v``/``-V``: those look a name up and execute
#: nothing, while ``-p`` means "use the default PATH" and the word after it
#: *is* the command (§C.1④).
_WRAPPER_VALUE_FLAGS = {
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "timeout": frozenset({"-s", "-k"}),
    "command": frozenset({"-v", "-V"}),
}

#: The top-level directories that belong to the system rather than to the
#: work.  A recursive delete aimed at one of them is a red line: the
#: operator did not mean to empty ``/etc``.
#:
#: ``/home`` is in this set but *not* in :data:`_PREFIX_ROOTS`: a home
#: directory is a prefix of ``/home/<name>`` and of a project tree under it
#: (``/home/<name>/proj/build`` is ordinary work), so ``/home`` is judged by
#: exact match and depth 3 instead of as a prefix (§C.1③).
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

#: The system roots matched by exact-or-prefix.  ``/home`` is excluded: a
#: prefix match would refuse ``/home/<name>/proj/build``, which is ordinary
#: work, and ``rm -rf /home`` is caught by the exact ``/home`` check below.
_PREFIX_ROOTS = _SYSTEM_ROOTS - {"/home"}

#: The values a ``~`` / ``$VAR`` expansion can be resolved to *without*
#: running a shell.  Only these three names: guessing at the whole environment
#: would turn the judge into a shell and make its verdict depend on a clock or
#: a file (§七: "不猜展开后的值").  An unset name is ``""`` and is **not**
#: expanded -- expanding ``$USER`` to the empty string would turn ``rm -rf
#: $USER`` into ``rm -rf ""`` and then judge ``/``, a false positive.
_EXPANSIONS = {
    "HOME": str(Path.home()),
    "PWD": os.getcwd(),
    "USER": os.environ.get("USER", ""),
}

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


def _expand(word: str) -> str:
    """``~`` / ``$VAR`` / ``${VAR}`` -> a concrete path; unresolvable -> ``""``.

    Returning ``""`` (not a sentinel) is deliberate: a target that cannot be
    resolved is **not** a protected target, so it does not trigger a red line
    and falls through to the gate's WARN -- ask the operator.  That is the
    same reading as Codex's ``ForcedRm -> Prompt``, which never looks at
    whether the target resolves at all (§1.4).  The OS write boundary is the
    backstop for the ones outside the workspace.

    Only double quotes are stripped: single quotes are the shell's *literal*
    marks (``'$HOME'`` names a directory literally called ``$HOME``), so
    stripping them would misjudge it as the home directory.

    No same-line assignment resolution: ``command_views`` does not return
    assignments, and ``D=/data`` / ``rm -rf $D`` are two separate commands, so
    ``$D`` stays unresolved and is asked about.
    """
    out = word.replace('"', "")
    if out == "~" or out.startswith("~/"):
        out = _EXPANSIONS["HOME"] + out[1:]
    elif out.startswith("~"):
        return ""  # ~user: not this operator's home, do not guess
    for name, value in _EXPANSIONS.items():
        if not value:
            continue  # unset (e.g. USER empty) -> leave it alone
        out = re.sub(r"\$\{" + name + r"\}", value, out)
        # A word boundary, or $PWDDIR is read as $PWD + "DIR".
        out = re.sub(r"\$" + name + r"(?![A-Za-z0-9_])", value, out)
    if "$" in out or "`" in out:
        return ""  # unresolvable -> not protected -> the gate asks
    return out


def _program_is_unauditable(program: str) -> bool:
    """Is this program name an expansion the name-based rules cannot read?

    ``$x`` may name anything, so no rule over names can be trusted to have read
    the command -- the whole rule set is about program names, so this is a hole
    rather than a question (unlike an unresolvable *target*, §1.4).

    A command substitution (``$(...)`` / backticks) is **not** unauditable in
    this sense: the grammar descends into it and judges the inner command on its
    own view, so the outer view is redundant.  Refusing it here would shadow the
    inner command with a vaguer reason -- ``$(rm -rf /)`` is already a red line
    via the ``rm`` view, and a bare ``$(echo hi)`` should fall through to the
    gate's question, not be refused as "unauditable".
    """
    if program.startswith("$(") or program.startswith("`"):
        return False
    return "$" in program or "`" in program


def _programs(view: CommandView) -> list[str]:
    """The programs the command runs, wrappers peeled off.

    ``env sudo ls`` runs sudo; ``sudo`` is a wrapper here and ``ls`` is
    not, so the chain stops at the first name that is not a wrapper.

    A wrapper's own arguments have to be walked past, or the program behind
    them hides: ``env -i sudo ls`` put ``-i`` in front of ``sudo`` and the
    chain stopped there (a measured miss).  Two shapes of wrapper argument are
    walked:

    * a **value-taking flag** (``-u``/``-C``/``-S`` for ``env``, ``-n`` for
      ``nice``, ``-c``/``-n``/``-p`` for ``ionice``, ``-i``/``-o``/``-e`` for
      ``stdbuf``, ``-s``/``-k`` for ``timeout``, ``-v``/``-V`` for ``command``)
      is followed by its value;
    * a wrapper whose first word is always a value (``timeout``) skips it, and
      one that may carry a number (``nice``) skips it only when it is a digit
      -- ``nice sudo ls`` is a legal spelling and must not be peeled to
      nothing.

    ``--`` is skipped in both loops (it ends the wrapper's options; the word
    after it is the command).
    """
    names: list[str] = []
    words = (view.program, *view.arguments)
    index = 0
    while index < len(words):
        if words[index] == "--":
            index += 1
            continue
        name = _basename(words[index])
        if not name:
            index += 1
            continue
        names.append(name)
        if name not in _WRAPPERS:
            break
        index += 1
        while index < len(words):
            nxt = words[index]
            if nxt == "--":
                index += 1
                continue
            if nxt.startswith("-"):
                index += 2 if nxt in _WRAPPER_VALUE_FLAGS.get(name, ()) else 1
                continue
            if name in _WRAPPER_VALUE_FIRST or (
                name in _WRAPPER_NUMERIC_FIRST and nxt.isdigit()
            ):
                index += 1
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


def _is_protected_target(path: str) -> bool:
    """Is this path the root, a system directory, or someone's home?

    Judged **after expansion**: ``rm -rf $HOME`` deletes the operator's home
    and ``rm -rf ~`` deletes it too, and a rule that reads only the literal
    word misses both.  The expansion is the limited one (:func:`_expand`), and
    an expansion it cannot resolve returns ``""`` -- which is **not** protected,
    so the command is asked about rather than refused (§1.4: unresolvable ->
    WARN, like Codex's ``ForcedRm -> Prompt``).

    Shapes:

    * the root, exactly or with a glob (``/``, ``/*``);
    * a top-level system directory, exactly or by prefix (``/etc``, and
      ``/var/log`` under ``/var``) -- a prefix match on ``/etc`` also catches
      ``/etc/foo``, while an exact match catches ``/etc`` itself;
    * the operator's home, exactly (``$HOME``, ``~``);
    * ``/home`` itself, exactly;
    * another user's home, ``/home/<name>`` at depth 3;
    * a path whose **last** component is ``..`` (``./..`` deletes the ancestor
      itself).  ``../build`` is *not* protected: that is a sibling directory,
      ordinary monorepo work, and the OS write boundary owns it.

    An **empty** target is protected: it is the old fail-closed reading, and it
    is what a quoted string whose whole content is a command substitution reads
    as here (``rm -rf "$(echo /)"`` -- the substitution is not expanded, so the
    word is empty, and the command it stands for really does delete a path).
    The unresolvable case is different: a *non-empty* word that ``_expand``
    cannot resolve (``$DIR``) is asked about, not refused (§1.4).
    """
    if not path:
        return True
    expanded = _expand(path)
    if not expanded:
        return False  # unresolvable -> not protected -> the gate asks
    # normpath first: /home/../etc, /home//user and /home/./user all normalize,
    # so the shape checks below cannot be walked past.  Same normalisation the
    # credential matcher uses.
    normalized = posixpath.normpath(expanded) or "/"
    if normalized in ("/", "/*"):
        return True
    parts = normalized.split("/")
    if parts[-1] == "..":
        return True
    if normalized == _EXPANSIONS["HOME"]:
        return True
    if normalized == "/home":
        return True
    if len(parts) == 3 and parts[1] == "home":
        return True
    return any(normalized == r or normalized.startswith(r + "/") for r in _PREFIX_ROOTS)


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
    # ``--force-with-lease`` is a force push written the safe way: it refuses
    # when the remote moved, but it still overwrites history.  Treating it as
    # ordinary would let the one flag that *looks* careful through.
    return any(word in ("--force", "-f", "--force-with-lease") for word in rest)


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
        and any(_is_protected_target(path) for path in _targets(view)),
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
        # doas is the OpenBSD sudo and is in _WRAPPERS; the rule has to ask for
        # it by name too, or a doas is waved through.
        lambda view: _runs(view, "sudo") or _runs(view, "doas"),
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
    (
        # A program name that is itself an expansion cannot be audited: ``$x``
        # may name anything, so no rule over names can be trusted to have read
        # the command.  Unlike an unresolvable *target* (asked about, §1.4), an
        # unresolvable *program* is refused -- the whole rule set is about
        # program names, so one it cannot read is a hole, not a question.  A
        # command substitution is not this case: see _program_is_unauditable.
        "unauditable command",
        lambda view: _program_is_unauditable(view.program),
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
