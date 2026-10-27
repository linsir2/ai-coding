"""Integration acceptance (r17 P1): do the five features close their data loop?

The unit tests of r17 drive collaborators directly -- a hand-built message list
handed to ``undo_last_turn``, an injected fake runner handed to grep, an
injected opener handed to webfetch.  That proves each part but not the *wiring*:
the question an acceptance test must answer is whether the data a user types,
pastes or points at actually arrives where the contract says, survives the round
trip to disk, and comes back out unchanged.

So every test here drives the real composition root:

    process_input -> _run_turn (capture snapshot, extract @images, begin turn)
                  -> RequestAssembler (volatile mention blocks)
                  -> mapping.to_chat_message (content parts)
                  -> engine.step  (what the provider would literally receive)
                  -> ToolRegistry / dispatch (what the tool literally returns)
                  -> SessionService.save -> load (what survives a restart)

and then asserts the contract on the far side.  The fake is the *provider*, never
the plumbing.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from ai_coding.ai.engine import ChatEngine
from ai_coding.ai.mapping import history_to_chat_messages
from ai_coding.attachments import save_data_url
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.core.snapshots import SnapshotStore
from ai_coding.domain.loop import StepResult
from ai_coding.domain.message import ChatMessage, ImageRef, ToolCallRef
from ai_coding.domain.session import _message_from_dict, _message_to_dict
from ai_coding.security.permission_gate import PermissionGate
from ai_coding.security.sandbox import Sandbox
from ai_coding.service.session_service import SessionService
from ai_coding.tools.grep_tool import GrepTool
from ai_coding.tools.registry import ToolRegistry

# M-G3: the integration layer.  These files already lived in tests/integration/;
# the marker is what lets CI run them as their own job.
pytestmark = pytest.mark.integration

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


class RecordingEngine(ChatEngine):
    """The provider boundary: records every request, answers with text.

    This is the only fake in these tests.  Everything between the user and this
    object -- assembly, mapping, tool dispatch, persistence -- is the real code
    the CLI builds.
    """

    def __init__(
        self,
        *,
        tool_reply: str | None = None,
        call: tuple[str, dict] | None = None,
    ) -> None:
        self.requests: list[dict] = []
        self.tool_reply = tool_reply
        #: ``(tool_name, params)`` for the first step only -- one call, then the
        #: answer, exactly the shape a provider produces.
        self.call = call
        self._served = False

    async def step(  # type: ignore[override]
        self, messages, *, tools, instructions=None, on_delta=None, **kwargs
    ):
        self.requests.append(
            {
                "messages": history_to_chat_messages(
                    list(messages), image_loader=kwargs.get("image_loader")
                ),
                "tools": tools,
                "extra": kwargs,
            }
        )
        if self.call is not None and not self._served:
            self._served = True
            name, params = self.call
            return StepResult(
                tool_calls=[ToolCallRef(id="c1", name=name, arguments=json.dumps(params))]
            )
        return StepResult(text=self.tool_reply or "ok")

    def last_request(self) -> dict:
        assert self.requests, "the provider was never called -- the loop never ran"
        return self.requests[-1]

    @staticmethod
    def text_parts(request: dict) -> list[str]:
        """Every *text* fragment in the request, with image payloads excluded.

        A content-parts message must be read part by part: stringifying the whole
        list would count an image's base64 as "text injected", which is the
        opposite of what the contract forbids.
        """
        out: list[str] = []
        for message in request["messages"]:
            content = message.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(
                    str(part.get("text", ""))
                    for part in content
                    if part.get("type") == "text"
                )
        return out

    @staticmethod
    def user_message_with_parts(request: dict) -> dict:
        for message in reversed(request["messages"]):
            if message.get("role") == "user" and isinstance(message.get("content"), list):
                return message
        raise AssertionError("no user message with content parts reached the provider")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one committed file: undo needs a truth source."""
    root = tmp_path / "repo"
    root.mkdir()
    ident = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", *ident, "commit", "-qm", "init", "--allow-empty"], cwd=root, check=True
    )
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "add"], cwd=root, check=True)
    return root


def build_loop(
    tmp_path: Path,
    workspace: Path,
    *,
    engine: ChatEngine | None = None,
    tools: ToolRegistry | None = None,
    gate: PermissionGate | None = None,
) -> tuple[AgentLoop, SessionService]:
    """The composition root ``cli._build_loop`` builds, minus the optional parts.

    Mirrors ``tests/test_undo_command.py``'s shape; the difference is that these
    tests go through ``process_input`` instead of poking the loop directly.  The
    loop builds its own ``RequestAssembler`` from ``workspace``
    (``agent_loop.py:319``), so the mention blocks under test are the real ones.
    """
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        engine or RecordingEngine(),
        sessions,
        tools if tools is not None else ToolRegistry(),
        gate=gate,
        workspace=str(workspace),
        snapshots=SnapshotStore(workspace),
    )
    return loop, sessions


def _grep_registry(root: Path) -> tuple[ToolRegistry, PermissionGate]:
    tools = ToolRegistry()
    tools.register(GrepTool(Sandbox(root)))
    return tools, PermissionGate(allowed_tools=("grep",))


# ---------------------------------------------------------------- T1: undo/redo


@pytest.mark.asyncio
async def test_a_real_turn_is_undoable_all_the_way_down_to_the_file(tmp_path, repo):
    """Contract: /undo = the user turn goes, the files it changed come back, and
    the message text returns to the composer.  Driven through ``process_input`` --
    the snapshot must be taken by the loop itself."""
    loop, sessions = build_loop(tmp_path, repo)
    session = sessions.create()

    await loop.process_input(session, "改一下 a.txt")
    (repo / "a.txt").write_text("broken\n", encoding="utf-8")
    # r18/F4: a file nothing reported writing is left alone -- it could be the
    # human's own.  The turn's own files are covered in test_r17_defects.py,
    # where a real write tool creates them.
    (repo / "created.txt").write_text("new\n", encoding="utf-8")

    report = await loop.undo_last_turn(session)

    assert report["ok"] is True
    assert report["text"] == "改一下 a.txt", "the composer gets the user's own words back"
    assert (repo / "a.txt").read_text(encoding="utf-8") == "v1\n", "the file came back"
    assert (repo / "created.txt").exists(), "restore deleted a file nobody reported"
    assert report["files"] == ["a.txt"]
    assert [m.role for m in session.messages] == [], "history unrolled to the start"


@pytest.mark.asyncio
async def test_undo_then_redo_is_a_closed_round_trip(tmp_path, repo):
    """Contract: /redo is the inverse of the most recent /undo."""
    loop, sessions = build_loop(tmp_path, repo)
    session = sessions.create()

    await loop.process_input(session, "turn one")
    (repo / "a.txt").write_text("after one\n", encoding="utf-8")
    await loop.undo_last_turn(session)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "v1\n"

    await loop.redo_last_turn(session)

    assert (repo / "a.txt").read_text(encoding="utf-8") == "after one\n"
    assert [m.content for m in session.messages if m.role == "user"] == ["turn one"]


@pytest.mark.asyncio
async def test_the_undo_report_and_the_history_both_survive_a_restart(tmp_path, repo):
    """The truncated history is what a restart replays; it must agree with what
    the report promised the user."""
    loop, sessions = build_loop(tmp_path, repo)
    session = sessions.create()

    await loop.process_input(session, "turn one")
    await loop.undo_last_turn(session)

    path = tmp_path / "sessions" / f"{session.session_id}.json"
    on_disk = json.loads(path.read_text("utf-8"))
    assert on_disk["messages"] == [], "a restart replays an already-undone history"
    assert sessions.load(session.session_id).messages == session.messages


# --------------------------------------------------------------------- T2: grep


@pytest.mark.asyncio
async def test_grep_results_reach_the_provider_as_tool_output(tmp_path, repo):
    """The answer must travel the real tool path: registry -> dispatch ->
    ToolResult -> tool message -> the next step's request."""
    engine = RecordingEngine(call=("grep", {"pattern": "hello"}))
    tools, gate = _grep_registry(repo)
    loop, sessions = build_loop(tmp_path, repo, engine=engine, tools=tools, gate=gate)
    session = sessions.create()
    (repo / "notes.txt").write_text("hello from notes\n", encoding="utf-8")

    await loop.process_input(session, "grep hello")

    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert tool_msgs, "grep was never dispatched"
    assert "notes.txt:1" in (tool_msgs[0].content or "")
    assert "hello from notes" in (tool_msgs[0].content or "")


# ---------------------------------------------------------------- T3: web tools


@pytest.mark.asyncio
async def test_webfetch_output_carries_its_source_and_the_untrusted_banner():
    """Contract: cleaned content + source header + an explicit untrusted line.
    No network: the opener is injected, everything else is the real tool."""
    from ai_coding.tools.webfetch_tool import WebfetchTool

    body = (
        b"<html><body><nav>menu</nav><script>alert(1)</script>"
        b"<p>Real content.</p></body></html>"
    )

    def opener(url, timeout):  # noqa: ARG001 - the real opener's shape
        return "text/html; charset=utf-8", body

    result = await WebfetchTool(opener=opener).execute({"url": "https://example.com/doc"})

    assert result.success is True
    assert result.output.startswith("Fetched https://example.com/doc")
    assert "Real content." in result.output
    assert "alert(1)" not in result.output and "menu" not in result.output
    assert "不可信" in result.output


@pytest.mark.asyncio
async def test_websearch_without_a_key_answers_how_to_configure():
    from ai_coding.tools.websearch_tool import WebsearchTool

    result = await WebsearchTool(api_key="").execute({"query": "q"})

    assert result.success is False
    assert "websearch_api_key" in result.output


# ------------------------------------------------------------- T4: file mentions


@pytest.mark.asyncio
async def test_a_typed_mention_reaches_the_provider_without_a_tool_call(tmp_path, repo):
    """Contract: ``@file`` is content in the request tail, so the answer needs no
    tool call.  What matters is that the provider request carries the file."""
    (repo / "notes.txt").write_text("the secret answer is 42\n", encoding="utf-8")
    engine = RecordingEngine()
    loop, sessions = build_loop(tmp_path, repo, engine=engine)
    session = sessions.create()

    await loop.process_input(session, "@notes.txt 里面写了什么")

    texts = RecordingEngine.text_parts(engine.last_request())
    assert any("the secret answer is 42" in t for t in texts), (
        "the referenced file never made it into the request"
    )


@pytest.mark.asyncio
async def test_the_mention_block_is_volatile_and_dies_with_the_next_message(tmp_path, repo):
    """Contract: the block exists on the turn it was typed, gone on the next."""
    (repo / "notes.txt").write_text("unique-file-payload-42\n", encoding="utf-8")
    engine = RecordingEngine()
    loop, sessions = build_loop(tmp_path, repo, engine=engine)
    session = sessions.create()

    await loop.process_input(session, "@notes.txt")
    await loop.process_input(session, "换个问题")

    texts = RecordingEngine.text_parts(engine.last_request())
    assert not any("unique-file-payload-42" in t for t in texts), (
        "the referenced file survived into the next turn -- volatile is broken"
    )


# --------------------------------------------------------------- T5: image input


@pytest.mark.asyncio
async def test_a_pasted_image_reaches_the_provider_as_an_image_part(tmp_path, repo):
    """The full clipboard path: data URL -> disk -> session JSON -> ImageRef ->
    content parts -> the request the provider receives."""
    engine = RecordingEngine()
    loop, sessions = build_loop(tmp_path, repo, engine=engine)
    session = sessions.create()

    saved = save_data_url(
        repo, "shot", "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    )
    assert saved.ref is not None and (repo / saved.ref.path).is_file()

    await loop.process_input(session, "看这个图", images=(saved.ref,))

    message = RecordingEngine.user_message_with_parts(engine.last_request())
    kinds = [part.get("type") for part in message["content"]]
    assert "image_url" in kinds, f"no image part reached the provider: {message['content']}"
    url = next(
        p["image_url"]["url"] for p in message["content"] if p.get("type") == "image_url"
    )
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_1PX, "wrong bytes arrived"


@pytest.mark.asyncio
async def test_an_at_mentioned_image_becomes_a_part_not_injected_text(tmp_path, repo):
    """Contract: ``@shot.png`` rides as a message part, and the same file must not
    also be injected as text by the mention block."""
    (repo / "shot.png").write_bytes(PNG_1PX)
    (repo / "guide.md").write_text("some text\n", encoding="utf-8")
    engine = RecordingEngine()
    loop, sessions = build_loop(tmp_path, repo, engine=engine)
    session = sessions.create()

    await loop.process_input(session, "@shot.png 是什么颜色")

    user_msgs = [m for m in session.messages if m.role == "user"]
    assert user_msgs and user_msgs[-1].images, "the @png never became a message image"
    assert user_msgs[-1].images[0].name == "shot.png"

    texts = RecordingEngine.text_parts(engine.last_request())
    assert not any("iVBORw0KGgo" in t for t in texts), (
        "the image bytes were injected as text -- the two paths do not exclude each other"
    )


@pytest.mark.asyncio
async def test_an_image_message_survives_a_restart(tmp_path, repo):
    """The session file stores a path, not base64.  After a reload the message
    must still be able to produce its bytes."""
    (repo / "shot.png").write_bytes(PNG_1PX)
    loop, sessions = build_loop(tmp_path, repo)
    session = sessions.create()

    await loop.process_input(session, "@shot.png")

    path = tmp_path / "sessions" / f"{session.session_id}.json"
    raw = path.read_text("utf-8")
    assert "iVBORw0KGgo" not in raw, "base64 leaked into the session file"
    stored = [m for m in json.loads(raw)["messages"] if m.get("role") == "user"]
    assert stored[-1]["images"][0]["path"] == "shot.png"

    reloaded = sessions.load(session.session_id)
    ref = [m for m in reloaded.messages if m.role == "user"][-1].images[0]
    assert ref.mime == "image/png"
    data_url = loop._load_image(ref)
    assert data_url is not None and data_url.startswith("data:image/png;base64,")


def test_a_message_with_images_round_trips_through_the_session_dict():
    """Unit-level, kept honest here because the integration path depends on it."""
    msg = ChatMessage(
        role="user",
        content="看这个",
        timestamp="t",
        images=(ImageRef(path="a.png", mime="image/png", name="a"),),
    )
    assert _message_from_dict(_message_to_dict(msg)).images == msg.images


# ------------------------------------------------------- cross-feature contract


@pytest.mark.asyncio
async def test_one_turn_can_carry_every_r17_feature_at_once(tmp_path, repo):
    """The acceptance question: do the five features interfere with each other?

    A turn that mentions a file, an image and greps, and is later undone.
    Nothing may be lost on the way through assembly, dispatch, persistence or
    rollback.
    """
    engine = RecordingEngine(call=("grep", {"pattern": "alpha"}))
    tools, gate = _grep_registry(repo)
    loop, sessions = build_loop(
        tmp_path, repo, engine=engine, tools=tools, gate=gate
    )
    session = sessions.create()
    (repo / "notes.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "shot.png").write_bytes(PNG_1PX)

    await loop.process_input(session, "@notes.txt @shot.png grep alpha")
    (repo / "a.txt").write_text("broken\n", encoding="utf-8")

    user_msg = [m for m in session.messages if m.role == "user"][-1]
    assert user_msg.content == "@notes.txt @shot.png grep alpha"
    assert user_msg.images and user_msg.images[0].name == "shot.png"
    assert any(m.role == "tool" for m in session.messages), "grep did not run"

    report = await loop.undo_last_turn(session)
    assert report["ok"] is True
    assert (repo / "a.txt").read_text(encoding="utf-8") == "v1\n"
    assert not [m for m in session.messages if m.role == "user"], "the turn stayed"
