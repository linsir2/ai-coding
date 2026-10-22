"""The permission rule language (M1a / T2).

The gate today answers one question -- "is this tool *name* dangerous?" --
from a table of names.  It cannot answer "may this action touch *this*
path?", because there is no resource dimension, no layer, and no posture.

This module is that missing language.  It is deliberately small and knows
nothing about the gate, the tools, or the verdicts: it holds rules, matches
them, layers them, and reads two files.  Everything else -- what a verdict
means, what a tool is called, what the operator sees -- belongs to
``permission_gate``.

The semantics are opencode's (concatenate the layers, the last match wins,
no "most specific wins"), because that is what the M1a blueprint settled on
and it is what opencode actually ships.  Two things come from Gemini CLI's
policy engine: the posture ladder (a rule may name the postures it is
active in) and the two rule classes (a preference file that a project may
override, and a global policy file that may only deny and has the last
word).
"""

from __future__ import annotations

import enum
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ai_coding.paths import config_home
from ai_coding.shell_parse import command_shape, command_views, parses_cleanly

__all__ = [
    "NO_FILE_RULES",
    "POLICY_FILE",
    "POSTURES",
    "PREFERENCE_FILE",
    "Effect",
    "FileRules",
    "Rule",
    "Ruleset",
    "evaluate",
    "glob_match",
    "load_file_rules",
    "load_policy",
    "load_rules",
    "posture_of",
    "rule_to_dict",
    "rules_from_dicts",
    "rule_matches",
    "subjects_of",
    "wholly_disabled",
]


#: The posture ladder, most restrictive first.  A rule may name the postures
#: it is active in; an empty tuple means every posture.
#:
#: The three are the postures a session can actually be in: plan mode (which
#: implies read-only), a read-only session, and an ordinary one.
POSTURES: tuple[str, ...] = ("plan", "readonly", "build")


class Effect(enum.Enum):
    """What a rule says about the call it matches."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Rule:
    """One rule: an action, the resource it applies to, and what it says.

    ``postures`` names the postures the rule is active in; empty means all
    of them.  ``reason`` is the operator's own words, shown to whoever the
    rule refuses.
    """

    action: str
    resource: str
    effect: Effect
    reason: str = ""
    postures: tuple[str, ...] = ()


#: One layer's rules, in the order they were written.
Ruleset = tuple[Rule, ...]


@dataclass(frozen=True)
class FileRules:
    """The rules that came from files, grouped by the file they came from.

    The grouping is not decoration: the operator's listing shows the rules
    by where they came from, and "the project overrides the user" only
    means something if the two are told apart.
    """

    #: The user's own file (``~/.config/ai-coding/permissions.yaml``).
    user: Ruleset = ()
    #: The project's file (``<workspace>/permissions.yaml``), which wins over
    #: the user's.
    project: Ruleset = ()
    #: The global policy file (``~/.config/ai-coding/policies.yaml``): deny
    #: only, and the last word.
    policy: Ruleset = ()
    #: Why a file or a rule was not read.  A rule the operator wrote must not
    #: fail silently.
    errors: tuple[str, ...] = ()


#: No rules at all -- what a gate that reads no files starts from.
NO_FILE_RULES = FileRules()


# ---- matching ---------------------------------------------------------------


def glob_match(value: str, pattern: str) -> bool:
    """Whether ``value`` matches ``pattern``, the way opencode's does.

    ``*`` is any text (slashes included), ``?`` is exactly one character,
    everything else is literal, and the pattern is anchored at both ends.
    There is no second operator for ``**``: two stars say what one star
    already says, and gitignore's "across directories" would be a third
    way of saying the same thing.
    """
    if not pattern:
        return value == ""
    escaped = []
    for char in pattern:
        if char == "*":
            escaped.append(".*")
        elif char == "?":
            escaped.append(".")
        else:
            escaped.append(re.escape(char))
    return re.fullmatch("".join(escaped), value) is not None


def rule_matches(
    action: str, resource: str, rule: Rule, *, posture: str
) -> bool:
    """Whether ``rule`` speaks about this call.

    Both the action and the resource must match, and the rule must be
    active in this posture.
    """
    if rule.postures and posture not in rule.postures:
        return False
    return glob_match(action, rule.action) and glob_match(resource, rule.resource)


# ---- layering ----------------------------------------------------------------


def evaluate(
    action: str,
    resource: str,
    *layers: Ruleset,
    posture: str = "build",
) -> Rule | None:
    """The rule that decides this call, or ``None`` when nobody had an opinion.

    The layers are concatenated and the **last** match wins -- opencode's rule,
    and the blueprint's: a later layer overrides an earlier one, and within
    a layer the later line wins.  There is no "most specific wins" rule:
    specificity is not a thing the language knows about.

    ``None`` is not "allow".  It means no rule spoke, and the caller decides
    what that means (the gate turns it into a question).
    """
    winner: Rule | None = None
    for layer in layers:
        for rule in layer:
            if rule_matches(action, resource, rule, posture=posture):
                winner = rule
    return winner


def wholly_disabled(action: str, *layers: Ruleset, posture: str = "build") -> bool:
    """Whether the whole action is cut from the tool catalogue.

    The *last* rule matching the action decides: ``edit * deny`` cuts
    everything, ``edit <dir>/* allow`` after it does not -- the tool stays
    visible and the refusal happens at dispatch, where the model can read
    what it hit.  This is Gemini's and Claude Code's rule (a bare tool name
    removes the tool from the model's context; a scoped one does not), and it
    is the same predicate the execution query uses, so the catalogue and
    the verdict cannot drift apart.
    """
    winner: Rule | None = None
    for layer in layers:
        for rule in layer:
            if not rule.postures or posture in rule.postures:
                if glob_match(action, rule.action):
                    winner = rule
    return winner is not None and winner.resource == "*" and winner.effect is Effect.DENY


# ---- the resource vocabulary --------------------------------------------------


def _lexical(path: str) -> str:
    """The path as the rule language compares it.

    Only lexical: a leading ``./`` goes, and backslashes become slashes.
    Resolving the path is the Sandbox's job (the same stance ``grant_key``
    takes), so a rule matches the string the tool was handed and the
    boundary is still adjudicated elsewhere.
    """
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _named(text: str) -> str:
    """A resource that could not be named is the wildcard.

    An empty string would match only an empty pattern, so a rule like
    ``skill "*" deny`` would silently not fire on a call that named no
    skill.  ``*`` says what it means: we cannot say what this touches.
    """
    return text or "*"


def subjects_of(action: str, params: Mapping[str, Any]) -> tuple[str, ...]:
    """What this call touches -- the things a rule's resource can name.

    Most actions touch one thing.  ``bash`` touches several: a compound
    command is read out of the shell grammar command by command, because a
    red line is a property of a command and not of a line of text (the
    reason ``shell_parse`` exists at all).
    """
    if action in ("read", "write", "edit"):
        return (_named(_lexical(str(params.get("path") or ""))),)
    if action in ("glob", "grep"):
        # The pattern is what is searched; ``path`` is the root, which is
        # not the thing being touched.
        return (_named(_lexical(str(params.get("pattern") or ""))),)
    if action == "bash":
        command = str(params.get("command") or "")
        views = command_views(command)
        subjects: list[str] = []
        for view in views:
            subjects.append(command_shape(view))
            # A redirection writes somewhere; the target is a thing the
            # call touches, and the shape alone does not contain it.
            subjects.extend(view.redirects)
        if not parses_cleanly(command):
            # The grammar did not read the line all the way through.  The
            # whole line is one opaque subject as well, so a rule written
            # against it still matches: the parts we did read may be
            # missing something.
            subjects.append(command.strip())
        elif not subjects:
            # Nothing to read: a comment, a plan's prose.
            return ("*",)
        return tuple(subjects)
    if action == "skill":
        return (_named(str(params.get("name") or "")),)
    if action == "webfetch":
        return (_named(str(params.get("url") or "")),)
    if action == "websearch":
        return (_named(str(params.get("query") or "")),)
    # Everything else -- the MCP vessel, the sub-agent, the tools that
    # touch no path -- touches nothing a rule could name.  The sub-agent
    # in particular has no agent id in this codebase: its ``name`` is a
    # label the model picks, so a rule naming it would be naming something
    # the model chose.
    return ("*",)


def posture_of(mode: str, readonly: bool) -> str:
    """Which posture a session is in.

    Plan mode implies read-only and is the stricter of the two, so it wins.
    """
    if mode == "plan":
        return "plan"
    if readonly:
        return "readonly"
    return "build"


# ---- the two files -----------------------------------------------------------


def _load(path: Path, *, deny_only: bool) -> tuple[Ruleset, list[str]]:
    """Read one rule file.  A broken file yields no rules and the reasons.

    The errors are returned rather than raised: a rule the operator wrote
    must not fail silently, so the caller shows them (the ``/permissions``
    listing) and logs them -- but a broken file is not a crash.
    """
    if not path.exists():
        return (), []
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return (), [f"{path}: {exc}"]
    if document is None:
        return (), []
    if not isinstance(document, Mapping):
        return (), [f"{path}: the file must be a mapping with a 'rules' list"]
    raw = document.get("rules")
    if raw is None:
        return (), []
    if not isinstance(raw, list):
        return (), [f"{path}: 'rules' must be a list"]

    rules: list[Rule] = []
    errors: list[str] = []
    for index, entry in enumerate(raw):
        rule, error = _rule_from(entry, deny_only=deny_only)
        if error:
            errors.append(f"{path}: rule {index + 1}: {error}")
            continue
        assert rule is not None
        rules.append(rule)
    return tuple(rules), errors


def _rule_from(entry: Any, *, deny_only: bool) -> tuple[Rule | None, str]:
    """One rule out of a file, or the reason it is not one."""
    if not isinstance(entry, Mapping):
        return None, "not a mapping"
    action = str(entry.get("action") or "").strip()
    resource = str(entry.get("resource") or "").strip()
    effect_text = str(entry.get("effect") or "").strip().lower()
    if not action:
        return None, "'action' is required"
    if not resource:
        return None, "'resource' is required"
    # The action names a tool, and the gate lowercases every tool name
    # before it looks for a rule.  Lowercasing the rule's action too keeps
    # one namespace: an operator who writes "Edit" gets a rule that fires,
    # not one that silently never matches.
    action = action.lower()
    try:
        effect = Effect(effect_text)
    except ValueError:
        return None, f"'effect' must be allow, ask or deny (got {effect_text!r})"
    if deny_only and effect is not Effect.DENY:
        # The policy class exists to tighten and to have the last word.  An
        # "allow" here would be an attempt to loosen a decision a project
        # file already made, so it is refused rather than obeyed.
        return None, "a policy rule may only deny"
    postures = entry.get("postures") or ()
    if isinstance(postures, str):
        postures = (postures,)
    if not isinstance(postures, (list, tuple)):
        return None, "'postures' must be a list of posture names"
    names = tuple(str(name).strip() for name in postures if str(name).strip())
    unknown = [name for name in names if name not in POSTURES]
    if unknown:
        return None, f"unknown posture(s): {', '.join(unknown)}"
    return Rule(
        action=action,
        resource=resource,
        effect=effect,
        reason=str(entry.get("reason") or "").strip(),
        postures=names,
    ), ""


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    """The JSON form a session stores (the same keys a YAML file uses)."""
    data: dict[str, Any] = {
        "action": rule.action,
        "resource": rule.resource,
        "effect": rule.effect.value,
    }
    if rule.reason:
        data["reason"] = rule.reason
    if rule.postures:
        data["postures"] = list(rule.postures)
    return data


def rules_from_dicts(
    rows: Iterable[Any], *, label: str = "session rule"
) -> tuple[Ruleset, tuple[str, ...]]:
    """Read the session's own layer, skipping (and naming) what is broken.

    A session file is written by this program, but it is also a JSON file a
    human can edit.  One bad row must not take the session down, and it must
    not disappear silently either -- the caller shows these reasons in
    ``/permissions``.

    ``label`` names the layer in the error text, so a rule that came from an
    agent file does not report itself as a session rule.  The default keeps
    the session layer's own messages byte-for-byte what they were.
    """
    rules: list[Rule] = []
    errors: list[str] = []
    for index, entry in enumerate(rows):
        rule, error = _rule_from(entry, deny_only=False)
        if error:
            errors.append(f"{label} {index + 1}: {error}")
            continue
        assert rule is not None
        rules.append(rule)
    return tuple(rules), tuple(errors)


def load_rules(path: Path) -> tuple[Ruleset, list[str]]:
    """Read a preference file: the rules an operator wrote for themselves.

    A preference file may allow, ask or deny.  A project file may override
    a user file (opencode's rule for its own ``permissions`` key).
    """
    return _load(Path(path), deny_only=False)


def load_policy(path: Path) -> tuple[Ruleset, list[str]]:
    """Read a policy file: global, deny-only, and the last word.

    The policy class is where a decision cannot be loosened.  It is global
    on purpose -- a project must not be able to talk its way out of it.
    """
    return _load(Path(path), deny_only=True)


#: The file names, in one place: the preference class and the policy class.
PREFERENCE_FILE = "permissions.yaml"
POLICY_FILE = "policies.yaml"


def load_file_rules(workspace: str | Path | None = None) -> FileRules:
    """Read the three rule files: the user's, the project's, and the policy.

    The user's file and the policy file live in the user's config directory
    -- they follow the machine.  The project's file lives in the workspace,
    so it can be committed with the project and travel with it.

    A missing file is not an error: most sessions have none.  A broken one
    is reported, never swallowed.
    """
    home = config_home()
    user, user_errors = load_rules(home / PREFERENCE_FILE)
    project: Ruleset = ()
    project_errors: list[str] = []
    if workspace:
        root = Path(workspace).expanduser().resolve()
        project, project_errors = load_rules(root / PREFERENCE_FILE)
    policy, policy_errors = load_policy(home / POLICY_FILE)
    return FileRules(
        user=user,
        project=project,
        policy=policy,
        errors=(*user_errors, *project_errors, *policy_errors),
    )
