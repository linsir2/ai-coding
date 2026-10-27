"""M-D2: the red line benchmark (U8-U11b).

Two things are pinned here, and they are not the same thing:

* **the corpus** (``security/redline_corpus.py``) holds one ``verdict`` per
  command, captured from the implementation *before* the two readers were
  merged.  The benchmark's hard assertion is a **golden comparison**, not a
  guess from the label: if any verdict moved, the change is visible.
* **the merge** (``bash_redlines`` now owns the fallback, ``permission_gate``
  delegates) is a behaviour-preserving refactor, and the frozen corpus is its
  safety net: the merged reader has to answer exactly what the old
  ``bash_denied`` answered, on the corpus *and* on the existing test set.

The ``known_miss`` class is the point of the whole exercise: ``x="rm -rf /"; $x``
used to be a real hole (the grammar read it, no rule matched, and
``_grammar_reads`` short-circuited the text fallback).  §C closed all 16 of
those rows, so the class is now empty and the counter is the assertion: a
regression that reopens one shows up as the count rising from zero.
"""

from __future__ import annotations

from ai_coding.security.bash_redlines import bash_red_line_with_fallback
from ai_coding.security.permission_gate import bash_denied
from ai_coding.security.redline_corpus import (
    CORPUS,
    DANGEROUS,
    KNOWN_FP,
    KNOWN_MISS,
    MIN_DANGEROUS,
    MIN_KNOWN_MISS,
    MIN_SAFE,
    MIN_UNPARSEABLE,
    SAFE,
    UNPARSEABLE_DENY,
    UNPARSEABLE_WARN,
)
from ai_coding.security.redline_legacy_snapshot import LEGACY_BASH_DENIED

#: The DENY half of ``tests/test_bash_red_lines.py``, replayed by U9.  Kept in
#: step with that file on purpose: the merge has to keep both green.
DENY_CASES = [
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
    "a && rm -rf /",
    "ls; rm -rf /",
    "ls | rm -rf /",
    "FOO=bar rm -rf /",
    "x=1; rm -rf /",
    "cat $(rm -rf /)",
    "ls; chmod 777 /tmp/x",
    "env sudo ls",
    'echo "rm -rf /',
    'rm -rf / "unterminated',
]

#: The WARN half of the same file: ordinary work, and text that only looks
#: like a red line.  The net must not widen.
WARN_CASES = [
    'echo "rm -rf /"',
    "echo 'rm -rf /'",
    "# rm -rf /",
    "cat notes.md  # do not rm -rf / here",
    'grep -r "chmod 777" docs/',
    'echo "sudo "',
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
    "printf done &&",
    "cat >",
]


# ---- U8: the corpus is a golden snapshot ------------------------------------


def test_the_corpus_matches_its_frozen_verdicts() -> None:
    """Every entry's frozen ``verdict`` is what the merged reader answers.

    The assertion is against the frozen value, not against the label: a label
    says what the author intended, a verdict says what the code does, and only
    the second one can drift.
    """
    mismatches = [
        (case.command, case.verdict, bash_red_line_with_fallback(case.command))
        for case in CORPUS
        if bash_red_line_with_fallback(case.command) != case.verdict
    ]
    assert not mismatches, f"verdicts moved: {mismatches}"


# ---- U9: the two readers agree (the merge is behaviour-preserving) ----------


def test_the_merged_reader_matches_the_frozen_bash_denied_snapshot() -> None:
    """U9: the merge is behaviour-preserving, measured against a frozen snapshot.

    ``bash_denied`` now delegates to ``bash_red_line_with_fallback``, so the two
    sides of the old comparison were the *same* function and the assertion was
    true by construction -- it could not fail no matter what the merge did.  The
    real equivalence is against ``LEGACY_BASH_DENIED``: the pre-merge
    ``bash_denied``'s verdicts, captured once and stored beside the corpus, so a
    divergence in either entry point is a red build rather than a tautology.
    """
    commands = (
        {case.command for case in CORPUS} | set(DENY_CASES) | set(WARN_CASES)
    )
    missing = commands - set(LEGACY_BASH_DENIED)
    assert not missing, f"the snapshot lost a command the tests use: {sorted(missing)}"
    for command in sorted(commands):
        expected = LEGACY_BASH_DENIED[command]
        assert bash_red_line_with_fallback(command) == expected, (
            command,
            expected,
            bash_red_line_with_fallback(command),
        )
        assert bash_denied(command) == expected, (
            command,
            expected,
            bash_denied(command),
        )


def test_the_gate_no_longer_owns_a_copy_of_the_rules() -> None:
    """The whole point of the merge: one judge, not two.

    A second copy of the substring table in ``permission_gate`` is exactly the
    drift the merge removes, so its absence is pinned rather than assumed.
    """
    import ai_coding.security.permission_gate as gate

    for gone in (
        "_BASH_HARD_DENY_PATTERNS",
        "_dangerous_recursive_root_delete",
        "_world_writable_chmod",
        "_reads_credential",
        "_grammar_reads",
        "_normalize_command",
        "_command_words",
    ):
        assert not hasattr(gate, gone), f"{gone} came back into permission_gate"


# ---- U10: the known hole is visible, not invisible --------------------------


def test_known_miss_is_surfaced(capsys) -> None:
    """The ``known_miss`` count is printed, and §C drove it to zero.

    It used to be that ``x="rm -rf /"; $x`` was counted and printed, every
    run, because the grammar read the command and no rule matched.  §C closed
    all 16 rows, so the section is gone and the *counter* is the assertion:
    a regression that reopens a hole has to show up as the number rising from
    zero, not as a section quietly disappearing.
    """
    import re

    from scripts import redline_benchmark

    assert redline_benchmark.main([]) == 0
    out = capsys.readouterr().out

    match = re.search(r"known_miss（仍漏报，按判定输出）\s*\|\s*(\d+)", out)
    assert match is not None, "the known_miss counter must stay in the report"
    assert int(match.group(1)) == 0, (
        "§C closed every known_miss row; a nonzero count is a reopened hole"
    )
    assert "known_fp" in out
    assert "| FN" in out
    assert "| FP" in out


# ---- U11: the corpus is not quietly emptied ---------------------------------


def test_the_corpus_is_not_emptied() -> None:
    """A corpus thinned to nothing would make the benchmark pass vacuously."""
    assert len(DANGEROUS) >= MIN_DANGEROUS
    assert len(UNPARSEABLE_DENY) + len(UNPARSEABLE_WARN) >= MIN_UNPARSEABLE
    assert len(SAFE) >= MIN_SAFE
    assert len(KNOWN_MISS) >= MIN_KNOWN_MISS

    commands = [case.command for case in CORPUS]
    assert len(set(commands)) == len(commands), "duplicate corpus commands"

    assert all(case.verdict == "" for case in UNPARSEABLE_WARN)
    assert all(case.verdict == "" for case in SAFE)
    assert all(case.verdict == "" for case in KNOWN_MISS)
    assert all(case.verdict != "" for case in KNOWN_FP)


# ---- U11b: "cannot read" is two states, not one -----------------------------


def test_unparseable_splits_into_deny_and_warn() -> None:
    """Both halves exist, and they are separated by the verdict, not the label.

    ``echo "rm -rf /`` (unterminated quote, red line inside) is refused by the
    conservative fallback; ``printf done &&`` (unterminated list, no red line)
    is only asked about.  Freezing them as one class would have made the CI red
    on day one.
    """
    deny = {case.command for case in UNPARSEABLE_DENY}
    warn = {case.command for case in UNPARSEABLE_WARN}

    assert 'echo "rm -rf /' in deny
    assert "printf done &&" in warn
    assert "cat >" in warn
    assert not (deny & warn), "a command cannot be both halves"
