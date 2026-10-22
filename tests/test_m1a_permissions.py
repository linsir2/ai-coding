"""M1a slice 1: the rule language.

The permission gate today answers "is this tool name dangerous?" from a
table of names.  It cannot answer "may this action touch *this* path?",
because there is no resource dimension, no layer, and no posture.

This file pins the language the rest of M1a is built on -- and only the
language: matching, layering, postures, the resource vocabulary, and the
two files it reads.  It knows nothing about the gate, the tools or the
verdicts, so it can be tested on its own.

The semantics come from opencode's rule engine (concatenate the layers,
the last match wins, no "most specific wins"), which is what the M1a
blueprint settled on; the posture ladder and the two rule classes come
from Gemini CLI's policy engine.
"""

from __future__ import annotations

import textwrap

from ai_coding.security.permissions import (
    ACTION_ALIASES,
    POSTURES,
    Effect,
    Rule,
    Ruleset,
    actions_for,
    evaluate,
    glob_match,
    load_policy,
    load_rules,
    posture_of,
    rule_matches,
    subjects_of,
    wholly_disabled,
)

# ---- R1: the glob -----------------------------------------------------------


class TestGlob:
    def test_star_crosses_a_slash(self):
        # oc's rule: ``*`` is any text, slashes included.  A path rule is
        # therefore "somewhere under here", not "one segment".
        assert glob_match("src/app.py", "*.py")
        assert glob_match("a/b/c", "a*c")

    def test_question_mark_is_exactly_one_character(self):
        assert glob_match("acb", "a?b")
        assert not glob_match("ab", "a?b")
        assert not glob_match("axcb", "a?b")

    def test_double_star_is_not_a_second_operator(self):
        # ``*`` already crosses slashes, so ``**`` is just two of them -- it
        # does not mean "across directories" the way gitignore's would.
        assert glob_match("ab", "a**b") == glob_match("ab", "a*b")
        assert glob_match("a/b", "a**b") == glob_match("a/b", "a*b")

    def test_everything_else_is_literal(self):
        assert glob_match("a.b", "a.b")
        assert not glob_match("axb", "a.b")

    def test_the_pattern_is_anchored(self):
        # The whole string has to match: neither a prefix nor a suffix of it.
        assert not glob_match("xsrc/app.py", "src/*")
        assert not glob_match("src/app.py", "src/app")
        # A trailing ``*`` reaches the end of the path it names.
        assert glob_match("src/app.py.bak", "src/*")

    def test_an_empty_pattern_matches_only_the_empty_string(self):
        assert glob_match("", "")
        assert not glob_match("x", "")


# ---- R2/R3: matching and the "no opinion" ----------------------------------


def _ask(action: str, resource: str = "*") -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.ASK)


def _deny(action: str, resource: str = "*") -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.DENY)


def _allow(action: str, resource: str = "*") -> Rule:
    return Rule(action=action, resource=resource, effect=Effect.ALLOW)


class TestMatching:
    def test_a_rule_matches_when_both_its_conditions_hold(self):
        rule = _deny("edit", "*.env")
        assert rule_matches("edit", "app.env", rule, posture="build")
        assert not rule_matches("edit", "app.py", rule, posture="build")
        assert not rule_matches("read", "app.env", rule, posture="build")

    def test_a_rule_with_no_postures_is_active_everywhere(self):
        rule = _deny("edit")
        assert rule_matches("edit", "x", rule, posture="plan")
        assert rule_matches("edit", "x", rule, posture="readonly")
        assert rule_matches("edit", "x", rule, posture="build")

    def test_a_rule_scoped_to_a_posture_is_active_only_there(self):
        rule = Rule(action="edit", resource="*", effect=Effect.DENY, postures=("build",))
        assert rule_matches("edit", "x", rule, posture="build")
        assert not rule_matches("edit", "x", rule, posture="plan")
        assert not rule_matches("edit", "x", rule, posture="readonly")


# ---- R2: layering ------------------------------------------------------------


class TestEvaluate:
    def test_the_last_matching_rule_wins(self):
        layers = (Ruleset((_ask("edit"),)), Ruleset((_allow("edit"),)))
        assert evaluate("edit", "app.py", *layers, posture="build").effect is Effect.ALLOW

    def test_a_later_layer_overrides_an_earlier_one(self):
        layers = (Ruleset((_allow("edit"),)), Ruleset((_deny("edit"),)))
        assert evaluate("edit", "app.py", *layers, posture="build").effect is Effect.DENY

    def test_the_last_rule_inside_one_layer_wins(self):
        layer = Ruleset((_allow("edit"), _deny("edit")))
        assert evaluate("edit", "app.py", layer, posture="build").effect is Effect.DENY

    def test_no_match_is_no_opinion_not_allow(self):
        # The difference matters: "no opinion" is what the gate turns into a
        # question, while an explicit allow skips it.
        assert evaluate("edit", "app.py", Ruleset(), posture="build") is None

    def test_a_non_matching_resource_does_not_count(self):
        layer = Ruleset((_deny("edit", "*.env"),))
        assert evaluate("edit", "app.py", layer, posture="build") is None


# ---- R4: what the catalogue shows -------------------------------------------


class TestWhollyDisabled:
    def test_a_whole_action_deny_cuts_the_action(self):
        assert wholly_disabled("edit", Ruleset((_deny("edit"),)), posture="build")

    def test_a_scoped_deny_leaves_the_action_visible(self):
        # Gemini/Claude semantics: a bare tool name removes the tool from the
        # model's context; a scoped rule leaves it visible and refuses the
        # call, so the model can read what it hit.
        layer = Ruleset((_deny("edit", "*.env"),))
        assert not wholly_disabled("edit", layer, posture="build")

    def test_the_last_matching_rule_decides(self):
        layers = (Ruleset((_deny("edit"),)), Ruleset((_allow("edit", "src/*"),)))
        assert not wholly_disabled("edit", *layers, posture="build")

    def test_a_whole_action_ask_never_cuts_the_action(self):
        assert not wholly_disabled("edit", Ruleset((_ask("edit"),)), posture="build")

    def test_a_posture_scoped_deny_only_cuts_in_that_posture(self):
        layer = Ruleset(
            (Rule(action="edit", resource="*", effect=Effect.DENY, postures=("build",)),)
        )
        assert wholly_disabled("edit", layer, posture="build")
        assert not wholly_disabled("edit", layer, posture="plan")


# ---- R5: the resource vocabulary --------------------------------------------


class TestSubjects:
    def test_a_path_tool_names_its_path(self):
        assert subjects_of("read", {"path": "./src/app.py"}) == ("src/app.py",)

    def test_a_glob_names_its_pattern(self):
        assert subjects_of("glob", {"pattern": "*.py"}) == ("*.py",)

    def test_a_grep_names_its_pattern_not_its_root(self):
        # The pattern is what is searched; ``path`` is the root, which is not
        # the thing being touched.
        assert subjects_of("grep", {"pattern": "TODO", "path": "src"}) == ("TODO",)

    def test_a_skill_names_the_skill(self):
        assert subjects_of("skill", {"action": "read", "name": "brainstorm"}) == (
            "brainstorm",
        )

    def test_a_skill_that_names_none_touches_nothing_in_particular(self):
        # The skill tool's "list" action names no skill.
        assert subjects_of("skill", {"action": "list"}) == ("*",)

    def test_a_subagent_touches_nothing_a_rule_could_name(self):
        # This codebase's sub-agent has no agent id: its "name" is a label
        # the model picks, so a rule naming it would name something the
        # model chose.
        assert subjects_of("subagent", {"name": "researcher", "task": "look"}) == ("*",)

    def test_an_unknown_tool_names_nothing_specific(self):
        assert subjects_of("todo_write", {}) == ("*",)

    def test_a_bash_command_is_split_into_its_commands(self):
        # oc and Claude Code both match a compound command command by command:
        # a rule for "rm -rf *" must not miss the second half of
        # "cd /tmp && rm -rf x".
        assert subjects_of("bash", {"command": "cd /tmp && rm -rf x"}) == (
            "cd /tmp",
            "rm -rf x",
        )

    def test_a_bash_redirect_target_is_a_resource_of_its_own(self):
        # The shape does not contain the redirection: "cat a > /dev/sda" is
        # the command "cat a" and the write to "/dev/sda".  A rule has to be
        # able to name the place that is written.
        assert subjects_of("bash", {"command": "cat a > /dev/sda"}) == (
            "cat a",
            "/dev/sda",
        )

    def test_a_bash_command_the_grammar_cannot_read_keeps_the_whole_line(self):
        # Codex's conservative fallback: what the grammar could not read is
        # one opaque invocation, not a guess at its parts -- so a rule
        # written against the whole line still matches.
        subjects = subjects_of("bash", {"command": "rm -rf / && echo '"})
        assert "rm -rf /" in subjects
        assert "rm -rf / && echo '" in subjects

    def test_a_text_that_is_not_a_command_touches_nothing_in_particular(self):
        assert subjects_of("bash", {"command": "# rm -rf /"}) == ("*",)


# ---- R6: the posture ladder ---------------------------------------------------


class TestPosture:
    def test_the_ladder_is_ordered_most_to_least_restrictive(self):
        assert POSTURES == ("plan", "readonly", "build")

    def test_plan_mode_is_the_plan_posture(self):
        assert posture_of("plan", True) == "plan"

    def test_a_read_only_session_is_the_readonly_posture(self):
        assert posture_of("normal", True) == "readonly"

    def test_an_ordinary_session_is_the_build_posture(self):
        assert posture_of("normal", False) == "build"

    def test_plan_mode_wins_over_readonly(self):
        # plan implies read-only, and it is the stricter of the two.
        assert posture_of("plan", False) == "plan"


# ---- R7/R8: the two files ---------------------------------------------------


def _write(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestLoadRules:
    def test_a_preference_file_is_read(self, tmp_path):
        path = _write(
            tmp_path,
            "permissions.yaml",
            """
            rules:
              - action: edit
                resource: "*.env"
                effect: deny
                reason: "环境变量文件不能改"
            """,
        )
        rules, errors = load_rules(path)
        assert errors == []
        assert len(rules) == 1
        assert rules[0].action == "edit"
        assert rules[0].resource == "*.env"
        assert rules[0].effect is Effect.DENY
        assert rules[0].reason == "环境变量文件不能改"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        rules, errors = load_rules(tmp_path / "nope.yaml")
        assert rules == ()
        assert errors == []

    def test_a_broken_file_yields_no_rules_and_says_why(self, tmp_path):
        # A rule the operator wrote must not fail silently, so the error is
        # reported rather than swallowed -- but a broken file is not a crash.
        path = tmp_path / "permissions.yaml"
        path.write_text("rules: [this is not a mapping", encoding="utf-8")
        rules, errors = load_rules(path)
        assert rules == ()
        assert errors

    def test_a_broken_rule_is_skipped_and_reported(self, tmp_path):
        path = _write(
            tmp_path,
            "permissions.yaml",
            """
            rules:
              - action: edit
                resource: "*.env"
                effect: maybe
            """,
        )
        rules, errors = load_rules(path)
        assert rules == ()
        assert errors

    def test_a_rule_may_be_scoped_to_postures(self, tmp_path):
        path = _write(
            tmp_path,
            "permissions.yaml",
            """
            rules:
              - action: edit
                resource: "*"
                effect: deny
                postures: [build]
            """,
        )
        rules, errors = load_rules(path)
        assert errors == []
        assert rules[0].postures == ("build",)

    def test_an_unknown_effect_is_an_error(self, tmp_path):
        path = _write(
            tmp_path,
            "permissions.yaml",
            """
            rules:
              - action: edit
                resource: "*"
                effect: allowish
            """,
        )
        rules, errors = load_rules(path)
        assert rules == ()
        assert errors

    def test_an_action_is_lowercased_but_a_resource_is_not(self, tmp_path):
        # The gate lowercases every tool name before it looks for a rule, so
        # a rule's action is lowercased too -- otherwise "Edit" would be a
        # rule that never fires.  A path is not lowercased: on Linux it is
        # not the same file.
        path = _write(
            tmp_path,
            "permissions.yaml",
            """
            rules:
              - action: Edit
                resource: "src/App.PY"
                effect: deny
            """,
        )
        rules, errors = load_rules(path)
        assert errors == []
        assert rules[0].action == "edit"
        assert rules[0].resource == "src/App.PY"


class TestLoadPolicy:
    def test_a_policy_file_is_read(self, tmp_path):
        path = _write(
            tmp_path,
            "policies.yaml",
            """
            rules:
              - action: edit
                resource: "*.env"
                effect: deny
            """,
        )
        rules, errors = load_policy(path)
        assert errors == []
        assert len(rules) == 1
        assert rules[0].effect is Effect.DENY

    def test_a_policy_may_only_deny(self, tmp_path):
        # The policy class exists to be the last word and to tighten.  An
        # "allow" there would be an attempt to loosen a decision that a
        # project file already made, so it is refused rather than obeyed.
        path = _write(
            tmp_path,
            "policies.yaml",
            """
            rules:
              - action: edit
                resource: "*.env"
                effect: allow
            """,
        )
        rules, errors = load_policy(path)
        assert rules == ()
        assert errors

    def test_a_missing_policy_file_is_not_an_error(self, tmp_path):
        rules, errors = load_policy(tmp_path / "nope.yaml")
        assert rules == ()
        assert errors == []


# ---- the vocabulary ----------------------------------------------------------


def test_the_effect_vocabulary_is_exactly_three_words():
    assert {e.value for e in Effect} == {"allow", "ask", "deny"}


def test_a_rule_is_hashable():
    # The layers are tuples of rules and get compared and cached.
    rule = _deny("edit")
    assert hash(rule) == hash(_deny("edit"))


# ---- M2 T6: the action aliases ---------------------------------------------
#
# A rule names an *action*, and some actions are more than one tool: "edit"
# is write and edit, "shell" is bash.  Without the aliases a rule for
# ``deny edit *`` would refuse the edit tool and let write straight
# through -- the rule would say one thing and do another.
#
# The names are **lower case**, because the gate lower-cases the tool name
# before it matches (``permission_gate.check``): a rule written
# "subAgent" would never fire, since the gate compares it against
# "subagent".


class TestActionAliases:
    def test_edit_covers_write_and_edit(self):
        assert actions_for("edit") == frozenset({"write", "edit"})

    def test_shell_covers_bash(self):
        assert actions_for("shell") == frozenset({"bash"})

    def test_question_covers_ask_user(self):
        assert actions_for("question") == frozenset({"ask_user"})

    def test_an_ordinary_action_is_itself(self):
        assert actions_for("read") == frozenset({"read"})
        assert actions_for("glob") == frozenset({"glob"})
        assert actions_for("subagent") == frozenset({"subagent"})

    def test_every_alias_is_lower_case(self):
        for action, tools in ACTION_ALIASES.items():
            assert action == action.lower(), action
            for tool in tools:
                assert tool == tool.lower(), tool

    def test_a_tool_matches_its_alias(self):
        assert rule_matches("edit", "x.py", Rule("edit", "*", Effect.DENY), posture="build")
        assert rule_matches("write", "x.py", Rule("edit", "*", Effect.DENY), posture="build")
        assert rule_matches("bash", "ls", Rule("shell", "*", Effect.DENY), posture="build")
        # The gate lower-cases the tool name before it asks (``check`` does
        # ``name = tool_name.lower()``), so this is the action it uses.
        assert rule_matches("subagent", "t", Rule("subagent", "*", Effect.DENY), posture="build")

    def test_a_tool_does_not_match_another_actions_alias(self):
        assert not rule_matches("read", "x.py", Rule("edit", "*", Effect.DENY), posture="build")
        assert not rule_matches("bash", "ls", Rule("edit", "*", Effect.DENY), posture="build")
