"""The workspace reachability gate shared by planning, proof commands, and the
first-turn path hint.

``outside_workspace_paths`` answers one question — "can the file tools reach
this path?" — and three callers ask it: the workspace gates, the proof command
gate, and the first-turn hint in ``request_assembly``.  It is not
plan-specific, so it lives in its own module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from ai_coding.core.file_mentions import FILE_PATH_EXTENSIONS
from ai_coding.security.sandbox import Sandbox, SandboxViolation
from ai_coding.shell_parse import path_candidates as _shell_path_candidates

__all__ = ["outside_workspace_message", "outside_workspace_paths"]

# Conservative on purpose: only well-known filesystem roots are candidates, and
# a leading word/colon is excluded so URLs and prose never match.  The gate
# exists because file tools refuse outside paths anyway; failing at plan time
# beats seventeen failed calls (hotel-session audit, root cause R2).
#
# W2/R02 replaced the root list: a mount point the operator never named
# (``/data``, ``/workspace``) was invisible to it, so a plan naming one
# sailed through.  The candidate is now *any* absolute path -- which roots
# exist is not the question; reachability is.  Reachability is answered by
# :class:`Sandbox`, which resolves for real, so the scan no longer has to
# guess which prefixes might be roots.
#
# The lookbehind keeps three things out: a path glued to a word (``x/home``),
# a URL's authority (``https://host/a/b``), and a relative path (``../sibling``
# and ``./config`` are relative, and the leading ``.`` is what says so).  What
# is left is a string that *could* be a path, which :func:`_looks_like_path`
# then has to agree with.
_OUTSIDE_PATH_RE = re.compile(r"(?<![\w:./])(?:/[^/\s'\"`,;)\]]+){2,}")

#: The same "is this a file path?" question :mod:`ai_coding.core.file_mentions`
#: asks of a pasted path, asked here of a path a plan names.  One answer for
#: both: a known source/document extension, or a first segment that is a real
#: directory on this machine.
#:
#: Without it the scan reads an HTTP route (``/api/v1/users``) as a file
#: system path and refuses the plan -- a route names a resource, not a place
#: on disk, and a plan may legitimately name one.
#:
#: The dot before the extension is part of the answer, not a detail: without
#: it ``users`` ends in ``rs`` (Rust) and every route ending in a two-letter
#: extension would read as a file.  ``file_mentions`` requires the dot for the
#: same reason.
_FILE_SUFFIX_RE = re.compile(rf"\.(?:{FILE_PATH_EXTENSIONS})$", re.IGNORECASE)

#: The words that make the path in front of them a *directory*.  Used only
#: for the trailing-slash form, which on its own cannot be told apart from a
#: route (``/api/v1/users/`` and ``/data/w2demo/`` have the same shape).
#:
#: The live check found why this needs the words: the model writes a
#: directory target as "确保目录 /data/w2demo/ 存在" and a route as
#: "GET /api/v1/users/ 接口" -- the same shape, different surroundings.
#: Reading only the slash would refuse the route, and a refused plan is a
#: dead end the user cannot get around.
#:
#: The word has to be *right next to* the path, separated by nothing but
#: space or a quote.  "目录" also means a table of contents, and "目录结构
#: 如下 /api/v1/" is a document's outline, not a directory to create --
#: there the words between the word and the path belong to the other
#: meaning, and the gap is what tells the two apart.
#:
#: A command's own options may sit in the gap ("mkdir -p /data/x"): the
#: option says how the directory is made, not that it is somewhere else.
_DIRECTORY_BEFORE_RE = re.compile(
    r"(?:目录|文件夹|创建目录|新建目录|建立目录|mkdir|directory|folder)"
    r"(?:\s+-{1,2}[A-Za-z-]+)*"
    r"[\s`'\"]*$",
    re.IGNORECASE,
)

#: A shebang marker.  What follows it is the interpreter that runs the
#: script, which is not a file the tools would write -- and a plan that
#: writes a script says so.  The marker may be quoted into a printf rather
#: than written at the start of a line, so it is looked for anywhere.
_SHEBANG_MARKER = "#!"


def _looks_like_path(candidate: str, *, directory: bool = False) -> bool:
    """Does this absolute path name something on the file system?

    Three answers, all borrowed from the repository's own sense of what a
    path is (``core/file_mentions.py``):

    * the last segment names a known source or document (``app.py``);
    * the first segment is a directory that exists here (``/etc/passwd``,
      ``/usr/local/bin/tool``);
    * the path was written as a directory, and the words around it say so
      (``确保目录 /data/w2demo/ 存在``).

    The third is the one the live check found the hard way: the model writes
    a directory target the way a person does -- with no extension, under a
    first segment the machine has never heard of.  A gate that recognised
    only files let that plan through, and the review card went out for a
    plan the file tools cannot execute.

    A path that is none of these is prose that happens to be shaped like one:
    an API route, a version number, a fraction.  Those are not places, and
    refusing a plan over them would be a false alarm.
    """
    if directory:
        return True
    if _FILE_SUFFIX_RE.search(candidate.rsplit("/", 1)[-1]):
        return True
    return Path("/" + candidate.split("/")[1]).is_dir()


def outside_workspace_paths(
    texts: Iterable[str],
    workspace: str,
    *,
    extra_roots: Sequence[str] = (),
    from_shell: bool = False,
) -> list[str]:
    """Absolute filesystem paths in ``texts`` that file tools cannot reach.

    ``extra_roots`` are the directories the operator declared
    (``tools.extra_workspace_roots``).  Without them this function answered
    "can a file tool reach this?" with a :class:`Sandbox` built from the
    workspace alone, so a directory the operator had already authorized was
    reported as unreachable -- and the message tells the model to switch
    workspaces for a path that already works.

    ``from_shell`` says the text is a shell command rather than prose, and
    it changes where the candidates come from.  A command is read by the
    shell grammar (:mod:`ai_coding.shell_parse`), which sees paths
    that a text scan cannot -- the one behind a variable
    (``D=/data/x; rm -rf $D``), the one inside a command substitution
    (``$(cat /data/secret)``), the one in a directory command
    (``mkdir -p /data/w2demo``).  The grammar confirms those are words in
    a command, so the shape heuristic (:func:`_looks_like_path`) has
    nothing left to say about them and is skipped: a word that starts at
    the root *is* a path.

    Prose is the other way round.  A plan's sentence is not a command, the
    grammar can only read fragments out of it, and the shape heuristic is
    what keeps "新增 GET /api/v1/users 接口" from turning into a
    workspace refusal.
    """
    sandbox = Sandbox(workspace or ".", extra_roots=tuple(extra_roots))
    offenders: list[str] = []
    for text in texts:
        body = str(text or "")
        interpreters = _shebang_interpreters(body)
        candidates: list[tuple[str, bool]] = []
        for match in _OUTSIDE_PATH_RE.finditer(body):
            candidates.append((match.group(0).rstrip(".,"), False))
        if from_shell:
            for candidate in _shell_path_candidates(body):
                candidates.append((candidate, True))
        for candidate, from_grammar in candidates:
            if candidate in offenders:
                continue
            # Shell redirections name device files, not files a plan writes:
            # `2>/dev/null` appeared four times in one measured plan and each
            # time the gate refused the plan ("path /dev/null resolves outside
            # workspace").  A device is a sink, not a target.
            if _is_device_path(candidate):
                continue
            # A shebang names the interpreter that runs a script, not a file
            # the tools would write.  The live plan carried
            # "#!/usr/bin/env python3" inside the script it wrote, and the
            # gate reported the interpreter as unreachable -- so the refusal
            # named a path the model had no reason to change, and it retried
            # the same plan thirteen times before it moved on.
            #
            # Only the interpreter itself: a shebang exempts the first path
            # after it, not every path after it (a plan that writes a script
            # may well write another file too, and that one is a target).
            if candidate in interpreters:
                continue
            if not from_grammar:
                # A trailing slash says "directory" -- and so do the words
                # around the path: the model writes a directory target as
                # "确保目录 /data/w2demo/ 存在", and a command as
                # "mkdir -p /data/w2demo".  A route written with a trailing
                # slash ("GET /api/v1/users/") has the same shape and no
                # directory words around it, so it stays what it is.
                directory = _has_directory_context(body, match.start())
                if not _looks_like_path(candidate, directory=directory):
                    continue
            try:
                sandbox.resolve(candidate)
            except SandboxViolation:
                offenders.append(candidate)
    return offenders


#: Device paths a shell redirection may name.  They are sinks/sources the
#: kernel owns, never a target the plan's file tools would write.
_DEVICE_PATHS = frozenset(
    {
        "/dev/null",
        "/dev/zero",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/stdin",
        "/dev/tty",
        "/dev/random",
        "/dev/urandom",
    }
)


def _is_device_path(candidate: str) -> bool:
    return candidate in _DEVICE_PATHS or candidate.startswith("/dev/fd/")


def _shebang_interpreters(body: str) -> set[str]:
    """The interpreter paths the shebangs in this text name.

    A shebang (``#!``) says "this interpreter runs that script", which is a
    different thing from "the file tools touch this": the interpreter is
    read by the kernel, not written by a plan.  The live plan carried
    ``#!/usr/bin/env python3`` inside the script it wrote, and the gate
    reported the interpreter as an unreachable target -- so the refusal named
    a path the model had no reason to change, and it retried the same plan
    thirteen times before it moved on.

    Each shebang exempts the first path after it and no other: a plan that
    writes a script may write another file too, and that one is a target.
    """
    interpreters: set[str] = set()
    start = 0
    while True:
        marker = body.find(_SHEBANG_MARKER, start)
        if marker < 0:
            return interpreters
        after = _OUTSIDE_PATH_RE.search(body, marker + len(_SHEBANG_MARKER))
        if after is None:
            return interpreters
        interpreters.add(after.group(0).rstrip(".,"))
        start = after.end()


def _has_directory_context(body: str, start: int) -> bool:
    """Do the words just before this path say it is a directory?

    The live check is what made this necessary: the model writes a directory
    target as "确保目录 /data/w2demo/ 存在" and a route as "GET /api/v1/users/
    接口" -- the same shape, different words around them.  The words are read
    from the text just before the path, and only the ones that name a
    directory count: a route's own words ("接口", "端点", "路由") say the
    opposite.
    """
    return _DIRECTORY_BEFORE_RE.search(body[:start]) is not None


def outside_workspace_message(paths: Sequence[str]) -> str:
    """What the caller tells the model when a plan names an unreachable path."""
    return (
        "目标路径在工作区外：" + ", ".join(paths)
        + "。文件工具（read/write/edit/glob）无法访问工作区外的路径；"
        "请 cd 到目标目录重新启动 ai-coding，或把计划改为工作区内的路径。"
    )
