"""Interoperability with the Agent Skills standard.

Anthropic published Agent Skills as an open standard (2025-12-16) and Codex and
OpenCode both implement it, so a skill written for either should load here
unmodified. That claim is only worth something if it is checked against a real
skill rather than one written to our parser's taste.

The two fixtures below are shaped like the ones those tools ship: ``pdf`` uses
every standard field including the 1,024-character description cap and a
``metadata`` block; ``code-review`` adds Claude Code's own extension keys, which
the standard does not define and which must therefore survive as unread data
rather than break the parse or be mistaken for instructions we honour.
"""

import pytest
import yaml

from ai_coding.skills.catalog import build_catalog
from ai_coding.skills.registry import SkillRegistry, parse_skill

# A description at the standard's documented length, with the kind of value that
# makes a YAML scalar ambiguous — a colon and a space.
PDF = """---
name: pdf
description: >-
  Extract text and tables from PDF files. Use when the user asks a question
  about a specific PDF, or when a document must be read programmatically:
  the text layer is usually enough, and a scanned page needs OCR.
license: Complete terms from the Anthropic notice
compatibility: claude-code>=1.0
allowed-tools:
  - Read
  - Bash(python:*)
metadata:
  category: documents
  version: 1.2.0
---

# PDF

## Steps

1. Try `pdftotext` first.
2. Fall back to python only if the text layer is empty.

## Constraints

Never install a package without asking first.
"""

# Claude Code extensions the standard does not define. `context: fork` and
# `effort` in particular are instructions to *that* runtime; honouring them here
# would be claiming a behaviour we do not have.
CODE_REVIEW = """---
name: code-review
description: Review a diff for correctness and security before it lands.
allowed-tools: [Read, Grep]
context: fork
agent: reviewer
effort: high
disable-model-invocation: true
hooks:
  - PreToolUse: block-on-secrets
---

# Code review

Read the diff, then the surrounding code it depends on.
"""


def _write(tmp_path, name, text):
    target = tmp_path / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(text, encoding="utf-8")
    return target / "SKILL.md"


def test_a_standard_skill_parses_with_every_field_intact(tmp_path):
    skill = parse_skill(_write(tmp_path, "pdf", PDF))

    assert skill.is_healthy
    assert skill.id == "pdf"
    assert "Extract text and tables" in skill.description
    assert skill.license == "Complete terms from the Anthropic notice"
    assert skill.compatibility == "claude-code>=1.0"
    assert skill.allowed_tools == ("Read", "Bash(python:*)")
    assert skill.metadata == {"category": "documents", "version": "1.2.0"}


def test_the_body_arrives_without_the_frontmatter(tmp_path):
    """The catalog already showed the description; handing the model the header
    again spends context on data it has read."""
    skill = parse_skill(_write(tmp_path, "pdf", PDF))

    assert not skill.body.startswith("---")
    assert "Try `pdftotext` first." in skill.body
    assert "Never install a package" in skill.body, "the last section is not cut"


def test_a_folded_description_keeps_its_text(tmp_path):
    """`>-` is how a long description is written once it outgrows one line, and
    a skill whose description is silently empty is one the model can never
    choose."""
    skill = parse_skill(_write(tmp_path, "pdf", PDF))

    assert skill.description.endswith("needs OCR.")
    assert "\n" not in skill.description, "a folded scalar is one string"


def test_fields_another_runtime_defines_do_not_break_the_parse(tmp_path):
    skill = parse_skill(_write(tmp_path, "code-review", CODE_REVIEW))

    assert skill.is_healthy, "keys we do not define must not make a skill unreadable"
    assert skill.description == "Review a diff for correctness and security before it lands."
    assert skill.allowed_tools == ("Read", "Grep")


def test_those_fields_are_kept_rather_than_silently_dropped(tmp_path):
    """Dropping them is how an author ends up believing `disable-model-invocation`
    or `context: fork` is doing something here."""
    skill = parse_skill(_write(tmp_path, "code-review", CODE_REVIEW))

    assert skill.extra["context"] == "fork"
    assert skill.extra["disable-model-invocation"] is True
    assert skill.extra["effort"] == "high"


def test_and_are_not_presented_as_instructions_we_honour(tmp_path):
    """They are inert data. Nothing in this codebase reads them, and the catalog
    must not imply otherwise by echoing them back."""
    skill = parse_skill(_write(tmp_path, "code-review", CODE_REVIEW))

    catalog = build_catalog([skill])

    assert "fork" not in catalog
    assert "disable-model-invocation" not in catalog


def test_a_standard_skill_is_discovered_listed_and_loadable_end_to_end(tmp_path):
    from ai_coding.tools.skill_tool import SkillTool

    _write(tmp_path, "pdf", PDF)
    _write(tmp_path, "code-review", CODE_REVIEW)
    registry = SkillRegistry(tmp_path)
    tool = SkillTool(registry)

    assert registry.names() == ["code-review", "pdf"]
    catalog = build_catalog(registry.all())
    assert "- pdf: Extract text and tables" in catalog

    import asyncio

    result = asyncio.run(tool.execute({"action": "read", "name": "pdf"}))

    assert result.success
    assert "Try `pdftotext` first." in result.output
    assert "---" not in result.output.splitlines()[0]


def test_the_alias_lets_a_skill_load_when_the_directory_was_renamed(tmp_path):
    """`name:` is the standard's identity; the directory is ours. A skill copied
    into a differently-named directory still has to load."""
    _write(tmp_path, "renamed-on-disk", PDF)

    registry = SkillRegistry(tmp_path)

    assert registry.resolve("pdf").id == "renamed-on-disk"
    assert registry.get("renamed-on-disk").alias == "pdf"


@pytest.mark.parametrize(
    "description",
    [
        "A" * 1024,
        "short",
        "Ends with a colon:",
        "Contains 'single' and 'double' quotes",
        "有中文的描述：带冒号",
        "构建 AWS ECS 镜像",
    ],
)
def test_descriptions_that_break_yaml_still_parse(tmp_path, description):
    """A description is the only thing the model has when deciding whether to
    load a skill. `description: Build for AWS: ECS` is how people write things,
    and a description lost to a YAML scanner error is a skill the model can
    never choose."""
    # Build the frontmatter through yaml.dump itself so the value is encoded
    # correctly; the interesting part is that our parser survives a long
    # description, one with a colon, and non-ASCII — not that we hand YAML bad
    # input here.
    header = yaml.safe_dump({"name": "tricky", "description": description}, allow_unicode=True)
    text = f"---\n{header}---\n\n# Body\n\nsteps\n"

    skill = parse_skill(_write(tmp_path, "tricky", text))

    assert skill.is_healthy
    assert skill.description == description


def test_a_raw_unquoted_colon_in_a_description_is_repaired(tmp_path):
    """The one input that genuinely arrives malformed: the author typed a colon
    and did not quote it. The value has to survive, because it is the only
    handle the model has on the skill."""
    text = "---\nname: aws\ndescription: Build for AWS: ECS\n---\n\nbody\n"

    skill = parse_skill(_write(tmp_path, "aws", text))

    assert skill.is_healthy
    assert skill.description == "Build for AWS: ECS"
