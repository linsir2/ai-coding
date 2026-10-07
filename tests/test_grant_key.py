"""TDD (G4): what an "always" is allowed to remember.

The defect this file pins is a mismatch of scope.  The operator reads
"edit src/foo.py" in the modal -- diff and all -- and presses Always; until now
the thing remembered was ``edit``, which is every file in the workspace.  A grant
broader than the call it came from is the same class of mistake as a ``None``
that silently means "allow everything": it reads like one thing and behaves like
another.

``grant_key`` is pure and total, so the whole contract is a table.
"""

from __future__ import annotations

import pytest

from ai_coding.security.permission_gate import grant_key


def test_a_path_tool_keys_on_its_path():
    assert grant_key("edit", {"path": "src/foo.py"}) == "edit:src/foo.py"
    assert grant_key("write", {"path": "src/foo.py"}) == "write:src/foo.py"


def test_write_and_edit_are_separate_grants():
    """Replacing a file wholesale and changing one hunk in it are different
    actions; granting one has never been a reason to stop asking about the
    other."""
    assert grant_key("write", {"path": "a.py"}) != grant_key("edit", {"path": "a.py"})


def test_two_spellings_of_one_path_are_one_grant():
    """``./a.py`` and ``a.py`` are the same file; treating them as two grants
    would nag the operator for the same decision."""
    assert grant_key("edit", {"path": "./src/foo.py"}) == grant_key(
        "edit", {"path": "src/foo.py"}
    )
    assert grant_key("edit", {"path": "src/./foo.py"}) == "edit:src/foo.py"


def test_the_tool_name_is_matched_case_insensitively():
    assert grant_key("Edit", {"path": "a.py"}) == "edit:a.py"


def test_a_tool_without_a_path_has_no_grant():
    """Nothing to name means nothing may be remembered: recording "something"
    would repeat the same defect at a different level."""
    assert grant_key("write", {}) is None
    assert grant_key("edit", {"path": "   "}) is None


def test_an_mcp_tool_has_no_grant():
    """A remote tool's arguments are not summarisable into a resource, and the
    modal cannot state what would be remembered."""
    assert grant_key("github__create_issue", {"title": "x"}) is None
    assert grant_key("filesystem__read_file", {"path": "a.py"}) is None


def test_bash_is_never_remembered():
    """A policy exclusion, not a technical one: an always on bash would be an
    always on every command it may ever run."""
    assert grant_key("bash", {"command": "ls"}) is None


def test_bash_is_never_remembered_even_if_it_carries_a_path():
    """The exclusion is checked before resource extraction, so a param shape
    that happens to look like a path cannot turn it into a grantable call."""
    assert grant_key("bash", {"command": "ls", "path": "a.py"}) is None


@pytest.mark.parametrize("tool", ["BASH", "Bash"])
def test_the_exclusion_is_case_insensitive(tool):
    assert grant_key(tool, {"command": "ls"}) is None
