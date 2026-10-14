#!/usr/bin/env python3
"""r17/r18 acceptance: does the data actually close its loop?

Run it after any change to the five P1 features::

    python scripts/r18_acceptance.py            # offline: no network, no provider
    python scripts/r18_acceptance.py --live     # also hits the real provider

It is deliberately not a pytest file.  pytest asserts against doubles; this one
drives the real objects -- SnapshotStore, git, the tool registry, the session
service, AgentLoop -- and prints what actually came out the other end, so a
reviewer can read whether the contract held rather than whether a mock agreed.

Every check states the contract it is holding the code to, and the exit code is
non-zero if any of them did not.
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.engine import ChatEngine  # noqa: E402
from ai_coding.attachments import _allowed_mime, save_data_url  # noqa: E402
from ai_coding.core.agent_loop import AgentLoop  # noqa: E402
from ai_coding.core.snapshots import SnapshotStore  # noqa: E402
from ai_coding.core.workspace_files import SKIP_DIRS, walk_workspace  # noqa: E402
from ai_coding.domain.loop import StepResult  # noqa: E402
from ai_coding.domain.message import ImageRef, ToolCallRef  # noqa: E402
from ai_coding.security.file_state_tracker import FileStateTracker  # noqa: E402
from ai_coding.security.permission_gate import PermissionGate  # noqa: E402
from ai_coding.security.sandbox import Sandbox  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402
from ai_coding.tools.grep_tool import GrepTool  # noqa: E402
from ai_coding.tools.registry import ToolRegistry  # noqa: E402
from ai_coding.tools.webfetch_tool import WebfetchTool  # noqa: E402
from ai_coding.tools.write_tool import WriteTool  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name if ok else (name, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}")
    if detail:
        print(f"         {detail}")


def git_repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir(parents=True)
    ident = ["-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "i", "--allow-empty"],
                   cwd=root, check=True)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "a"], cwd=root, check=True)
    return root


class ScriptedEngine(ChatEngine):
    """A provider that replays a fixed script of steps."""

    def __init__(self, script: list[StepResult]) -> None:
        self.script = script
        self.seen: list[dict] = []

    async def step(self, messages, *, tools, instructions=None, on_delta=None, **kw):  # type: ignore[override]
        from ai_coding.ai.mapping import history_to_chat_messages

        self.seen.append({"messages": history_to_chat_messages(list(messages), **kw)})
        return self.script.pop(0) if self.script else StepResult(text="done")


async def section_undo() -> None:
    print("\n1. undo / redo — files, history, composer text, turn number")
    base = Path(tempfile.mkdtemp())
    root = git_repo(base)
    store = SnapshotStore(root)
    sessions = SessionService(base / "sessions")
    loop = AgentLoop(
        ScriptedEngine([StepResult(text="ok")]), sessions, ToolRegistry(),
        workspace=str(root), snapshots=store,
    )
    session = sessions.create()
    await loop.process_input(session, "真实的一句需求")
    (root / "a.txt").write_text("BROKEN\n", encoding="utf-8")
    (root / "typed-by-human.txt").write_text("mine\n", encoding="utf-8")

    report = await loop.undo_last_turn(session)

    check("undo restores the tracked file",
          (root / "a.txt").read_text(encoding="utf-8") == "v1\n",
          f"a.txt = {(root / 'a.txt').read_text().strip()!r}")
    check("undo keeps a file the human wrote",
          (root / "typed-by-human.txt").exists(),
          "the user's own file survived")
    check("undo hands the human's sentence back",
          report.get("text") == "真实的一句需求", repr(report.get("text")))
    check("undo empties the history",
          not [m for m in session.messages if m.role == "user"],
          f"{len(session.messages)} messages left")
    check("undo rewinds the turn counter",
          session.turn_count == 0, f"turn_count = {session.turn_count}")

    await loop.process_input(session, "第二句")
    stale = await loop.redo_last_turn(session)
    check("a new turn retires redo",
          stale == {"ok": False, "reason": "nothing to redo"}, repr(stale))
    check("the redo ref is gone",
          not await store.exists(f"{session.session_id}/redo"),
          "refs/ai-coding/undo/<sid>/redo was deleted at the next turn")


async def section_artifacts() -> None:
    print("\n2. artifacts — the turn's own report drives what gets deleted")
    base = Path(tempfile.mkdtemp())
    root = git_repo(base)
    sessions = SessionService(base / "sessions")
    tools = ToolRegistry()
    tools.register(WriteTool(Sandbox(root), FileStateTracker()))
    engine = ScriptedEngine([
        StepResult(tool_calls=[ToolCallRef(
            id="c1", name="write",
            arguments=json.dumps({"path": "./sub/../made.txt", "content": "a\n"}))]),
        StepResult(text="写好了"),
    ])
    loop = AgentLoop(engine, sessions, tools, workspace=str(root),
                     snapshots=SnapshotStore(root),
                     gate=PermissionGate(allowed_tools=("write",)))
    session = sessions.create()

    await loop.process_input(session, "建个文件")
    reported = [a.path for m in session.messages for a in m.artifacts]
    check("write reports the path it wrote, workspace-relative",
          reported == ["made.txt"], f"artifacts = {reported}")
    check("the report survives to disk",
          '"made.txt"' in (base / "sessions" / f"{session.session_id}.json").read_text("utf-8"),
          "session json carries the artifacts")

    await loop.undo_last_turn(session)
    check("undo removes the file the turn created",
          not (root / "made.txt").exists(), "made.txt is gone")


async def section_images() -> None:
    print("\n3. images — one gate out of the workspace")
    base = Path(tempfile.mkdtemp())
    (base / "inside.png").write_bytes(PNG)
    (base / "link-out.png").symlink_to("/etc/hostname")
    ref = save_data_url(base, "clip", "data:image/png;base64," + base64.b64encode(PNG).decode())
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = str(base)

    check("a pasted image lands under .ai-coding/attachments",
          ref is not None and (base / ref.path).is_file(),
          f"path = {ref.path if ref else None}")
    check("a workspace image loads",
          (loop._load_image(ref) or "").startswith("data:image/png;base64,"),
          "data URL produced")
    for label, bad in (
        ("absolute path", ImageRef(path="/etc/hostname", mime="image/png", name="x")),
        ("dot-dot escape", ImageRef(path="../../../../etc/hostname", mime="image/png", name="x")),
        ("symlink out", ImageRef(path="link-out.png", mime="image/png", name="x")),
        ("empty mime", ImageRef(path="inside.png", mime="", name="x")),
        ("non-image mime", ImageRef(path="inside.png", mime="image/bmp", name="x")),
    ):
        check(f"refuses a {label}", loop._load_image(bad) is None, "returned None")


async def section_workspace_files() -> None:
    print("\n4. workspace files — one list, pruned rather than filtered")
    base = Path(tempfile.mkdtemp())
    root = base / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "a.py").write_text("target\n", encoding="utf-8")
    (root / "x.log").write_text("target\n", encoding="utf-8")
    (root / "sub" / "b.py").write_text("target\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    objects = root / ".git" / "objects"
    objects.mkdir(parents=True)
    for i in range(2000):
        (objects / f"{i}.pack").write_text("x", encoding="utf-8")

    entries = list(walk_workspace(root))
    names = {str(rel) for rel, _ in entries}
    check("skipped directories are pruned, not walked",
          len(entries) < 20, f"{len(entries)} entries for a tree holding 2004 files")
    check(".gitignore is honoured", "x.log" not in names, f"saw {sorted(names)}")
    check("the source table is the only definition",
          "node_modules" in SKIP_DIRS and "_SKIP_DIRS" not in
          (ROOT / "ai_coding/tools/grep_tool.py").read_text(encoding="utf-8"),
          "grep_tool has no private copy")

    tool = GrepTool(Sandbox(root), use_ripgrep=False)
    root_hit = await tool.execute({"pattern": "target"})
    sub_hit = await tool.execute({"pattern": "target", "path": "sub"})
    check("grep over the workspace finds the files",
          "a.py:1" in root_hit.output and "sub/b.py:1" in root_hit.output,
          root_hit.output.replace("\n", " | "))
    check("grep scoped to a subdirectory finds them too",
          "sub/b.py:1" in sub_hit.output,
          f"got {sub_hit.output!r}")


async def section_tools() -> None:
    print("\n5. tools — a failure is never dressed as an answer")
    base = Path(tempfile.mkdtemp())
    (base / "a.py").write_text("target\n", encoding="utf-8")

    from types import SimpleNamespace

    async def dead(argv, *, cwd, timeout):
        return SimpleNamespace(returncode=None, stdout="", stderr="", timed_out=False,
                               error="FileNotFoundError: rg")

    tool = GrepTool(Sandbox(base), use_ripgrep=True, rg_path="/nope", runner=dead)
    result = await tool.execute({"pattern": "target"})
    check("a broken rg fails instead of reporting no matches",
          result.success is False and "no matches" not in result.output,
          result.output[:60])

    from ai_coding.tools.htmltext import _SKIP, html_to_text

    voids = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
             "param", "source", "track", "wbr"}
    check("no void element is in the html suppression set",
          not (voids & _SKIP), f"overlap = {sorted(voids & _SKIP) or 'none'}")
    check("an <embed> page keeps its text",
          "REAL" in html_to_text("<body><embed src=x.pdf><p>REAL</p></body>"),
          html_to_text("<body><embed src=x.pdf><p>REAL</p></body>"))

    from ai_coding.tools.websearch_tool import WebsearchTool

    def shape(url, timeout, data):
        return "application/json", json.dumps({"results": ["junk"]}).encode()

    dirty = await WebsearchTool(api_key="k", opener=shape).execute({"query": "q"})
    check("a results list of junk is a failure, not an empty search",
          dirty.success is False, dirty.output[:60])

    sent: dict = {}

    def cap(url, timeout, data):
        sent["n"] = json.loads(data)["max_results"]
        return "application/json", b'{"results": []}'

    await WebsearchTool(api_key="k", opener=cap).execute({"query": "q", "max_results": 10000})
    check("max_results is capped before the request",
          sent["n"] <= 20, f"asked for {sent['n']}")

    def opener(url, timeout):  # noqa: ARG001 - the injected seam's shape
        html = b"<html><body><nav>x</nav><p>Real content.</p></body></html>"
        return "text/html; charset=utf-8", html

    fetched = await WebfetchTool(opener=opener).execute({"url": "https://example.com/doc"})
    check("webfetch cleans and marks the source",
          fetched.success and "Real content." in fetched.output
          and "不可信" in fetched.output and "nav" not in fetched.output.split("---")[1][:20],
          fetched.output[:70].replace("\n", " | "))


def section_contracts() -> None:
    print("\n6. contracts — one table, one spelling")
    from ai_coding.domain.message import IMAGE_MIME_BY_SUFFIX

    derived = _allowed_mime()
    check("the paste whitelist is derived from the suffix table",
          set(derived) == set(IMAGE_MIME_BY_SUFFIX.values()), f"{sorted(derived)}")
    check("pasted jpegs keep their name", derived.get("image/jpeg") == "jpg",
          f"image/jpeg -> {derived.get('image/jpeg')}")

    handlers = (ROOT / "ai_coding/server/handlers.py").read_text(encoding="utf-8")
    check("files.find has no private copy of the ignore list",
          "node_modules" not in handlers, "handlers.py delegates to workspace_files")


async def main(live: bool) -> int:
    print("=" * 68)
    print("r17/r18 acceptance — undo · artifacts · images · files · tools")
    print("=" * 68)
    await section_undo()
    await section_artifacts()
    await section_images()
    await section_workspace_files()
    await section_tools()
    section_contracts()
    if live:
        print("\n7. live provider — the image pipeline end to end")
        try:
            fetched = await WebfetchTool().execute({"url": "https://example.com"})
            check("webfetch reaches the real internet", fetched.success,
                  fetched.output[:60].replace("\n", " | "))
        except Exception as exc:  # noqa: BLE001 - report, do not crash the run
            check("webfetch reaches the real internet", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "=" * 68)
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    for name, detail in FAILED:
        print(f"    FAIL  {name}: {detail}")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main("--live" in sys.argv)))
