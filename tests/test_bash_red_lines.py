"""The bash red lines, read as commands (W2/T).

A red line is a property of a *command*, not of a line of text.  The old
reading was two: a substring table over the whole text, and a shape check
over a flat word list.  Both had the wrong unit -- the text has no
position, and a flat list has no commands in it.

The grammar has both: a command's program name, its arguments, and the
targets of its redirections.  These tests pin what each red line means
now, and what it does not mean.
"""

import pytest

from ai_coding.security.permission_gate import PermissionDecision, PermissionGate


def _gate() -> PermissionGate:
    return PermissionGate()


def _verdict(command: str) -> PermissionDecision:
    return _gate().check("bash", {"command": command})


# ---- the red lines still are red lines


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm  -rf  /",
        "rm -r -f /",
        "rm -fr /",
        "RM -RF /",
        "rm --recursive --force /",
        "rm -rf /*",
        "rm -rf /etc",
        "chmod 777 /tmp/x",
        "chmod  777  /tmp/x",
        "chmod 0777 /tmp/x",
        "chmod a+rwx /tmp/x",
        "sudo rm x",
        "sudo  ls",
        "shutdown now",
        "reboot",
        "mkfs /dev/sda1",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "dd of=/dev/sda if=/dev/zero",
        "echo x > /dev/sda",
        "echo x >/dev/sda",
        "git push --force origin main",
        "git push -f origin",
        "git -C /tmp push --force",
        "cat ~/.ssh/id_rsa",
        "cat .env",
    ],
)
def test_the_red_lines_are_still_red_lines(command: str):
    """Every one of these is refused today.  Reading the command properly
    must not change that -- the refusal and the approval panel share this
    verdict, so a difference here is a difference the operator sees."""
    assert _verdict(command) == PermissionDecision.DENY


# ---- a red line is a command, so where the command sits no longer matters


@pytest.mark.parametrize(
    "command",
    [
        "a && rm -rf /",
        "ls; rm -rf /",
        "ls | rm -rf /",
        "FOO=bar rm -rf /",
        "x=1; rm -rf /",
        "cat $(rm -rf /)",
        "ls; chmod 777 /tmp/x",
        "env sudo ls",
    ],
)
def test_a_red_line_behind_another_command_is_still_a_red_line(command: str):
    """The old shape check looked at the whole text's first word, so every
    one of these was missed by it and survived only on the substring
    table's luck.  The grammar knows where the command is."""
    assert _verdict(command) == PermissionDecision.DENY


# ---- a red line that is not a command is not a red line


@pytest.mark.parametrize(
    "command",
    [
        'echo "rm -rf /"',
        "echo 'rm -rf /'",
        "# rm -rf /",
        "cat notes.md  # do not rm -rf / here",
        'grep -r "chmod 777" docs/',
        'echo "sudo "',
    ],
)
def test_a_red_line_that_is_only_text_is_not_a_command(command: str):
    """A quoted phrase and a comment are not commands.  Refusing a string
    that will never run teaches the model that the red lines are noise."""
    assert _verdict(command) == PermissionDecision.WARN


# ---- the ordinary work of a coding turn is not a red line


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "rm -rf ./build",
        "rm -rf /tmp/build",
        "grep -r TODO src/",
        "git push origin main",
        "chmod 644 script.sh",
        "cat README.md",
        "cat .env.example",
        "cat src/config.py",
        "grep -r password src/ tests/",
        "dd if=/dev/zero of=/tmp/out.bin",
        "mkfsdoc --help",
        "echo shutdown",
    ],
)
def test_ordinary_work_stays_ordinary(command: str):
    """The net must not widen: the ordinary commands a coding turn runs all
    day are asked about, not refused."""
    assert _verdict(command) == PermissionDecision.WARN


# ---- a command the grammar cannot read falls back to the old reading


@pytest.mark.parametrize(
    "command",
    [
        'echo "rm -rf /',  # the quote never closes
        'rm -rf / "unterminated',  # the same, the other way round
    ],
)
def test_a_broken_command_falls_back_to_the_texts_own_reading(command: str):
    """A line the grammar could not finish is judged by the rules that
    existed before it, not waved through.

    The fallback is the conservative direction: a broken line is refused
    if the old table would have refused it, and never less.  Pinned here
    because a fallback that silently relaxed would be the worst kind of
    bug -- it would look like the gate got smarter.
    """
    from ai_coding.shell_parse import parses_cleanly

    assert not parses_cleanly(command)
    assert _verdict(command) == PermissionDecision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "printf done &&",  # the list never finishes
        "cat >",  # the redirection has no target
    ],
)
def test_a_broken_command_without_a_red_line_is_only_asked_about(command: str):
    """The other half: a broken line that holds no red line is asked
    about, not refused."""
    assert _verdict(command) == PermissionDecision.WARN

