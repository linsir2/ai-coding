"""The plan prompt must ask for work, not for one document per step.

Trace 2026-10-31 (session f1d881ee): a request to *explore* the codebase's
layers came back as 12 items, then 14, almost all of the shape "read N files →
produce phaseN-x.md", with ``verify: test -f … && wc -l``.  The prompt asked for
a proof command on every step and the only cheap machine-checkable proof is a
file the plan itself writes; the plan then optimises for its own evidence.

The rules pinned here are the prompt-side half (the executor's one-item-per-turn
shape is a separate phase):

* verify is optional, and a plan artifact is never its own proof;
* an implementation plan is a handful of outcome items, not one per file;
* a research request is one report.
"""


def test_verify_is_optional_and_an_artifact_is_not_its_own_proof():
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()

    assert "only when a real acceptance command exists" in lowered
    assert "never use a file the plan itself produces" in lowered


def test_implementation_plans_are_a_handful_of_outcome_items():
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()

    assert "one item is one deliverable outcome" in lowered
    assert "at most six items" in lowered


def test_a_research_request_is_one_report_not_a_plan_of_documents():
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()

    assert "produces one report" in lowered
    assert "not a plan whose items each write a separate document" in lowered


def test_the_plan_mode_reminder_no_longer_demands_proof_on_every_step():
    from ai_coding.core.plan_reminders import PLAN_ENTER_REMINDER

    assert "every step you submit must carry" not in PLAN_ENTER_REMINDER.lower()
    assert "optional" in PLAN_ENTER_REMINDER.lower()


def test_plan_mode_gets_a_read_only_shell_and_stays_inside_the_workspace():
    from ai_coding.core.plan_runner import PLAN_PROMPT

    lowered = PLAN_PROMPT.lower()

    assert "shell_read" in lowered, "git log/find/wc need a read-only shell"
    assert "no pipes or redirection" in lowered
    assert "temp files" in lowered and "do not use /tmp" in lowered
