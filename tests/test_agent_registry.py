"""TDD (M1b T1): where agents come from — discovery, validation, precedence.

The loader is the only place that reads the filesystem, and it is the only
place that can decide an agent file is broken.  Two rules shape it:

* **fail closed, whole file**: a file whose frontmatter or whose rule rows do
  not read is skipped *entirely*, with the reason kept — an agent that
  half-loaded is an agent whose rules nobody can explain.
* **one id, one agent**: the same id in a nearer scope replaces the farther
  one *whole* (no field merging), so a project file is a different agent, not
  a patch of the user's.
"""

from __future__ import annotations

from pathlib import Path

from ai_coding.agents.registry import builtin_catalog, load_agents
from ai_coding.config.models import AIConfig
from ai_coding.paths import Paths


def _write(root: Path, name: str, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


# ---- the built-in four ------------------------------------------------------


def test_the_builtin_four_are_there_and_know_what_they_are():
    catalog = builtin_catalog()
    assert sorted(catalog.ids()) == ["build", "explore", "general", "plan"]
    assert sorted(spec.id for spec in catalog.primary()) == ["build", "plan"]
    assert sorted(spec.id for spec in catalog.subagents()) == ["explore", "general"]


def test_the_plan_agent_has_no_rules_yet():
    """M1b keeps plan's read-only on the overlay; T5/T6 write its rules."""
    assert builtin_catalog().get("plan").permissions == ()


def test_explore_denies_everything_and_allows_the_read_only_ones():
    rows = builtin_catalog().get("explore").permissions
    assert rows[0] == {"action": "*", "resource": "*", "effect": "deny"}
    allowed = {row["action"] for row in rows[1:]}
    assert allowed == {
        "read",
        "glob",
        "grep",
        "webfetch",
        "websearch",
        "skill",
        "memory_search",
        "history_search",
    }


# ---- discovery --------------------------------------------------------------


def test_a_frontmatter_file_becomes_an_agent(tmp_path: Path):
    _write(
        tmp_path,
        "reviewer.md",
        """---
id: reviewer
name: Reviewer
description: Reviews the diff
mode: primary
hidden: false
permissions:
  - {action: read, resource: "*", effect: allow}
---
You review changes.
""",
    )
    spec = load_agents([tmp_path]).get("reviewer")
    assert spec is not None
    assert spec.name == "Reviewer"
    assert spec.description == "Reviews the diff"
    assert spec.kind == "primary"
    assert spec.hidden is False
    assert spec.body.strip() == "You review changes."
    assert spec.permissions == ({"action": "read", "resource": "*", "effect": "allow"},)
    assert spec.source == "project"
    assert spec.path.endswith("reviewer.md")


def test_the_defaults_are_the_filename_primary_and_not_hidden(tmp_path: Path):
    _write(tmp_path, "helper.md", "---\nname: Helper\n---\nBody\n")
    spec = load_agents([tmp_path]).get("helper")
    assert spec is not None
    assert spec.kind == "primary"
    assert spec.hidden is False


def test_the_filename_is_the_id_unless_the_file_says_otherwise(tmp_path: Path):
    _write(tmp_path, "helper.md", "---\nid: renamed\n---\n")
    catalog = load_agents([tmp_path])
    assert "renamed" in catalog.ids()
    assert "helper" not in catalog.ids()


def test_a_nearer_scope_replaces_the_whole_agent(tmp_path: Path):
    user = tmp_path / "user"
    project = tmp_path / "project"
    _write(user, "helper.md", "---\nname: User's\ndescription: mine\n---\n")
    _write(project, "helper.md", "---\nname: Project's\n---\n")
    spec = load_agents([user, project]).get("helper")
    assert spec is not None
    assert spec.name == "Project's"
    # Whole replacement, not a merge: the user's description is gone.
    assert spec.description == ""


def test_a_project_agent_replaces_a_builtin_whole(tmp_path: Path):
    _write(tmp_path, "build.md", "---\nname: Builder\n---\n")
    spec = load_agents([tmp_path]).get("build")
    assert spec is not None
    assert spec.name == "Builder"


# ---- fail closed ------------------------------------------------------------


def test_a_broken_file_is_skipped_whole_and_the_reason_is_kept(tmp_path: Path):
    _write(tmp_path, "broken.md", "---\nid: [unclosed\n---\nbody\n")
    _write(tmp_path, "fine.md", "---\nid: fine\n---\n")
    catalog = load_agents([tmp_path])
    assert "broken" not in catalog.ids()
    assert "fine" in catalog.ids()
    assert any("broken.md" in reason for reason in catalog.skipped)


def test_an_unknown_mode_skips_the_whole_file(tmp_path: Path):
    _write(tmp_path, "weird.md", "---\nmode: sideways\n---\n")
    catalog = load_agents([tmp_path])
    assert "weird" not in catalog.ids()
    assert any("mode" in reason for reason in catalog.skipped)


def test_an_illegal_rule_row_skips_the_whole_file(tmp_path: Path):
    _write(
        tmp_path,
        "badrule.md",
        """---
permissions:
  - {action: read, resource: "*", effect: maybe}
---
""",
    )
    catalog = load_agents([tmp_path])
    assert "badrule" not in catalog.ids()
    assert any("effect" in reason for reason in catalog.skipped)


def test_a_rule_row_without_a_resource_skips_the_whole_file(tmp_path: Path):
    _write(
        tmp_path,
        "badrule.md",
        """---
permissions:
  - {action: read, effect: allow}
---
""",
    )
    catalog = load_agents([tmp_path])
    assert "badrule" not in catalog.ids()
    assert catalog.skipped


def test_an_unknown_action_is_not_rejected(tmp_path: Path):
    """The action space is open (MCP's ``server__tool``, plugin tools)."""
    _write(
        tmp_path,
        "mcpish.md",
        """---
permissions:
  - {action: "fs__read_file", resource: "*", effect: ask}
---
""",
    )
    assert "mcpish" in load_agents([tmp_path]).ids()


def test_permissions_that_are_not_a_list_skip_the_file(tmp_path: Path):
    _write(tmp_path, "badperm.md", "---\npermissions: nope\n---\n")
    assert "badperm" not in load_agents([tmp_path]).ids()


# ---- where the files live ---------------------------------------------------


def test_the_agent_roots_are_lowest_precedence_first(tmp_path: Path):
    """The user's library first, the project's last — the project wins."""
    paths = Paths.for_workspace(tmp_path)
    assert paths.agent_roots == (paths.user_agents, paths.project_agents)
    assert paths.agent_roots[0].name == "agents"
    assert paths.agent_roots[-1].parts[-2:] == (".ai-coding", "agents")


def test_the_default_agent_is_build_until_the_catalog_says_otherwise():
    assert AIConfig().default_agent == "build"
    assert builtin_catalog().default_id(AIConfig().default_agent) == "build"
