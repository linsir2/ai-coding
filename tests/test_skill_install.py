"""Installing a skill from a directory or a git repository.

A skill is not a file the agent reads once — it is an instruction the model
will follow from then on, with the same weight as anything in the system prompt.
So installing one is a supply-chain decision, and the rules here come from that:

* nothing is fetched without saying where it came from;
* a name is never silently overwritten;
* a file that is not a real ``SKILL.md`` is refused rather than installed broken;
* a symlink is refused, because a repository can carry
  ``skills/x/SKILL.md -> /etc/shadow`` and nothing else about it looks odd.
"""

import asyncio
from pathlib import Path

import pytest

from ai_coding.skills.install import (
    InstallError,
    _looks_local,
    git_source_url,
    install_from_git,
    install_from_path,
    list_installed,
    remove_skill,
)


def _skill_dir(root: Path, name: str, description: str = "a skill", body: str = "body") -> Path:
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    return target


# ---- from a local directory ------------------------------------------------


def test_a_single_skill_directory_is_installed(tmp_path):
    source = _skill_dir(tmp_path / "src", "tidy")
    library = tmp_path / "library"

    result = install_from_path(source, library)

    assert result.installed == ["tidy"]
    assert (library / "tidy" / "SKILL.md").is_file()


def test_a_parent_of_skills_is_installed_whole(tmp_path):
    source = tmp_path / "src"
    _skill_dir(source, "one")
    _skill_dir(source, "two")
    library = tmp_path / "library"

    result = install_from_path(source, library)

    assert sorted(result.installed) == ["one", "two"]


def test_the_skill_lands_where_it_was_told_to_not_wherever_the_last_one_went(tmp_path):
    """Installing into the global library and then into a project's must not
    leave both in the first place."""
    source = _skill_dir(tmp_path / "src", "tidy")
    project = tmp_path / "proj"

    install_from_path(source, project)
    install_from_path(source, tmp_path / "global")

    assert (project / "tidy").is_dir(), "the project's own copy is still there"
    assert (tmp_path / "global" / "tidy").is_dir(), "and so is the global one"
    assert project != tmp_path / "global", "the second call went somewhere else"


# ---- refusing ---------------------------------------------------------------


def test_an_existing_skill_is_not_overwritten_by_accident(tmp_path):
    source = _skill_dir(tmp_path / "src", "tidy", description="the new one")
    library = tmp_path / "library"
    _skill_dir(library, "tidy", description="the one I wrote")

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, library)

    assert "tidy" in str(excinfo.value)
    assert "already exists" in str(excinfo.value)
    kept = (library / "tidy" / "SKILL.md").read_text(encoding="utf-8")
    assert "the one I wrote" in kept, "the existing file is untouched"


def test_force_overwrites_and_says_that_it_did(tmp_path):
    source = _skill_dir(tmp_path / "src", "tidy", description="the new one")
    library = tmp_path / "library"
    _skill_dir(library, "tidy", description="the old one")

    result = install_from_path(source, library, force=True)

    assert result.replaced == ["tidy"]
    assert "the new one" in (library / "tidy" / "SKILL.md").read_text(encoding="utf-8")


def test_a_directory_with_no_skill_is_refused_rather_than_silently_empty(tmp_path):
    empty = tmp_path / "src"
    (empty / "notes").mkdir(parents=True)

    with pytest.raises(InstallError) as excinfo:
        install_from_path(empty, tmp_path / "library")

    assert "no SKILL.md" in str(excinfo.value)


def test_a_skill_whose_frontmatter_does_not_parse_is_refused(tmp_path):
    """Installing a broken skill would put something in the catalog that says
    nothing about itself except that it is broken."""
    source = tmp_path / "src" / "broken"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: [unclosed\ndescription: d\n---\nb\n")

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, tmp_path / "library")

    assert "无法解析" in str(excinfo.value) or "parse" in str(excinfo.value).lower()


def test_a_skill_without_a_description_is_refused(tmp_path):
    """The description is the only thing the model has when deciding whether to
    load it. A skill with none is invisible in the catalog and cannot be chosen."""
    source = tmp_path / "src" / "quiet"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: quiet\n---\nbody\n")

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, tmp_path / "library")

    assert "description" in str(excinfo.value)


def test_a_symlink_is_refused(tmp_path):
    """A repository can carry `skills/x/SKILL.md -> /etc/shadow` and nothing
    about the listing looks wrong."""
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: sneaky\ndescription: d\n---\nbody\n", encoding="utf-8")
    source = tmp_path / "src" / "sneaky"
    source.mkdir(parents=True)
    (source / "SKILL.md").symlink_to(outside)

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, tmp_path / "library")

    assert "symlink" in str(excinfo.value).lower()


def test_a_name_that_would_escape_the_library_is_refused(tmp_path):
    source = tmp_path / "src"
    _skill_dir(source, "ok")
    (source / "..").resolve()
    sneaky = source / "escape"
    sneaky.mkdir()
    (sneaky / "SKILL.md").write_text(
        "---\nname: escape\ndescription: d\n---\nb\n", encoding="utf-8"
    )

    install_from_path(sneaky, tmp_path / "library")

    assert not (tmp_path.parent / "escape").exists(), "nothing lands outside the library"


def test_supporting_files_come_along(tmp_path):
    """A skill may ship `scripts/`; a half-copy leaves the model following
    instructions that point at files that are not there."""
    source = _skill_dir(tmp_path / "src", "with-script")
    (source / "scripts").mkdir()
    (source / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")

    install_from_path(source, tmp_path / "library")

    assert (tmp_path / "library" / "with-script" / "scripts" / "run.py").is_file()


# ---- listing and removing ---------------------------------------------------


def test_listing_says_where_each_skill_came_from(tmp_path):
    library = tmp_path / "library"
    _skill_dir(library, "tidy", description="one")

    rows = list_installed(library)

    assert [(row["name"], row["description"]) for row in rows] == [("tidy", "one")]


def test_listing_an_empty_library_is_empty_not_an_error(tmp_path):
    assert list_installed(tmp_path / "nothing") == []


def test_removing_takes_the_whole_skill_directory(tmp_path):
    library = tmp_path / "library"
    source = _skill_dir(tmp_path / "src", "tidy")
    (source / "scripts").mkdir()
    (source / "scripts" / "run.py").write_text("x\n")
    install_from_path(source, library)

    assert remove_skill(library, "tidy") is True
    assert not (library / "tidy").exists()


def test_removing_a_name_that_is_not_there_says_so(tmp_path):
    assert remove_skill(tmp_path / "library", "nope") is False


def test_a_pending_directory_is_not_presented_as_installed(tmp_path):
    """`.pending` holds generated skills waiting for approval; listing them as
    installed would be claiming a thing is in place that is not."""
    library = tmp_path / "library"
    staged = library / ".pending" / "waiting"
    staged.mkdir(parents=True)
    (staged / "SKILL.md").write_text("---\nname: waiting\ndescription: d\n---\nb\n")

    assert list_installed(library) == []

# ---- the shapes a repository actually has ---------------------------------
#
# Found by pointing the local-path installer at a repo: it looked one level
# deep, so `skill install ./some-repo` failed on a repo whose skills live in
# `skills/`, while the git path handled that layout. The same repo installed one
# way and not the other.


def test_a_repository_directory_is_installed_by_pointing_at_it(tmp_path):
    repo = tmp_path / "repo"
    _skill_dir(repo / "skills", "deploy")
    _skill_dir(repo / "skills", "rollback")

    result = install_from_path(repo, tmp_path / "library")

    assert sorted(result.installed) == ["deploy", "rollback"]


def test_the_claude_and_agents_layouts_are_recognised_too(tmp_path):
    """Both are first-class in OpenCode, so a skill taken from either has to
    install without being repacked first."""
    for layout in (".claude/skills", ".agents/skills"):
        library = tmp_path / ("lib-" + layout.replace("/", "-").replace(".", ""))
        (tmp_path / layout).mkdir(parents=True)
        _skill_dir(tmp_path / layout, "from-here")

        assert sorted(install_from_path(tmp_path, library).installed) == ["from-here"]


def test_a_directory_that_is_itself_a_skill_is_not_captured_by_a_nested_one(tmp_path):
    """`skill install ./my-skill` has to mean *that* skill, even if the
    directory also happens to contain a `skills/` folder."""
    outer = tmp_path / "my-skill"
    outer.mkdir()
    (outer / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: the outer one\n---\nouter body\n",
        encoding="utf-8",
    )
    (outer / "skills" / "nested").mkdir(parents=True)
    (outer / "skills" / "nested" / "SKILL.md").write_text(
        "---\nname: nested\ndescription: the nested one\n---\nnested body\n",
        encoding="utf-8",
    )

    result = install_from_path(outer, tmp_path / "library")

    assert result.installed == ["my-skill"]


# ---- SKL-02: a symlink at any depth is refused ------------------------------


def test_a_nested_directory_symlink_is_refused(tmp_path):
    """`copytree(symlinks=False)` dereferences, so `scripts -> /home/you`
    would copy the host directory into the skill library."""
    source = _skill_dir(tmp_path / "src", "tidy")
    (tmp_path / "outside").mkdir()
    (source / "scripts").symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, tmp_path / "library")

    message = str(excinfo.value)
    assert "symlink" in message.lower()
    assert "scripts" in message


def test_a_nested_file_symlink_is_refused(tmp_path):
    source = _skill_dir(tmp_path / "src", "tidy")
    outside = tmp_path / "secret"
    outside.write_text("host secret\n", encoding="utf-8")
    (source / "reference").mkdir()
    (source / "reference" / "secret").symlink_to(outside)

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, tmp_path / "library")

    message = str(excinfo.value)
    assert "symlink" in message.lower()
    assert "reference/secret" in message or "reference\\secret" in message


def test_a_dangling_symlink_is_refused(tmp_path):
    source = _skill_dir(tmp_path / "src", "tidy")
    (source / "missing").symlink_to(tmp_path / "does-not-exist")

    with pytest.raises(InstallError) as excinfo:
        install_from_path(source, tmp_path / "library")

    assert "symlink" in str(excinfo.value).lower()


def test_a_container_level_symlink_is_refused(tmp_path):
    """The scan runs on the whole source, not just the chosen candidate:
    `repo/skills -> /host/skills` must not slip past `_candidates`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "host-skills").mkdir()
    _skill_dir(tmp_path / "host-skills", "outside")
    (repo / "skills").symlink_to(tmp_path / "host-skills", target_is_directory=True)

    with pytest.raises(InstallError) as excinfo:
        install_from_path(repo, tmp_path / "library")

    message = str(excinfo.value)
    assert "symlink" in message.lower()
    assert "skills" in message


def test_the_production_call_site_refuses_a_symlinked_skills_root(tmp_path):
    """`install_from_git` hands ``install_from_path`` the repository's own
    ``skills`` directory. Resolving that root first unbinds the link, so the
    internal walk only ever saw the target -- a checkout could point ``skills``
    at any host directory and have it copied into the library."""
    host = tmp_path / "host-skills"
    _skill_dir(host, "outside")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "skills").symlink_to(host, target_is_directory=True)
    library = tmp_path / "library"

    with pytest.raises(InstallError) as excinfo:
        install_from_path(repo / "skills", library)

    assert "symlink" in str(excinfo.value).lower()
    assert not (library / "outside").exists()
    assert not (library / "skills").exists()
    assert not library.exists() or list(library.iterdir()) == []


# ---- SKL-05: a local path is not a repository reference --------------------


def test_a_dot_slash_path_is_not_a_repository_reference():
    with pytest.raises(InstallError) as excinfo:
        git_source_url("./x")

    assert "local path" in str(excinfo.value)


def test_absolute_and_home_paths_are_not_repository_references():
    for source in ("/tmp/x", "~/x"):
        with pytest.raises(InstallError):
            git_source_url(source)


def test_owner_repo_still_maps_to_github():
    assert git_source_url("acme/skills") == "https://github.com/acme/skills.git"


def test_full_url_and_scp_style_are_unchanged():
    assert git_source_url("https://example.com/acme/skills") == (
        "https://example.com/acme/skills"
    )
    assert git_source_url("git@example.com:acme/skills.git") == (
        "git@example.com:acme/skills.git"
    )


def test_an_existing_relative_directory_is_local(tmp_path, monkeypatch):
    (tmp_path / "acme" / "skills").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    assert _looks_local("acme/skills") is True


def test_install_from_git_delegates_a_local_directory(tmp_path):
    """`skill install ./repo` reaches install_from_git via the CLI; it must
    install the working tree, not clone `github.com/./repo`."""
    source = _skill_dir(tmp_path / "src", "tidy")

    result = asyncio.run(install_from_git(str(source), tmp_path / "library"))

    assert result.installed == ["tidy"]
    assert (tmp_path / "library" / "tidy" / "SKILL.md").is_file()
