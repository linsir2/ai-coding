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

from typing import Any

__all__ = ["command_words", "path_candidates"]

#: The nodes whose text is a word the shell would pass on.  ``number`` is
#: in the list because ``777`` is what makes ``chmod`` a red line, and the
#: grammar keeps it in a node of its own.  A quoted string contributes its
#: *content*: the quotes are the shell's, not the word's.
_WORD_NODES = ("word", "number")
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
            for part in child.children:
                if part.type == "string_content":
                    words.append(_text(part))
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
            for part in child.children:
                if part.type == "string_content":
                    found.extend(_text(part).split())
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
