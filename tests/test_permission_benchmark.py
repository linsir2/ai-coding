"""M-G4: the permission gate's rule chain has a golden corpus, like the red lines.

``redline_benchmark`` freezes one judge (``bash_red_line_with_fallback``).  The
gate is five steps and only the first step's bash branch had a benchmark; the
risk that matters lives in the other four.  These pin the benchmark, the corpus
floors, and the three ways a row can silently stop testing what it claims to.
"""

from __future__ import annotations


def test_the_benchmark_passes_on_the_frozen_corpus(capsys):
    from scripts import permission_benchmark

    assert permission_benchmark.main([]) == 0
    out = capsys.readouterr().out
    assert "| FN" in out and "| FP" in out
    assert "permission gate benchmark OK" in out


def test_every_class_meets_its_floor():
    """Per class, not per direction: an aggregate floor hides a shrinking class.

    The agent-veto test loops over its class, so a class thinned to nothing would
    pass vacuously.
    """
    from ai_coding.security import permission_corpus as corpus

    actual = {
        label: sum(1 for case in corpus.CORPUS if case.label == label)
        for label in corpus.FLOORS
    }
    short = {
        label: (actual[label], floor)
        for label, floor in corpus.FLOORS.items()
        if actual[label] < floor
    }
    assert short == {}, short
    assert len(corpus.CORPUS) >= corpus.MIN_CORPUS


def test_the_corpus_has_no_duplicate_rows():
    """The whole row, not ``(tool, params)``: the same call under a different
    gate configuration is a different case, and the corpus leans on that."""
    from ai_coding.security import permission_corpus as corpus

    assert len(set(corpus.CORPUS)) == len(corpus.CORPUS)


def test_every_param_value_is_a_string():
    """A frozen dataclass is only hashable if its fields are.

    ``params`` is annotated ``tuple[tuple[str, Any], ...]``; a nested list or dict
    would make ``set(CORPUS)`` raise ``TypeError`` instead of reporting a broken
    row.  The corpus keeps every value a string, and this pins it.
    """
    from ai_coding.security import permission_corpus as corpus

    offenders = [
        (case.label, key, value)
        for case in corpus.CORPUS
        for key, value in case.params
        if not isinstance(value, str)
    ]
    assert offenders == [], offenders


# ---- the invariants the corpus exists to protect ---------------------------


def test_agent_veto_is_not_reopened_by_a_session_rule():
    """M1b: an agent that narrows a capability must not be reopened.

    Every row in the class pairs an agent DENY with a session ALLOW; the verdict
    *and* the rule are both pinned, so a chain that returns DENY for the wrong
    reason is still a failure.
    """
    from ai_coding.security import permission_corpus as corpus
    from scripts.permission_benchmark import judge

    for case in corpus.DENY_AGENT_VETO:
        assert case.session_rules, "the row must actually try to reopen the veto"
        verdict, rule = judge(case)
        assert verdict == "DENY", case
        assert rule == case.rule, (case, rule)


def test_a_grant_cannot_reopen_a_refusal():
    """Step 3 (rules' refusals) runs before step 4 (a saved grant)."""
    from ai_coding.security.permission_corpus import PermissionCase
    from ai_coding.security.permissions import Effect, FileRules, Rule
    from scripts.permission_benchmark import judge

    case = PermissionCase(
        tool="read", params=(("path", "secret.txt"),), label="deny_rule_chain",
        verdict="DENY", grants=("read:secret.txt",),
        rules=FileRules(user=(Rule("read", "secret.txt", Effect.DENY, "no"),)),
    )

    assert judge(case)[0] == "DENY"


# ---- the three ways a row silently stops testing what it claims -------------


def test_a_fn_and_a_fp_are_counted_apart():
    """On a clean corpus both are 0, so a swapped pair would be invisible.

    Each synthetic row lies in a different direction: one frozen DENY that the
    gate allows (a false negative), one frozen ALLOW that the gate denies (a
    false positive).
    """
    from ai_coding.security.permission_corpus import PermissionCase
    from scripts.permission_benchmark import Metrics

    missed = PermissionCase(
        tool="read", params=(("path", "a.py"),), label="deny_hard_tier",
        verdict="DENY",  # frozen as refused; the gate allows it
    )
    overblocked = PermissionCase(
        tool="bash", params=(("command", "rm -rf /"),), label="allow_safe",
        verdict="ALLOW",  # frozen as allowed; the gate refuses it
    )

    metrics = Metrics((missed, overblocked))

    assert metrics.fn == 1
    assert metrics.fp == 1


def test_the_judge_returns_the_decision_name_not_the_value():
    """``PermissionDecision.value`` is lower case; the corpus is frozen upper."""
    from ai_coding.security.permission_corpus import PermissionCase
    from scripts.permission_benchmark import judge

    case = PermissionCase(
        tool="bash", params=(("command", "rm -rf /"),), label="deny_hard_tier",
        verdict="DENY",
    )

    assert judge(case)[0] == "DENY"


def test_grant_rows_use_a_tool_that_needs_a_grant():
    """`read` is allowed anyway, so deleting step 4 would leave it ALLOW.

    `write` / `edit` without a grant are WARN, so the grant is what the row is
    actually testing -- and removing step 4 turns the row WARN, which is visible.
    """
    from ai_coding.security import permission_corpus as corpus

    for case in corpus.ALLOW_GRANT_ANSWERS:
        assert case.tool in {"write", "edit"}, case


def test_grant_keys_match_what_the_gate_computes():
    """``read`` is granted by directory, so a hand-written file key never matches."""
    from ai_coding.security import permission_corpus as corpus
    from ai_coding.security.permission_gate import PermissionGate

    gate = PermissionGate()
    for case in corpus.ALLOW_GRANT_ANSWERS:
        assert case.grants, f"{case} carries no grant, so it tests nothing"
        produced = gate.grant_key(case.tool, dict(case.params))
        assert produced is not None, f"{case.tool} cannot be granted at all"
        assert produced in case.grants, (
            f"{case.tool} produces {produced!r}, the row carries {case.grants!r}"
        )


def test_boundary_rows_set_a_workspace():
    """With ``workspace=None`` the edge check returns immediately.

    ``_outside_resource_reason`` starts with ``if not self._workspace: return
    None``, so a row testing the edge without a workspace silently tests ALLOW.
    """
    from ai_coding.security import permission_corpus as corpus

    for case in corpus.WARN_OUTSIDE_WORKSPACE:
        assert case.workspace is not None, case


def test_the_known_miss_is_named_not_hidden(capsys):
    """The grep/glob edge gap is printed as a number, not dropped from the corpus."""
    from scripts import permission_benchmark

    assert permission_benchmark.main([]) == 0
    out = capsys.readouterr().out

    assert "known_miss（仍漏报，按判定输出） | 1" in out
    assert "known_miss（缺口可见，修好即降到 0）" in out
