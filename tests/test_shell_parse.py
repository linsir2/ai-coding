"""The shell grammar, read (W2/T): the words and the paths a command names.

A command is a shell command, and a shell has a grammar.  Quotes hold a
phrase together, ``;`` inside a quote is not a separator, a command
substitution is a command of its own.  These tests pin what the grammar
says about the shapes that a regular expression got wrong.
"""

import pytest

from ai_coding.core import shell_parse


class TestPaths:
    def test_a_path_in_a_variable_assignment_is_found(self):
        """``D=/data/x; rm -rf $D`` names ``/data/x`` in the assignment."""
        assert "/data/x" in shell_parse.path_candidates("D=/data/x; rm -rf $D")

    def test_a_path_in_a_command_substitution_is_found(self):
        """``$(cat /data/secret)`` is a command, and it names a path."""
        found = shell_parse.path_candidates("echo $(cat /data/secret)")
        assert "/data/secret" in found

    def test_a_path_in_a_backtick_substitution_is_found(self):
        """The other substitution spelling."""
        found = shell_parse.path_candidates("echo `cat /data/secret`")
        assert "/data/secret" in found

    def test_a_path_in_quotes_is_found(self):
        """A quoted path is still a path -- the quotes are not part of it."""
        found = shell_parse.path_candidates('test -f "/data/x/app.py"')
        assert "/data/x/app.py" in found

    def test_a_home_path_is_found(self):
        assert "~/.ssh/id_rsa" in shell_parse.path_candidates("cat ~/.ssh/id_rsa")

    def test_a_relative_path_is_not_a_candidate(self):
        """Only the root and the home start a path; a relative one is the
        workspace's own business."""
        assert shell_parse.path_candidates("touch inside.marker") == []

    def test_the_same_path_is_reported_once(self):
        found = shell_parse.path_candidates("test -f /data/x/app.py && test -d /data/x/app.py")
        assert found == ["/data/x/app.py"]


class TestWords:
    def test_a_variable_prefix_does_not_hide_the_command(self):
        """``FOO=bar rm -rf /`` is still ``rm -rf /``."""
        assert shell_parse.command_words("FOO=bar rm -rf /") == ["rm", "-rf", "/"]

    def test_a_number_is_a_word(self):
        """``777`` is the word that makes ``chmod`` a red line, and the
        grammar keeps it in a node of its own."""
        assert "777" in shell_parse.command_words("chmod 777 /tmp/x")

    def test_a_quoted_phrase_is_one_word(self):
        """The ``;`` inside the quotes is not a separator."""
        assert shell_parse.command_words('echo "a; b"') == ["echo", "a; b"]

    def test_a_command_inside_a_substitution_is_found(self):
        """``cat $(rm -rf /)`` runs ``rm`` too."""
        words = shell_parse.command_words("cat $(rm -rf /)")
        assert "rm" in words
        assert "/" in words

    def test_a_comment_is_not_a_word(self):
        """``# rm -rf /`` is a comment, not a command.

        The real command in front of it still has its words; the comment's
        do not join them.
        """
        assert shell_parse.command_words("rm -rf / # rm -rf /") == ["rm", "-rf", "/"]

    def test_a_comment_alone_is_no_words(self):
        assert shell_parse.command_words("# rm -rf /") == []


class TestDegradation:
    def test_a_broken_command_does_not_raise(self):
        """Malformed shell must not take the gate down with it.

        A broken command is read as far as it goes: the words before the
        break are real, and the path inside an unterminated quote is a
        real path too.
        """
        assert shell_parse.command_words('echo "unterminated') == ["echo"]
        assert shell_parse.path_candidates('echo "unterminated /data/x') == [
            "/data/x"
        ]

    def test_an_empty_command_is_empty(self):
        assert shell_parse.command_words("") == []
        assert shell_parse.path_candidates("   ") == []

    def test_a_plain_command_keeps_its_words(self):
        assert shell_parse.command_words("rm -rf /") == ["rm", "-rf", "/"]


@pytest.mark.parametrize(
    "text",
    [
        "rm -rf /",
        "chmod 777 /tmp/x",
        "cat $(rm -rf /)",
        'echo "rm -rf /"',
        "FOO=bar rm -rf /",
        "# rm -rf /",
        "a && rm -rf /",
        "ls | grep x",
    ],
)
def test_the_parser_does_not_raise_on_any_shape(text):
    """Whatever the shape, the answer is an answer."""
    assert isinstance(shell_parse.command_words(text), list)
    assert isinstance(shell_parse.path_candidates(text), list)
