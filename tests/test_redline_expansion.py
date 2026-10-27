"""N-backlog §C: the red lines must see the words that carry expansions.

Measured root cause: `_WORD_NODES` was `("word", "number")`, so the grammar
**dropped** every word containing an expansion -- `cat ~/.ssh/id_$USER` had
`view.arguments == ()`, and `rm -rf $HOME` had `()` too.  `_grammar_reads` then
short-circuited the text fallback, so the command was waved through twice over.

This module pins the fixed behaviour: the words are no longer dropped, the
protected targets are judged after expansion, and an expansion that cannot be
resolved is **asked about** (WARN) rather than denied -- matching Codex's
`ForcedRm -> Prompt` (see docs/plan-n-backlog.md §1.4 for the primary source).
"""

from __future__ import annotations

import pytest

from ai_coding.security.bash_redlines import bash_red_line_with_fallback as judge
from ai_coding.shell_parse import command_views

# --- C.1①  the grammar must not drop a word that carries an expansion --------

@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat ~/.ssh/id_$USER", ("~/.ssh/id_$USER",)),
        ("rm -rf $HOME", ("-rf", "$HOME")),
        ("rm -rf ${HOME}", ("-rf", "${HOME}")),
        ('rm -rf "$HOME"', ("-rf", "$HOME")),
    ],
)
def test_a_word_with_an_expansion_is_not_dropped(command, expected):
    views = command_views(command)
    assert len(views) == 1
    assert views[0].arguments == expected, (
        f"{command!r}: the expansion-carrying word was dropped"
    )


# --- C.1③  path normalisation + quote handling ------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /home/../etc",
        "rm -rf /home//user",
        "rm -rf /home/./user",
        # A concatenation node keeps its quotes in the view, so `_expand` has to
        # strip them -- `/etc/x` is protected whatever the environment.
        'rm -rf "/etc"/x',
    ],
)
def test_the_path_is_normalised_and_the_quotes_are_not_part_of_it(command):
    assert judge(command), f"{command!r}: normpath + quote handling"


# --- C.1③  protected targets, judged after expansion ------------------------

@pytest.mark.parametrize(
    "command",
    [
        "rm -rf $HOME",
        "rm -rf ${HOME}",
        "rm -rf ~",
        "rm -rf ~/",
        "rm -rf /home/user",
        "rm -rf /var/log",
        "rm -rf ./..",
        "rm -rf /home/../etc",
        "rm -rf /home//user",
        "rm -rf /home/./user",
        "rm -rf /etc",
        "rm -rf /home",
    ],
)
def test_a_protected_target_is_refused(command):
    assert judge(command), f"{command!r} must be a red line"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf ./build",
        "rm -rf /tmp/build",
        "rm -rf build",
        "rm -rf ../build",
        "rm -rf /home/user/proj/build",
    ],
)
def test_an_ordinary_target_is_not_refused(command):
    assert judge(command) == "", f"{command!r} is ordinary work"


# --- C.1②  an unresolvable expansion is asked about, not denied --------------

@pytest.mark.parametrize(
    "command",
    [
        "rm -rf $DIR",
        "rm -rf $(pwd)",
        "rm -rf ${TMPDIR}/build",
        "rm -rf ~root",
        "rm -rf $PWDDIR",
    ],
)
def test_an_unresolvable_target_is_not_a_red_line(command):
    """Codex's `ForcedRm -> Prompt`: the gate asks, the OS boundary is the backstop."""
    assert judge(command) == "", (
        f"{command!r}: an unresolvable target must not be hard-denied"
    )


def test_a_resolvable_home_target_is_still_refused():
    """The line is 'can it be resolved', not 'does it contain a variable'."""
    assert judge("rm -rf $HOME")
    assert judge("rm -rf $DIR") == ""


# --- C.1⑥  an unresolvable program name is unauditable ----------------------

@pytest.mark.parametrize(
    "command",
    [
        'x="rm -rf /"; $x',
        "x=1; $x rm -rf /",
    ],
)
def test_an_unresolvable_program_is_refused(command):
    assert judge(command), f"{command!r}: the program name cannot be audited"


# --- C.1④  wrapper peeling ---------------------------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "doas rm x",
        "env -i sudo ls",
        "timeout 5 sudo ls",
        "nice sudo ls",
        "nice 5 sudo ls",
        "nice -n 5 sudo ls",
        "env -- sudo ls",
        "timeout 5 -- sudo ls",
        "stdbuf -o0 sudo ls",
        "ionice -c 3 sudo ls",
    ],
)
def test_a_wrapper_chain_still_reaches_the_escalation(command):
    assert judge(command), f"{command!r}: the wrapper chain must be peeled"


@pytest.mark.parametrize(
    "command",
    [
        "command -v sudo",
        "command -v doas",
        "command -p sudo ls",      # -p takes no value: sudo really runs
    ],
)
def test_command_lookup_is_not_an_escalation(command):
    if command == "command -p sudo ls":
        assert judge(command), "-p does not take a value: sudo really runs"
    else:
        assert judge(command) == "", f"{command!r} only looks a name up"


# --- C.1⑤  force push -------------------------------------------------------

def test_force_with_lease_is_a_force_push():
    assert judge("git push --force-with-lease origin main")


def test_an_ordinary_push_is_not():
    assert judge("git push origin main") == ""


# --- C.1①b  a double-quoted expansion must survive --------------------------

def test_a_double_quoted_home_target_is_refused():
    assert judge('rm -rf "$HOME"')


# --- the corpus's own two "not a miss" cases --------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "rm -rf $PWD",
        "D=/data; rm -rf $D",
    ],
)
def test_these_two_are_not_red_lines(command):
    assert judge(command) == ""
