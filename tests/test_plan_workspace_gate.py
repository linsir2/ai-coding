"""/plan refuses to start when its target lives outside the workspace."""

from types import SimpleNamespace

import pytest

from ai_coding.core.plan_runner import outside_workspace_paths, plan_only
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.session import SessionData


def test_finds_the_real_target(tmp_path):
    text = "把项目放到 /home/linsir365/projects/awesome-tool-kit/hotel 里并跑 pytest"
    assert outside_workspace_paths([text], str(tmp_path)) == [
        "/home/linsir365/projects/awesome-tool-kit/hotel"
    ]


def test_ignores_routes_urls_and_inside_paths(tmp_path):
    text = (
        f"GET /rooms 与 /health；参考 https://x.dev/home/a/b；"
        f"项目在 {tmp_path}/app；另见 /etc/passwd"
    )
    assert outside_workspace_paths([text], str(tmp_path)) == ["/etc/passwd"]


class _StubLoop:
    def __init__(self, workspace, *, items=(), extra_roots=(), armed=False):
        self.workspace = workspace
        self.settings = SimpleNamespace(
            plan_step_max_iterations=8, plan_max_step_turns=0, stop_policy="todo_aware"
        )
        self.policy = TurnPolicy(TurnPolicyConfig(max_iterations=100))
        self.calls = []
        self._items = tuple(items)
        self.extra_workspace_roots = tuple(extra_roots)
        self.closer = SimpleNamespace(save=lambda session: None)
        #: the human approved the plan inside the planning turn, so the task
        #: is armed by the time the second gate runs (present_plan arms at
        #: approval time).
        self._armed = armed

    async def process_input(self, session, text, on_delta=None, observer=None, **kwargs):
        self.calls.append(text)
        session.begin_plan("t")
        for content in self._items:
            session.task.add(content)
        if self._armed:
            session.task.armed = True
            session.task.armed_profile = "plan"
        return "planned"


def _session():
    return SessionData(session_id="s", title="", created_time="t", last_access_time="t")


@pytest.mark.asyncio
async def test_outside_topic_stops_before_any_planning(tmp_path):
    loop = _StubLoop(str(tmp_path))
    report = await plan_only(loop, _session(), topic="把项目建到 /home/x/y")
    assert report.stopped is not None and "/home/x/y" in report.stopped
    assert loop.calls == []


@pytest.mark.asyncio
async def test_outside_plan_item_stops_before_execution(tmp_path):
    loop = _StubLoop(str(tmp_path), items=("写代码到 /home/x/y/app.py",))
    report = await plan_only(loop, _session(), topic="实现一个小系统")
    assert report.stopped is not None and "/home/x/y/app.py" in report.stopped
    assert len(loop.calls) == 1  # planned once; the caller never runs it


def test_a_configured_extra_root_is_not_reported_as_outside(tmp_path):
    """Regression (B, found while adding D5): the warning asked "can the file
    tools reach this?", and answered with a Sandbox built from the workspace
    alone.  A directory the operator declared in ``tools.extra_workspace_roots``
    is reachable, so reporting it as unreachable told the model to switch
    workspaces for a path that already works."""
    other = tmp_path / "sibling"
    other.mkdir()
    text = f"看下 {other}/app.py"

    assert outside_workspace_paths([text], str(tmp_path), extra_roots=(other,)) == []


def test_a_shell_text_reports_a_path_the_shape_heuristic_would_skip(tmp_path):
    """W2/T: the grammar confirms these are words in a command, so the shape
    heuristic has nothing left to say about them.

    ``D=/data/x`` names a path with no extension, under a first segment
    this machine has never heard of, and with no directory words around
    it -- the shape heuristic passes it over.  The grammar does not: it is
    a word in a command, and a word that starts at the root is a path.
    """
    assert outside_workspace_paths(
        ["D=/data/x; rm -rf $D"], str(tmp_path), from_shell=True
    ) == ["/data/x"]


def test_a_shell_text_still_passes_a_declared_root(tmp_path):
    """The shell reading does not change what is reachable."""
    other = tmp_path / "sibling"
    other.mkdir()
    text = f"test -f {other}/app.py"

    assert outside_workspace_paths(
        [text], str(tmp_path), extra_roots=(other,), from_shell=True
    ) == []


def test_a_prose_text_keeps_the_shape_heuristic(tmp_path):
    """Prose is not a command.  A plan's sentence that mentions an API route
    must not turn into a workspace refusal, and the shape heuristic is what
    keeps it out."""
    text = "新增 GET /api/v1/users 接口"

    assert outside_workspace_paths([text], str(tmp_path)) == []
    assert outside_workspace_paths([text], str(tmp_path), from_shell=True) == [
        "/api/v1/users"
    ]


def test_an_api_route_is_not_a_workspace_path(tmp_path):
    """W2/R02: the candidate scan must not read an HTTP route as a file path.

    The route and a real target have the same shape (``/seg/seg``), so the
    scan needs a second question beyond "does it look absolute": does it look
    like a *file system* path at all?  A route names a resource, not a place
    on disk, and refusing a plan over it would kill legitimate work.
    """
    text = "新增 GET /api/v1/users 接口，并写 /data/x/app.py"

    assert outside_workspace_paths([text], str(tmp_path)) == ["/data/x/app.py"]


def test_a_mount_point_outside_the_old_root_list_is_found(tmp_path):
    """W2/R02: the old candidate regex knew eleven roots, so a mount point
    (``/data``, ``/workspace``) was invisible and the plan sailed through."""
    assert outside_workspace_paths(["写入 /data/x/app.py"], str(tmp_path)) == [
        "/data/x/app.py"
    ]


def test_a_directory_target_is_found_even_without_an_extension(tmp_path):
    """W2/R02 (live check): the real model writes a directory target the way
    a person does -- ``确保目录 /data/w2demo/ 存在`` -- with no extension and
    a first segment this machine has never heard of.  A gate that only
    recognised files let that plan through, and the review card went out for
    a plan the file tools cannot execute."""
    assert outside_workspace_paths(["确保目录 /data/w2demo/ 存在"], str(tmp_path)) == [
        "/data/w2demo"
    ]


def test_a_route_with_a_trailing_slash_is_not_a_workspace_path(tmp_path):
    """The trailing slash says "directory", and a route is not one.  This is
    the other side of the directory rule: it must not swallow the routes.

    A trailing slash alone cannot tell the two apart -- ``/api/v1/users/``
    and ``/data/w2demo/`` have the same shape -- so the slash is only read
    as "directory" when the words around it say so.
    """
    text = "新增 GET /api/v1/users/ 接口"
    assert outside_workspace_paths([text], str(tmp_path)) == []


def test_a_table_of_contents_is_not_a_directory(tmp_path):
    """"目录" also means a table of contents.  The word only names a
    directory when it sits right next to the path; "目录结构如下 /api/v1/"
    is a document's outline, and the words between are the other meaning."""
    text = "目录结构如下 /api/v1/"
    assert outside_workspace_paths([text], str(tmp_path)) == []


def test_a_shebang_interpreter_is_not_a_workspace_path(tmp_path):
    """W2/R02 (live check): a script's interpreter is not a file the tools
    would write.  The live plan carried ``#!/usr/bin/env python3`` inside the
    script it wrote, and the gate reported the interpreter as an unreachable
    target -- so the refusal named a path the model had no reason to change,
    and the model retried the same plan thirteen times before it moved on.

    ``#!`` says "the interpreter runs this", which is a different thing from
    "the file tools touch this", and it says so in every spelling: the line
    may be quoted into a printf, so the marker is looked for anywhere before
    the path.
    """
    text = "printf '#!/usr/bin/env python3\\nprint(1)\\n' > app.py"

    assert outside_workspace_paths([text], str(tmp_path)) == []


def test_a_shebang_exempts_only_the_interpreter(tmp_path):
    """A plan that writes a script may write another file too, and that one
    is a target.  The shebang exempts the interpreter it names, not every
    path that follows it."""
    text = "printf '#!/usr/bin/env python3\\n' > app.py；然后写 /data/x/app.py"

    assert outside_workspace_paths([text], str(tmp_path)) == ["/data/x/app.py"]


@pytest.mark.asyncio
async def test_every_gate_honours_extra_roots(tmp_path):
    """W2/R02: one roots pipeline, and the gates share it.  A directory the
    operator declared is reachable, so no gate may report it as outside.

    The runner's own gate (added with the executor front gate) has its own
    case; here the two plan gates are what this pipeline feeds.
    """
    other = tmp_path / "sibling"
    other.mkdir()
    text = f"看下 {other}/app.py"

    # gate #1: the topic
    loop = _StubLoop(str(tmp_path), extra_roots=(other,))
    report = await plan_only(loop, _session(), topic=text)
    assert report.stopped is None, "the topic gate reported a declared root as outside"
    assert len(loop.calls) == 1  # the planning turn ran

    # gate #2: the written items
    loop = _StubLoop(str(tmp_path), items=(text,), extra_roots=(other,))
    report = await plan_only(loop, _session(), topic="实现一个小系统")
    assert report.stopped is None, "the item gate reported a declared root as outside"


@pytest.mark.asyncio
async def test_the_second_gate_disarms_the_approved_plan(tmp_path):
    """W2/R02: ``present_plan`` arms the task when the human approves, and
    the second gate runs *after* that.  Refusing the plan must also withdraw
    the approval, or the armed plan stays on disk and the boot resume runs it
    (the plan was never executable in the first place)."""
    loop = _StubLoop(str(tmp_path), items=("写代码到 /data/x/app.py",), armed=True)
    session = _session()

    report = await plan_only(loop, session, topic="实现一个小系统")

    assert report.stopped is not None
    assert session.task.armed is False
    assert session.task.armed_profile == ""
