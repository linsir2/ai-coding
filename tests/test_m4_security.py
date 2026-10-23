"""M4a security boundaries: credentials, plan writes, and proof commands."""

from __future__ import annotations

import os

import pytest

from ai_coding.core.acceptance import _needs_a_human, verify_items
from ai_coding.core.dispatch import ToolDispatcher
from ai_coding.domain.acceptance import AcceptanceItem
from ai_coding.domain.message import ToolCallRef
from ai_coding.infra.subprocess_runner import ProcessOutcome
from ai_coding.security.approval import (
    ApprovalCallback,
    ApprovalResult,
    AutoApproveCallback,
    CLIApprovalCallback,
)
from ai_coding.security.file_state_tracker import FileStateTracker
from ai_coding.security.permission_gate import PermissionDecision, PermissionGate
from ai_coding.security.sandbox import Sandbox, SandboxViolation, open_inside
from ai_coding.tools.base import ToolContext
from ai_coding.tools.edit_tool import EditTool
from ai_coding.tools.grep_tool import GrepTool
from ai_coding.tools.read_tool import ReadTool
from ai_coding.tools.registry import ToolRegistry
from ai_coding.tools.write_tool import WriteTool


class _RecordingApproval:
    """A human who answers, and a record of what they were asked."""

    def __init__(self, answer: ApprovalResult) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict[str, str], str]] = []

    async def approve(
        self, tool_name: str, params: dict[str, str], reason: str
    ) -> ApprovalResult:
        self.asked.append((tool_name, dict(params), reason))
        return self.answer


def _item(command: str) -> AcceptanceItem:
    return AcceptanceItem(id="AC-1", statement="proof", verify=command)


# ---- L3: an obvious path written inside a string is still a path ----------


@pytest.mark.asyncio
async def test_a_python_one_liner_cannot_read_outside(tmp_path):
    """The quoted argument is a string to Python, but a path to the boundary."""
    secret = tmp_path.parent / "m4-proof-secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    approval = _RecordingApproval(ApprovalResult.NO)

    result = await verify_items(
        [_item('python -c "print(open(\'../m4-proof-secret.txt\').read())"')],
        str(tmp_path),
        approval=approval,
    )

    assert len(approval.asked) == 1
    assert result[0].passed is False


@pytest.mark.asyncio
async def test_a_single_quoted_python_one_liner_is_seen(tmp_path):
    """A raw string is still a shell argument; the grammar must keep its text."""
    secret = tmp_path.parent / "m4-proof-single-secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    approval = _RecordingApproval(ApprovalResult.NO)

    result = await verify_items(
        [_item("python -c 'print(open(\"../m4-proof-single-secret.txt\").read())'")],
        str(tmp_path),
        approval=approval,
    )

    assert len(approval.asked) == 1
    assert result[0].passed is False


@pytest.mark.parametrize(
    "command",
    [
        "grep 'a/b' f",
        "pytest tests/unit",
        "curl https://x.dev/a/b",
        "python -c \"open('./inside.txt')\"",
    ],
)
def test_url_grep_pattern_and_pytest_target_are_not_paths(tmp_path, command):
    """The literal scan must not turn ordinary command text into a prompt."""
    assert _needs_a_human(command, str(tmp_path)) == ""


def test_a_dynamic_path_is_not_claimed_closed(tmp_path):
    """This layer promises obvious literals, not runtime string construction."""
    command = 'python -c "name=\'secret.txt\'; print(open(\'../\' + name).read())"'
    assert _needs_a_human(command, str(tmp_path)) == ""


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"open('../{}'.format(name)).read()\"",
        "python -c \"open('../%s' % name).read()\"",
    ],
)
def test_dynamic_string_construction_is_a_known_limitation(tmp_path, command):
    """known-limitation: runtime-built paths are not promised closed."""
    assert _needs_a_human(command, str(tmp_path)) == ""


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"open('../a/b'.format(x)).read()\"",
        "python -c \"open('../a/b' % x).read()\"",
        "node -e \"require('../a/b')\"",
        "node -e \"require('../x')\"",
    ],
)
def test_format_percent_and_node_literals_are_detected(tmp_path, command):
    """Obvious literals inside .format/%/node -e are still paths."""
    assert _needs_a_human(command, str(tmp_path)) != ""


# ---- L2: credentials are one vocabulary and one approval decision ----------


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".npmrc",
        "id_rsa",
        "server.pem",
        ".ssh/id_rsa",
        ".aws/credentials",
        ".docker/config.json",
    ],
)
def test_credentials_match_names_suffixes_and_directory_shapes(path):
    from ai_coding.security.credentials import is_credential_path

    assert is_credential_path(path)


@pytest.mark.parametrize("path", [".env.example", "src/main.py", "docs/config.txt"])
def test_ordinary_files_are_not_credentials(path):
    from ai_coding.security.credentials import is_credential_path

    assert not is_credential_path(path)


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("read", {"path": ".env"}),
        ("read", {"path": ".npmrc"}),
        ("read", {"path": "id_rsa"}),
        ("grep", {"pattern": "TOKEN", "path": ".env"}),
        ("grep", {"pattern": "TOKEN", "path": "id_rsa"}),
    ],
)
def test_read_and_grep_ask_for_credentials(tmp_path, tool, params):
    gate = PermissionGate(workspace=str(tmp_path))

    assert gate.check(tool, params).decision is PermissionDecision.WARN


def test_env_example_stays_a_readable_template(tmp_path):
    gate = PermissionGate(workspace=str(tmp_path))

    assert gate.check("read", {"path": ".env.example"}).decision is PermissionDecision.ALLOW
    assert (
        gate.check("grep", {"pattern": "TOKEN", "path": ".env.example"}).decision
        is PermissionDecision.ALLOW
    )


def test_only_the_interactive_callback_is_a_human_source():
    assert CLIApprovalCallback().is_human is True
    assert AutoApproveCallback().is_human is False
    assert ApprovalCallback().is_human is False


@pytest.mark.asyncio
async def test_auto_approval_does_not_authorize_a_credential_read(tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=secret\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))
    registry = ToolRegistry()
    registry.register(ReadTool(Sandbox(tmp_path), FileStateTracker()))
    dispatcher = ToolDispatcher(registry, gate, AutoApproveCallback())
    call = ToolCallRef(id="c1", name="read", arguments='{"path":".env"}')

    outcome = (
        await dispatcher.dispatch_batch(
            [call], ToolContext(workspace=str(tmp_path), mode="normal")
        )
    )[0]

    assert outcome.denied is True
    assert outcome.result.error == "credential_approval_required"
    assert "TOKEN=secret" not in outcome.result.output


class _HumanApproval(ApprovalCallback):
    is_human = True

    def __init__(self, answers: list[ApprovalResult]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    async def approve(self, tool_name, params, reason):
        self.asked.append(tool_name)
        return self.answers.pop(0)


@pytest.mark.asyncio
async def test_human_approval_is_scoped_to_the_one_credential_call(tmp_path):
    (tmp_path / ".env").write_text("ONE=1\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("TWO=2\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))
    registry = ToolRegistry()
    registry.register(ReadTool(Sandbox(tmp_path), FileStateTracker()))
    approval = _HumanApproval([ApprovalResult.YES, ApprovalResult.NO])
    dispatcher = ToolDispatcher(registry, gate, approval)
    ctx = ToolContext(workspace=str(tmp_path), mode="normal")

    outcomes = await dispatcher.dispatch_batch(
        [
            ToolCallRef(id="c1", name="read", arguments='{"path":".env"}'),
            ToolCallRef(id="c2", name="read", arguments='{"path":"id_rsa"}'),
        ],
        ctx,
    )

    assert outcomes[0].result.success is True
    assert outcomes[1].denied is True
    assert approval.asked == ["read", "read"]


def test_credentials_are_not_grantable_even_through_a_symlink(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "config.txt").symlink_to(tmp_path / ".env")
    (tmp_path / "notes.txt").write_text("ordinary\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))

    for tool, params in (
        ("read", {"path": ".env"}),
        ("read", {"path": "id_rsa"}),
        ("read", {"path": "config.txt"}),
        ("grep", {"pattern": "TOKEN", "path": ".env"}),
    ):
        assert gate.grant_key(tool, params) is None

    assert gate.grant_key("read", {"path": "notes.txt"}) is not None
    assert gate.grant_key("read", {"path": ".env.example"}) is not None


def test_standing_grant_alias_still_warns(tmp_path):
    """A standing grant for an ordinary name must not survive a symlink swap."""
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "config.txt").write_text("ordinary\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))

    granted = gate.grant("read", {"path": "config.txt"})
    assert granted is not None

    (tmp_path / "config.txt").unlink()
    (tmp_path / "config.txt").symlink_to(tmp_path / ".env")

    assert gate.check("read", {"path": "config.txt"}).decision is PermissionDecision.WARN
    assert (
        gate.check("grep", {"pattern": "TOKEN", "path": "config.txt"}).decision
        is PermissionDecision.WARN
    )


@pytest.mark.asyncio
async def test_explicit_credential_name_needs_approval_even_when_linked_to_plain(tmp_path):
    """Lexical credential names are fail-closed even if the target is ordinary."""
    (tmp_path / "notes.txt").write_text("plain\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(tmp_path / "notes.txt")
    (tmp_path / "plain.txt").write_text("plain\n", encoding="utf-8")
    (tmp_path / "id_rsa").symlink_to(tmp_path / "plain.txt")
    read_tool = ReadTool(Sandbox(tmp_path), FileStateTracker())
    grep_tool = GrepTool(Sandbox(tmp_path), use_ripgrep=False)

    # Raw is credential (.env, id_rsa) but the resolved target is ordinary;
    # the lexical side alone must still force credential_approval_required.
    r1 = await read_tool.execute({"path": ".env"})
    r2 = await read_tool.execute({"path": "id_rsa"})
    g1 = await grep_tool.execute({"pattern": "plain", "path": ".env"})
    g2 = await grep_tool.execute({"pattern": "plain", "path": "id_rsa"})

    assert r1.error == "credential_approval_required"
    assert r2.error == "credential_approval_required"
    assert g1.error == "credential_approval_required"
    assert g2.error == "credential_approval_required"


def test_allow_once_does_not_remember_a_credential_alias(tmp_path):
    """allow_once must use the workspace-aware key, so an alias stores nothing."""
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "config.txt").symlink_to(tmp_path / ".env")
    gate = PermissionGate(workspace=str(tmp_path))

    gate.allow_once("read", {"path": "config.txt"})

    assert gate._once_grants == set()


# ---- L2: grep filters the tree, not only the output ------------------------


@pytest.mark.asyncio
async def test_python_grep_skips_credentials_and_their_symlink_aliases(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".npmrc").write_text("TOKEN=npm\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("TOKEN=key\n", encoding="utf-8")
    (tmp_path / "config.txt").symlink_to(tmp_path / ".env")
    (tmp_path / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=False)

    result = await tool.execute({"pattern": "TOKEN"})

    assert result.success is True
    assert ".env.example:1: TOKEN=example" in result.output
    for leaked in (".env:", ".npmrc:", "id_rsa:", "config.txt:"):
        assert leaked not in result.output


@pytest.mark.asyncio
async def test_rg_gets_credential_exclusions_and_still_filters_alias_hits(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "config.txt").symlink_to(tmp_path / ".env")
    calls: list[tuple[str, ...]] = []

    async def runner(argv, *, cwd, timeout):
        calls.append(tuple(argv))
        return ProcessOutcome(
            returncode=0,
            stdout=(
                '{"type":"match","data":{"path":{"text":"config.txt"},'
                '"line_number":1,"lines":{"text":"TOKEN=secret\\n"}}}\n'
            ),
        )

    tool = GrepTool(
        Sandbox(tmp_path),
        use_ripgrep=True,
        rg_path="rg",
        runner=runner,
    )

    result = await tool.execute({"pattern": "TOKEN"})

    assert result.success is True
    assert result.output == "(no matches)"
    assert any("!**/.env" in arg for arg in calls[0])


@pytest.mark.asyncio
async def test_grep_explicit_credential_requires_the_call_approval(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=False)

    refused = await tool.execute({"pattern": "TOKEN", "path": ".env"})
    approved = await tool.execute(
        {"pattern": "TOKEN", "path": ".env"},
        ToolContext(credential_approved=True),
    )

    assert refused.success is False
    assert refused.error == "credential_approval_required"
    assert approved.success is True
    assert "TOKEN=secret" in approved.output


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("read", {"path": "server.pem"}),
        ("read", {"path": "app.key"}),
        ("read", {"path": ".ssh/id_rsa"}),
        ("read", {"path": ".aws/credentials"}),
        ("read", {"path": ".docker/config.json"}),
        ("grep", {"pattern": "TOKEN", "path": "server.pem"}),
        ("grep", {"pattern": "TOKEN", "path": ".ssh/id_rsa"}),
        ("grep", {"pattern": "TOKEN", "path": ".aws/credentials"}),
    ],
)
def test_read_and_grep_ask_for_suffix_and_directory_credentials(tmp_path, tool, params):
    gate = PermissionGate(workspace=str(tmp_path))

    assert gate.check(tool, params).decision is PermissionDecision.WARN


@pytest.mark.asyncio
async def test_grep_with_auto_approval_still_filters_credentials(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("TOKEN=key\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("TOKEN=ok\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))
    registry = ToolRegistry()
    registry.register(GrepTool(Sandbox(tmp_path), use_ripgrep=False))
    dispatcher = ToolDispatcher(registry, gate, AutoApproveCallback())
    call = ToolCallRef(id="c1", name="grep", arguments='{"pattern":"TOKEN"}')

    outcome = (
        await dispatcher.dispatch_batch(
            [call], ToolContext(workspace=str(tmp_path), mode="normal")
        )
    )[0]

    assert "TOKEN=secret" not in outcome.result.output
    assert "TOKEN=key" not in outcome.result.output
    assert "TOKEN=ok" in outcome.result.output


@pytest.mark.asyncio
async def test_always_on_a_credential_executes_once_but_remembers_nothing(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    gate = PermissionGate(workspace=str(tmp_path))
    registry = ToolRegistry()
    registry.register(ReadTool(Sandbox(tmp_path), FileStateTracker()))
    approval = _HumanApproval([ApprovalResult.ALWAYS])
    dispatcher = ToolDispatcher(registry, gate, approval)
    ctx = ToolContext(workspace=str(tmp_path), mode="normal")

    first = (
        await dispatcher.dispatch_batch(
            [ToolCallRef(id="c1", name="read", arguments='{"path":".env"}')], ctx
        )
    )[0]

    assert first.result.success is True
    assert "TOKEN=secret" in first.result.output
    assert gate.grants == ()

    approval.answers.append(ApprovalResult.NO)
    second = (
        await dispatcher.dispatch_batch(
            [ToolCallRef(id="c2", name="read", arguments='{"path":".env"}')], ctx
        )
    )[0]

    assert second.denied is True
    assert approval.asked == ["read", "read"]


@pytest.mark.asyncio
async def test_grep_include_dotenv_still_filters_credentials(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
    for use_rg in (False, True):
        tool = GrepTool(Sandbox(tmp_path), use_ripgrep=use_rg)
        result = await tool.execute({"pattern": "TOKEN", "include": "*.env"})
        assert "TOKEN=secret" not in result.output


@pytest.mark.asyncio
async def test_rg_keeps_env_example_searchable(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("TOKEN=ok\n", encoding="utf-8")
    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=True)

    result = await tool.execute({"pattern": "TOKEN"})

    assert result.success is True
    assert ".env.example:1: TOKEN=example" in result.output
    assert "a.txt:1: TOKEN=ok" in result.output
    assert ".env:" not in result.output


@pytest.mark.asyncio
async def test_rescue_skips_a_credential_symlink_named_env_example(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".env.example").symlink_to(tmp_path / ".env")
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / ".env.example").write_text("TOKEN=example\n", encoding="utf-8")
    tool = GrepTool(Sandbox(tmp_path), use_ripgrep=True)

    result = await tool.execute({"pattern": "TOKEN"})

    assert result.success is True
    assert "ok/.env.example:1: TOKEN=example" in result.output
    assert "sub/.env.example" not in result.output
    assert "TOKEN=secret" not in result.output


# ---- L1: plan writes stay on a verified descriptor chain -------------------


def test_open_inside_refuses_final_and_middle_symlinks(tmp_path):
    root = tmp_path / ".ai-coding" / "plan"
    root.mkdir(parents=True)
    outside = tmp_path / "src"
    outside.mkdir()
    (root / "final.txt").symlink_to(outside / "pwn.txt")
    (root / "nested").symlink_to(outside)

    with pytest.raises(SandboxViolation):
        open_inside(root, "final.txt", mode="write", create=True)
    with pytest.raises(SandboxViolation):
        open_inside(root, "nested/pwn.txt", mode="write", create=True)

    assert not (outside / "pwn.txt").exists()


def test_open_inside_creates_parents_and_returns_verified_fd(tmp_path):
    root = tmp_path / ".ai-coding" / "plan"

    opened = open_inside(root, "a/b/note.md", mode="write", create=True)
    try:
        os.write(opened.fd, b"hello")
        size = os.fstat(opened.fd).st_size
    finally:
        os.close(opened.fd)

    assert opened.relative_path == "a/b/note.md"
    assert size == 5
    assert (root / "a" / "b" / "note.md").read_text() == "hello"


@pytest.mark.asyncio
async def test_a_swap_after_the_gate_cannot_redirect_a_plan_write(
    tmp_path, monkeypatch
):
    workspace = tmp_path
    plan_root = workspace / ".ai-coding" / "plan"
    plan_root.mkdir(parents=True)
    outside = workspace / "src"
    outside.mkdir()
    sandbox = Sandbox(workspace)
    original = sandbox.resolve
    swapped = False

    def resolve_then_swap(path):
        nonlocal swapped
        resolved = original(path)
        if not swapped:
            swapped = True
            plan_root.rmdir()
            plan_root.symlink_to(outside)
        return resolved

    monkeypatch.setattr(sandbox, "resolve", resolve_then_swap)
    tool = WriteTool(sandbox, FileStateTracker(), plan_root=plan_root)

    result = await tool.execute(
        {"path": ".ai-coding/plan/pwn.txt", "content": "escaped"},
        ToolContext(workspace=str(workspace), mode="plan"),
    )

    assert result.success is False
    assert not (outside / "pwn.txt").exists()


@pytest.mark.asyncio
async def test_plan_write_artifact_comes_from_the_open_fd(tmp_path):
    workspace = tmp_path
    plan_root = workspace / ".ai-coding" / "plan"
    sandbox = Sandbox(workspace)
    tracker = FileStateTracker()
    tool = WriteTool(sandbox, tracker, plan_root=plan_root)
    result = await tool.execute(
        {"path": ".ai-coding/plan/note.md", "content": "hello"},
        ToolContext(workspace=str(workspace), mode="plan"),
    )

    assert result.success is True
    assert result.artifacts[0].bytes == 5
    assert result.artifacts[0].path == ".ai-coding/plan/note.md"


@pytest.mark.asyncio
async def test_plan_edit_reads_and_writes_the_same_fd(tmp_path, monkeypatch):
    workspace = tmp_path
    plan_root = workspace / ".ai-coding" / "plan"
    plan_root.mkdir(parents=True)
    target = plan_root / "note.md"
    target.write_text("hello world", encoding="utf-8")
    tracker = FileStateTracker()
    tracker.record(target)
    tool = EditTool(Sandbox(workspace), tracker, plan_root=plan_root)
    monkeypatch.setattr(
        type(target),
        "write_text",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("plan edit must write through its fd")
        ),
    )

    result = await tool.execute(
        {
            "path": ".ai-coding/plan/note.md",
            "old_string": "world",
            "new_string": "there",
        },
        ToolContext(workspace=str(workspace), mode="plan"),
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "hello there"


@pytest.mark.asyncio
async def test_build_mode_keeps_its_existing_symlink_behavior(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("old", encoding="utf-8")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(target)
    tracker = FileStateTracker()
    tracker.record(alias)
    tool = WriteTool(Sandbox(tmp_path), tracker)

    result = await tool.execute(
        {"path": "alias.txt", "content": "new"},
        ToolContext(workspace=str(tmp_path), mode="normal"),
    )

    assert result.success is True
    assert target.read_text(encoding="utf-8") == "new"


def test_walk_to_refuses_a_symlinked_parent(tmp_path):
    """_walk_to is the single fd-relative traversal; a swapped parent is refused."""
    from ai_coding.security.sandbox import _walk_to

    root = tmp_path / "plan"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "mid").symlink_to(outside)
    import os as _os

    root_fd = _os.open(str(root), _os.O_RDONLY | _os.O_DIRECTORY | _os.O_NOFOLLOW)
    try:
        import pytest as _pytest

        with _pytest.raises(SandboxViolation):
            _walk_to(root_fd, ["mid", "leaf.txt"], create=True)
    finally:
        _os.close(root_fd)
    assert not (outside / "leaf.txt").exists()


@pytest.mark.asyncio
async def test_plan_write_uses_fd_stat_not_path_stat(tmp_path, monkeypatch):
    """After resolve→execute swap, fstat(fd) and stat(path) fork; fd wins."""
    workspace = tmp_path
    plan_root = workspace / ".ai-coding" / "plan"
    plan_root.mkdir(parents=True)
    target = plan_root / "note.md"
    target.write_text("hello", encoding="utf-8")
    tracker = FileStateTracker()
    tracker.record(target)
    tool = WriteTool(Sandbox(workspace), tracker, plan_root=plan_root)

    # If record_stat does not exist yet, this is the RED: new API missing.
    assert hasattr(tracker, "record_stat"), "FileStateTracker.record_stat missing"
    assert hasattr(tracker, "check_stat"), "FileStateTracker.check_stat missing"
    calls: dict[str, int] = {"record": 0}
    orig_record = tracker.record

    def counting_record(path):
        calls["record"] += 1
        return orig_record(path)

    monkeypatch.setattr(tracker, "record", counting_record)
    result = await tool.execute(
        {"path": ".ai-coding/plan/note.md", "content": "hello!"},
        ToolContext(workspace=str(workspace), mode="plan"),
    )
    assert result.success is True
    assert calls["record"] == 0, "plan write must not re-stat the path"


@pytest.mark.asyncio
async def test_plan_edit_uses_one_rdwr_nofollow_open_and_keeps_fd_stat(
    tmp_path, monkeypatch
):
    """Edit must read-modify-write on one O_RDWR|O_NOFOLLOW fd and keep its fstat."""
    import os as _os

    workspace = tmp_path
    plan_root = workspace / ".ai-coding" / "plan"
    plan_root.mkdir(parents=True)
    target = plan_root / "note.md"
    target.write_text("hello world", encoding="utf-8")
    tracker = FileStateTracker()
    tracker.record(target)
    tool = EditTool(Sandbox(workspace), tracker, plan_root=plan_root)

    opened_flags: list[int] = []
    real_open = _os.open

    def spying_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and path == "note.md":
            opened_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(_os, "open", spying_open)
    # os.open is monkeypatched; keep the platform gate (supports_dir_fd) green.
    try:
        _supports = set(_os.supports_dir_fd)
        _supports.add(spying_open)  # type: ignore[arg-type]
        monkeypatch.setattr(_os, "supports_dir_fd", _supports)
    except Exception:
        pass
    assert hasattr(tracker, "record_stat"), "FileStateTracker.record_stat missing"
    result = await tool.execute(
        {
            "path": ".ai-coding/plan/note.md",
            "old_string": "world",
            "new_string": "there",
        },
        ToolContext(workspace=str(workspace), mode="plan"),
    )

    assert result.success is True
    rdwr_opens = [
        f
        for f in opened_flags
        if (f & _os.O_RDWR) and (f & getattr(_os, "O_NOFOLLOW", 0))
    ]
    assert len(rdwr_opens) == 1, f"expected one O_RDWR|O_NOFOLLOW open, got {opened_flags}"
    assert target.read_text(encoding="utf-8") == "hello there"


def test_builder_injects_the_plan_root(tmp_path):
    """Write/Edit built by the composer must carry workspace/.ai-coding/plan."""
    from ai_coding.config.models import AppConfig
    from ai_coding.domain.agents import PLAN_DIR
    from ai_coding.tools.builder import _assemble_registry

    cfg = AppConfig(models={}, default_model="")
    _, gate = _assemble_registry(cfg, str(tmp_path), None, True, None, "", None, None)
    # Rebuild the registry to inspect tools (builder owns plan_root wiring).
    from ai_coding.tools.builder import build_tool_registry

    registry = build_tool_registry(cfg, str(tmp_path), None, include_subagent=False)
    write = registry.get("write")
    edit = registry.get("edit")
    expected = (tmp_path / PLAN_DIR).resolve()
    assert write._plan_root is not None and write._plan_root.resolve() == expected
    assert edit._plan_root is not None and edit._plan_root.resolve() == expected
