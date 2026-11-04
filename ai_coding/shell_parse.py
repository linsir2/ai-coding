"""Shell text, parsed (W2/T): the words and the paths a command names.

A command is a shell command, and a shell has a grammar: quotes hold a
phrase together, ``;`` inside a quote is not a separator, a command
substitution is a command of its own, a comment is not a command at all.
Reading those with a regular expression is how ``echo "a; b"`` becomes two
words and ``cat $(rm -rf /)`` loses the ``rm`` -- and how a path hidden
behind a variable (``D=/data/x; rm -rf $D``) hides from the gate that is
supposed to see it.

tree-sitter gives the real grammar, so the words and the paths come from
the tree rather than from a guess.  The tree is only *read* here: the
judgements (is this path reachable, is this a red line) stay where they
are, in the modules that own them.

Two questions, two answers, and they are not the same list:

* :func:`command_words` -- what the shell would pass as arguments.  Only
  real commands contribute; a fragment the grammar could not read is not
  an argument, and a comment is not a command.
* :func:`path_candidates` -- what paths the text names.  This one does
  read the fragments, because a path inside a broken quote is still a
  path, and the gate that is supposed to see it cannot afford to be the
  thing that misses it.

The grammar is loaded once per process and the parser is reused: building
them costs 0.04 ms together, and a parser holds no state between parses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CommandView",
    "command_shape",
    "command_views",
    "command_words",
    "effect_path_candidates",
    "parses_cleanly",
    "path_candidates",
]


@dataclass(frozen=True)
class CommandView:
    """One command, read out of the shell grammar.

    A red line is a property of a *command*, not of a line of text: in
    ``a && rm -rf /`` there are two commands, and the second one is the
    dangerous one.  A flat word list cannot tell them apart, which is why
    the rules read the tree command by command.
    """

    #: The program's name, as the shell would run it (``/bin/rm`` keeps
    #: its path; the rules compare the basename).
    program: str
    #: The arguments, in order.
    arguments: tuple[str, ...]
    #: The targets of the command's redirections, in order.
    redirects: tuple[str, ...]
    #: Whether the grammar read the whole command.  A command the grammar
    #: could not finish is still read as far as it goes, and this says so.
    broken: bool = False


def command_shape(view: CommandView) -> str:
    """The command as it was written, in one line.

    The program and its arguments, separated by spaces.  The operator
    reads a refusal and has to see *what* was refused, so both the red
    lines and the permission rules speak about the command in these
    words -- one definition, so the two cannot drift apart.
    """
    return " ".join(part for part in (view.program, *view.arguments) if part)


#: The nodes whose text is a word the shell would pass on.  ``number`` is
#: in the list because ``777`` is what makes ``chmod`` a red line, and the
#: grammar keeps it in a node of its own.  A quoted string contributes its
#: *content*: the quotes are the shell's, not the word's.
#:
#: A word that *carries* an expansion is still a word -- the shell expands it
#: and passes the result on -- so ``concatenation`` (``~/.ssh/id_$USER``),
#: ``simple_expansion`` (``$HOME``), ``expansion`` (``${HOME}``) and
#: ``command_substitution`` (``$(pwd)``) are in the list too.  They used to be
#: dropped, which made ``rm -rf $HOME`` and ``cat ~/.ssh/id_$USER`` invisible to
#: the rules *and* to the text fallback behind them: a double blindness.
_WORD_NODES = (
    "word",
    "number",
    "concatenation",
    "simple_expansion",
    "expansion",
    "command_substitution",
)
_STRING_NODES = ("string", "raw_string")

#: A path starts at the root or at the home.  Anything else is relative,
#: and a relative path is the workspace's own business.
_PATH_PREFIXES = ("/", "~")


def _parser() -> Any | None:
    """The shell parser, or ``None`` when the grammar is not available.

    Loaded on first use and kept: the language is a constant, and building
    it costs 0.04 ms.  A missing grammar is not a crash -- the caller
    falls back to what it did before, which is why this returns ``None``
    instead of raising.
    """
    global _PARSER
    if _PARSER is None:
        try:
            import tree_sitter
            import tree_sitter_bash
        except ImportError:  # pragma: no cover - the grammar is a dependency
            return None
        try:
            language = tree_sitter.Language(tree_sitter_bash.language())
            _PARSER = tree_sitter.Parser(language)
        except Exception:  # noqa: BLE001 - a broken grammar is not a crash
            return None
    parser: Any | None = _PARSER
    return parser


#: The process-wide parser.  ``None`` means "not built yet"; the build
#: itself is guarded inside :func:`_parser`.
_PARSER: Any | None = None


def _text(node: Any) -> str:
    """The node's text, as a string."""
    return str(node.text.decode("utf-8", "replace"))


def _string_content(node: Any) -> str:
    """The content a quoted string contributes, without its quote marks.

    Expansion children are included: ``"$HOME"`` is one word that expands to
    the home directory, and dropping it would drop the argument entirely
    (``rm -rf "$HOME"`` used to read as ``rm -rf ""``).

    Only ordinary ``string``/``raw_string`` nodes are handled; ``$'..'`` and
    adjacent-string concatenation stay out of scope.  A ``raw_string`` keeps
    its literal text (quotes stripped) -- the shell does not expand inside it,
    and the red line layer reads what the shell would pass on.
    """
    raw = _text(node)
    if node.type == "raw_string":
        return raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == "'" else raw
    return "".join(
        _text(part)
        for part in node.children
        if part.type
        in ("string_content", "simple_expansion", "expansion", "concatenation")
    )


def _commands(node: Any, into: list[Any] | None = None) -> list[Any]:
    """Every command in the tree, including the ones inside a substitution.

    A command substitution is a command of its own -- ``cat $(rm -rf /)``
    runs the ``rm`` -- so the walk goes through every node rather than
    stopping at the top level.
    """
    if into is None:
        into = []
    if node.type == "command":
        into.append(node)
    for child in node.children:
        _commands(child, into)
    return into


def _assignments(node: Any, into: list[Any] | None = None) -> list[Any]:
    """Every variable assignment in the tree.

    ``D=/data/x`` is an assignment, not an argument -- and it is where the
    path shows.  The command that uses ``$D`` hides it; the assignment is
    what names it.  An assignment that belongs to a command
    (``FOO=bar rm x``) is found by the command walk as well, so the two
    lists overlap; the caller only cares that the path is seen once.
    """
    if into is None:
        into = []
    if node.type == "variable_assignment":
        into.append(node)
    for child in node.children:
        _assignments(child, into)
    return into


def _broken(node: Any, into: list[Any] | None = None) -> list[Any]:
    """The nodes the grammar could not read.

    A malformed command is still worth reading for paths: a path inside a
    broken quote is a real path.
    """
    if into is None:
        into = []
    if node.type == "ERROR" or node.is_error:
        into.append(node)
    for child in node.children:
        _broken(child, into)
    return into


def _words_of(command: Any) -> list[str]:
    """The words one command passes on."""
    words: list[str] = []
    for child in command.children:
        if child.type == "command_name":
            for part in child.children:
                if part.type == "word":
                    words.append(_text(part))
        elif child.type in _WORD_NODES:
            words.append(_text(child))
        elif child.type in _STRING_NODES:
            words.append(_string_content(child))
    return words


def _paths_in(node: Any) -> list[str]:
    """The paths one node names, whether it is a command or a fragment.

    A quoted string is one node holding everything between the quotes, so
    it can hold more than one path (``"a /b/c d"``).  Its content is split
    on whitespace, the way the shell would hand the words on.

    A node the grammar could not read has no quote around its content --
    the content is a ``string_content`` child directly -- so both shapes
    are read here.
    """
    found: list[str] = []
    for child in node.children:
        if child.type in _WORD_NODES:
            found.append(_text(child))
        elif child.type in _STRING_NODES:
            found.extend(_string_content(child).split())
        elif child.type == "string_content":
            found.extend(_text(child).split())
    return found


def _tree(text: str) -> Any | None:
    """The parse of ``text``, or ``None`` when there is no tree to read."""
    parser = _parser()
    if parser is None:
        return None
    try:
        return parser.parse(str(text or "").encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - a broken command is not a crash
        return None


def _redirect_targets(node: Any) -> list[str]:
    """The targets of every redirection around this command.

    A command with a redirection is not a root node but the ``body`` of a
    ``redirected_statement``, and the target lives in the *parent*'s
    ``redirect`` field.  There can be more than one, and the first is not
    the only one that matters (``cat a > /dev/sda > /tmp/b``).
    """
    targets: list[str] = []
    parent = node.parent
    if parent is None or parent.type != "redirected_statement":
        return targets
    for redirect in parent.children_by_field_name("redirect"):
        destination = redirect.child_by_field_name("destination")
        if destination is not None:
            targets.append(_text(destination))
    return targets


def _is_broken(node: Any) -> bool:
    """Did the grammar fail to read this command?"""
    if node.has_error:
        return True
    parent = node.parent
    return bool(parent is not None and parent.has_error)


def parses_cleanly(text: str) -> bool:
    """Did the grammar read this text all the way through?

    A line with nothing to read -- a comment, a bare assignment -- was
    read all the way through: there was nothing in it to read.  That is
    different from a line the grammar could not finish, and the
    difference matters to whoever decides whether to trust this reading.
    """
    tree = _tree(text)
    if tree is None:
        return False
    return not tree.root_node.has_error


def _program_and_arguments(command: Any) -> tuple[str, tuple[str, ...]]:
    name = command.child_by_field_name("name")
    program = _text(name) if name is not None else ""
    arguments: list[str] = []
    for argument in command.children_by_field_name("argument"):
        if argument.type in _WORD_NODES:
            arguments.append(_text(argument))
        elif argument.type in _STRING_NODES:
            arguments.append(_string_content(argument))
    return program, tuple(arguments)


def command_views(text: str) -> list[CommandView]:
    """One view per command in ``text``, in the order they run.

    A command is a ``command`` node in the tree, so a command after ``&&``
    or ``;`` is a view of its own, and a command inside a substitution is
    one too.  A text that is not a command at all -- a plan's prose, a
    comment -- yields no views.
    """
    tree = _tree(text)
    if tree is None:
        return []
    views: list[CommandView] = []
    for command in _commands(tree.root_node):
        program, arguments = _program_and_arguments(command)
        views.append(
            CommandView(
                program=program,
                arguments=arguments,
                redirects=tuple(_redirect_targets(command)),
                broken=_is_broken(command),
            )
        )
    return views


def command_words(text: str) -> list[str]:
    """The words of every command in ``text``, in order.

    A word is what the shell would pass as an argument: a bare word, a
    number, or the content of a quoted string.  Commands inside a command
    substitution are included.

    A text that is not a command at all -- a plan's prose, a malformed
    shell -- yields an empty list (or the words before the break).  The
    caller keeps whatever it did before; this is an addition, never a
    subtraction.
    """
    tree = _tree(text)
    if tree is None:
        return []
    words: list[str] = []
    for command in _commands(tree.root_node):
        words.extend(_words_of(command))
    return words


def path_candidates(text: str) -> list[str]:
    """The absolute paths ``text`` names, in order, without repeats.

    A path is a word that starts at the root (``/``) or at the home
    (``~``).  This is the *candidate* list: whether the path is reachable
    is the sandbox's question, not this module's.

    Unlike :func:`command_words`, this reads the fragments the grammar
    could not read and the variable assignments -- a path in a broken
    quote or behind an assignment is still a path.
    """
    tree = _tree(text)
    if tree is None:
        return []
    candidates: list[str] = []
    for command in _commands(tree.root_node):
        candidates.extend(_paths_in(command))
    for assignment in _assignments(tree.root_node):
        candidates.extend(_paths_in(assignment))
    for fragment in _broken(tree.root_node):
        candidates.extend(_paths_in(fragment))
    found: list[str] = []
    for candidate in candidates:
        if not candidate.startswith(_PATH_PREFIXES):
            continue
        if candidate in found:
            continue
        found.append(candidate)
    return found


_EFFECT_WRITE_PROGRAMS = frozenset(
    {
        "touch", "cp", "mv", "rm", "rmdir", "mkdir", "tee", "truncate",
        "install", "ln", "sed", "perl", "dd", "tar", "unzip", "chmod",
        "chown", "patch",
    }
)


def effect_path_candidates(text: str, workspace: str = "") -> list[str]:
    """Absolute write candidates a shell command may produce.

    This is a *candidate* list for BashTool's before/after stat probe, not a
    proof that a path was written.  A scope-aware traversal is needed because
    tree-sitter attaches a trailing redirect to a whole list/compound, and
    because ``cd sub && ...`` changes the base for later relative paths.  The
    public ``command_views`` contract stays unchanged for security rules.
    """
    if not str(text or "").strip():
        return []
    tree = _tree(text)
    if tree is None:
        return []
    found: list[str] = []
    _effect_walk(tree.root_node, Path(workspace or ".").expanduser().resolve(), found)
    for absolute in path_candidates(text):
        try:
            resolved = str(Path(absolute).expanduser())
        except OSError:
            continue
        if resolved not in found:
            found.append(resolved)
    return found


def _effect_add(
    word: str,
    cwd: Path | None,
    found: list[str],
    *,
    force: bool = False,
) -> None:
    word = word.strip()
    if len(word) >= 2 and word[0] == word[-1] and word[0] in ("'", '"'):
        word = word[1:-1]
    if not word or word.startswith("-"):
        return
    if word.startswith(("http://", "https://", "//")):
        return
    looks_path = (
        word in (".", "..")
        or word.startswith(("/", "~", "./", "../"))
        or "/" in word
        or "." in Path(word).name
    )
    if not looks_path and not force:
        return
    path = Path(word).expanduser()
    if not path.is_absolute():
        if cwd is None:
            return
        path = cwd / path
    try:
        value = str(path.resolve())
    except OSError:
        value = str(path)
    if value not in found:
        found.append(value)


def _effect_cd(args: tuple[str, ...], cwd: Path | None) -> Path | None:
    if len(args) != 1:
        return None
    target = args[0]
    if target in ("-", "") or target.startswith("-"):
        return None
    if any(ch in target for ch in "$`*?[]{}"):
        return None
    path = Path(target).expanduser()
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    # A missing literal cd fails in the shell; treat its cwd as unknown rather
    # than predicting writes into a directory that is not there.
    return resolved if resolved.is_dir() else None


def _effect_substitutions(node: Any, cwd: Path | None, found: list[str]) -> None:
    for child in node.named_children:
        if child.type in ("command_substitution", "process_substitution"):
            _effect_walk(child, cwd, found)
        else:
            _effect_substitutions(child, cwd, found)


_OUTPUT_REDIRECT_OPS = frozenset({">", ">>", ">|", "&>", "&>>", ">&"})


def _effect_contains_cd(node: Any) -> bool:
    if node.type == "command":
        program, _arguments = _program_and_arguments(node)
        return program.rsplit("/", 1)[-1] == "cd"
    return any(_effect_contains_cd(child) for child in node.named_children)


def _effect_redirect(redirect: Any, cwd: Path | None, found: list[str]) -> None:
    if redirect.type == "heredoc_redirect":
        for child in redirect.children:
            if child.type == "file_redirect":
                _effect_redirect(child, cwd, found)
        return
    if redirect.type != "file_redirect":
        return
    operator = next(
        (child.type for child in redirect.children if child.type in _OUTPUT_REDIRECT_OPS),
        "",
    )
    if not operator:
        return
    destination = redirect.child_by_field_name("destination")
    if destination is None or destination.type in ("number", "file_descriptor"):
        return
    if destination.type in ("command_substitution", "process_substitution"):
        _effect_walk(destination, cwd, found)
        return
    _effect_add(_text(destination), cwd, found, force=True)


def _effect_redirects(node: Any, cwd: Path | None, found: list[str]) -> None:
    for redirect in node.children_by_field_name("redirect"):
        destination = redirect.child_by_field_name("destination")
        if destination is not None and destination.type in (
            "command_substitution",
            "process_substitution",
        ):
            _effect_walk(destination, cwd, found)
        _effect_redirect(redirect, cwd, found)


def _effect_walk(
    node: Any,
    cwd: Path | None,
    found: list[str],
) -> Path | None:
    kind = node.type
    if kind == "program":
        current = cwd
        before = cwd
        for child in node.children:
            if child.type == "&":
                current = before
                continue
            if child.is_named:
                before = current
                current = _effect_walk(child, current, found)
        return current
    if kind == "command":
        program, arguments = _program_and_arguments(node)
        _effect_substitutions(node, cwd, found)
        leaf = program.rsplit("/", 1)[-1]
        if leaf == "cd":
            return _effect_cd(arguments, cwd)
        if leaf == "false":
            return None
        force = leaf in _EFFECT_WRITE_PROGRAMS
        for argument in arguments:
            _effect_add(argument, cwd, found, force=force)
        _effect_substitutions(node, cwd, found)
        return cwd
    if kind == "subshell":
        local = cwd
        for child in node.named_children:
            local = _effect_walk(child, local, found)
        return cwd
    if kind in ("if_statement", "for_statement", "while_statement", "case_statement"):
        if _effect_contains_cd(node):
            return None
    if kind in (
        "compound_statement",
        "do_group",
        "if_statement",
        "for_statement",
        "while_statement",
        "case_statement",
    ):
        current = cwd
        before = cwd
        for child in node.children:
            if child.type == "&":
                current = before
                continue
            if child.is_named:
                before = current
                current = _effect_walk(child, current, found)
        return current
    if kind == "pipeline":
        for child in node.named_children:
            _effect_walk(child, cwd, found)
        return cwd
    if kind == "list":
        current = cwd
        left_start = cwd
        op = ""
        for child in node.children:
            if child.type in ("&&", "||", ";", "\n", "&"):
                op = child.type
                continue
            if not child.is_named:
                continue
            if op == "&&":
                if current is None:
                    # The left command failed/unknown; the right branch did not
                    # run.  Do not predict writes from it.
                    continue
                result = _effect_walk(child, current, found)
                current = result
            elif op == "||":
                if current is not None:
                    # The left command succeeded; the right branch did not run.
                    continue
                result = _effect_walk(child, left_start, found)
                current = result if result is not None else left_start
            elif op == "&":
                result = _effect_walk(child, left_start, found)
                current = left_start
            else:
                result = _effect_walk(child, current, found)
                current = result
            left_start = current if op in ("&&", "||") else left_start
            op = ""
        return current
    if kind == "redirected_statement":
        body = node.child_by_field_name("body")
        entry = cwd
        after = _effect_walk(body, cwd, found) if body is not None else cwd
        redirect_cwd = (
            after
            if body is not None
            and body.type == "list"
            and not _effect_contains_cd(body)
            else entry
        )
        _effect_redirects(node, redirect_cwd, found)
        return after
    if kind in ("command_substitution", "process_substitution"):
        local = cwd
        for child in node.named_children:
            local = _effect_walk(child, local, found)
        return cwd
    if kind == "function_definition":
        # A function body is not executed at definition time.
        return cwd
    if kind in ("file_redirect", "heredoc_redirect"):
        return cwd
    # Transparent wrappers: process named children in order.
    current = cwd
    for child in node.named_children:
        current = _effect_walk(child, current, found)
    return current
