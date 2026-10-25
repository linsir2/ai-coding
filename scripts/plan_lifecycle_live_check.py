#!/usr/bin/env python3
"""批次 4a live check — the plan's lifecycle: giving one up, and being heard.

Three scenarios, behind ``scripts/recording_proxy.py`` with a fresh temp
workspace, so every byte of the traffic is on disk before it is asserted:

  swap  — the dead end, closed.  ``/plan <topic>`` plans and the review
          approves it (the task is armed with the plan profile), ``/plan
          off`` gives it up: the terminal says so, the flag *and* the
          profile go, the boot resume has nothing to pick up, and a second
          ``/plan <topic>`` is not refused any more.  Before this batch the
          command only flipped the posture, so the two refusals ("先执行它
          （/plan run），或用 /plan off 放弃后再重新规划") pointed at a door
          that was not there.

  note  — the guard's terminal line, in the terminal.  A planning round
          that ends without a submission leaves a note on the outcome
          ("本回合未提交计划…"), and until this batch nothing on the
          command side read it: the note rode the turn.finished event, and
          the front end was the only reader.

          The submission tool is deliberately absent from this scenario's
          tool table (the registry registers ``present_plan`` only with a
          human channel, and this run has none).  That is the deterministic
          way to reach the path the guard exists for: the model cannot
          submit, so the turn ends unsubmitted, and the note has to be
          where the operator can read it.

  budget — the exit is asked for *before* the budget is gone.  The guard's
          remedy used to arrive only after the model had stopped and
          written its answer in prose; the batch's early ask puts it in
          front of the model while it is still exploring.  The exploration
          budget is shrunk for this run (the patch lives in this script and
          is undone on the way out) so the ask lands in the first rounds
          whatever the model does -- with the real budget a good model
          submits in four rounds and the ask never arrives.

Usage:

    python scripts/plan_lifecycle_live_check.py [--config config.local.yaml]
        [--out-dir /tmp/opencode/plan-life] [--only swap|note|budget]
        [--topic "..."] [--note-topic "..."] [--model NAME]

Artifacts (temp config, JSONL wire recording, session copies) stay under
the out dir; nothing is deleted.  Exit code 0 = the scenarios passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from r19_acceptance import (  # noqa: E402
    _AutoReview,
    _git_workspace,
    _RecordingConsole,
    _redact_api_key,
    _temp_config,
)
from recording_proxy import serve, start  # noqa: E402

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.app.commands import handle_input  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.core import plan_runner  # noqa: E402
from ai_coding.core.events import TaskUpdated  # noqa: E402
from ai_coding.core.turn_policy import (  # noqa: E402
    PLAN_NUDGE,
    PLAN_UNSUBMITTED_NOTE,
)
from ai_coding.server.bridge import to_wire  # noqa: E402
from ai_coding.server.world import find_armed_session  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

SEP = "=" * 68

#: The exploration budget of the ``budget`` scenario, in rounds.  The real
#: one is 16 and the ask lands on round 13 -- but a good model submits a
#: plan in three or four rounds (measured on this machine), so with the real
#: budget the ask never arrives and the scenario would prove nothing.
#:
#: Five rounds put the ask on the model's second call -- after one round of
#: exploration, which is the shape the mechanism exists for -- and leave it
#: four rounds to hand something over.  Four rounds would do the same but
#: with no margin: the model needed three of them.
#:
#: The number is patched into the module for the length of the scenario and
#: put back afterwards; nothing in the product knows about it.
BUDGET_MAX_ITERATIONS = 5

#: An implementation request, so the model hands over a plan (the research
#: exit refuses an implementation request -- N1's anti-abuse guard).
DEFAULT_TOPIC = (
    "新增 PLAN_DEMO.md：写明本仓库的一句话用途；"
    "验收方式为文件存在（verify 用 test -f PLAN_DEMO.md）"
)

#: The second round, after the first plan was given up: it must not be
#: refused with "已有一份批准的计划尚未执行".
DEFAULT_SECOND_TOPIC = (
    "新增 PLAN_DEMO2.md：写明本仓库的一句话用途；"
    "验收方式为文件存在（verify 用 test -f PLAN_DEMO2.md）"
)

#: The scenario that must NOT submit a plan.  An implementation request
#: ("新增" is one of the verbs the research exit's anti-abuse guard
#: recognises), so the research exit refuses it and the model has nowhere to
#: go: it cannot submit a plan (no present_plan in this scenario's tool
#: table) and it cannot conclude research.
DEFAULT_NOTE_TOPIC = (
    "新增 PLAN_DEMO.md：写明本仓库的一句话用途；"
    "验收方式为文件存在（verify 用 test -f PLAN_DEMO.md）"
)

#: The scenario that proves the early ask.  An implementation request, so
#: the model has a plan to hand over -- the research exit refuses it (N1's
#: anti-abuse guard).
DEFAULT_BUDGET_TOPIC = DEFAULT_TOPIC


class _RecordingObserver:
    """Collects loop events so a scenario can assert on the wire shape.

    The live card reads ``plan.updated``; the script replays the same mapping
    the frontend consumes (``bridge.to_wire``), so the assertion proves what
    the card will see -- no frontend needed for this half.
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_event(self, event: Any) -> None:
        self.events.append(event)


async def _run_swap(
    *, topic: str, second_topic: str, model: dict[str, Any], out_root: Path
) -> dict[str, Any]:
    """Plan, approve, give it up, and plan again."""
    run_dir = out_root / "swap"
    run_dir.mkdir(parents=True, exist_ok=True)
    ws = run_dir / "ws"
    ws.mkdir(exist_ok=True)
    _git_workspace(ws)

    wire = run_dir / "wire.jsonl"
    server = serve(upstream=model["base_url"], port=0, out_path=wire, cut_first=0)
    start(server)
    port = server.server_address[1]

    config_path = run_dir / "config.yaml"
    _temp_config(config_path, model, port, model["base_url"], 12)
    console = _RecordingConsole()
    review = _AutoReview()
    try:
        app_cfg = ConfigLoader.load_app(str(config_path))
        _redact_api_key(config_path, model["api_key"])
        # The real registry with a scripted human: present_plan registers only
        # with a human channel, and the review rides that channel.
        registry = _build_tool_registry(app_cfg, str(ws), None, user_prompt=review)
        engine = build_engine_from_config(app_cfg)
        sessions = SessionService(ws / "sessions")
        rec = _RecordingObserver()
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None, observer=rec)
        session = sessions.create()
        started = time.monotonic()

        # 1. plan + approve
        await handle_input(loop, f"/plan {topic}", session, sessions, console)
        task = session.task
        # D5: what the live card will see -- the wire form of every
        # TaskUpdated, replayed through the same mapping the frontend
        # consumes.  The last non-empty one is the approved plan.
        plan_wires = [
            wire
            for e in rec.events
            if isinstance(e, TaskUpdated)
            for wire in [to_wire(e)]
            if wire is not None
            and wire.get("type") == "plan.updated"
            and wire.get("data", {}).get("items")
        ]
        after_plan = {
            "armed": bool(getattr(task, "armed", False)),
            "profile": str(getattr(task, "armed_profile", "") or ""),
            "items": len(task.items) if task else 0,
            "reviews": len([c for c in review.calls if c[3] == "plan_review"]),
            "wire_items": plan_wires[-1]["data"]["items"] if plan_wires else [],
            # D5: the domain shape the wire must equal -- the assertion is
            # structural, not content-based: whether the model submitted a
            # verify command varies run to run, but the live event must
            # always be the domain serialization (conditional emission
            # included), or the card cannot show what approval will run.
            "domain_items": [i.to_dict() for i in task.items] if task else [],
        }

        # 2. give it up
        await handle_input(loop, "/plan off", session, sessions, console)
        task = session.task
        after_off = {
            "armed": bool(getattr(task, "armed", False)),
            "profile": str(getattr(task, "armed_profile", "") or ""),
            "resume": find_armed_session(sessions) is not None,
        }

        # 3. plan again -- the dead end used to be here
        await handle_input(loop, f"/plan {second_topic}", session, sessions, console)
        after_replan = {
            "reviews": len([c for c in review.calls if c[3] == "plan_review"]),
            "items": len(session.task.items) if session.task else 0,
        }
        elapsed = round(time.monotonic() - started, 1)
        await loop.aclose()
    finally:
        server.shutdown()
        server.server_close()

    entries = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    shutil.copy(ws / "sessions" / f"{session.session_id}.json", run_dir / "session.json")
    return {
        "name": "swap",
        "run_dir": str(run_dir),
        "elapsed": elapsed,
        "entries": entries,
        "console": list(console.lines),
        "after_plan": after_plan,
        "after_off": after_off,
        "after_replan": after_replan,
    }


async def _run_note(
    *, topic: str, model: dict[str, Any], out_root: Path
) -> dict[str, Any]:
    """A planning round that ends without a submission: the note must show."""
    run_dir = out_root / "note"
    run_dir.mkdir(parents=True, exist_ok=True)
    ws = run_dir / "ws"
    ws.mkdir(exist_ok=True)
    _git_workspace(ws)

    wire = run_dir / "wire.jsonl"
    server = serve(upstream=model["base_url"], port=0, out_path=wire, cut_first=0)
    start(server)
    port = server.server_address[1]

    config_path = run_dir / "config.yaml"
    # A small iteration budget: the point of this scenario is the turn that
    # ends without a submission, not a long exploration.
    _temp_config(config_path, model, port, model["base_url"], 3)
    console = _RecordingConsole()
    try:
        app_cfg = ConfigLoader.load_app(str(config_path))
        _redact_api_key(config_path, model["api_key"])
        # No human channel: present_plan is not registered, so the model
        # cannot submit a plan (see the module docstring).
        registry = _build_tool_registry(app_cfg, str(ws), None)
        engine = build_engine_from_config(app_cfg)
        sessions = SessionService(ws / "sessions")
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)
        session = sessions.create()
        started = time.monotonic()

        await handle_input(loop, f"/plan {topic}", session, sessions, console)
        elapsed = round(time.monotonic() - started, 1)
        outcome = getattr(loop, "last_outcome", None)
        note = str(getattr(outcome, "note", "") or "")
        status = str(getattr(outcome, "status", "") or "")
        await loop.aclose()
    finally:
        server.shutdown()
        server.server_close()

    entries = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    shutil.copy(ws / "sessions" / f"{session.session_id}.json", run_dir / "session.json")
    return {
        "name": "note",
        "run_dir": str(run_dir),
        "elapsed": elapsed,
        "entries": entries,
        "console": list(console.lines),
        "note": note,
        "status": status,
    }


def _check_swap(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    lines = result["console"]
    after_plan = result["after_plan"]
    after_off = result["after_off"]
    after_replan = result["after_replan"]

    if not after_plan["armed"] or after_plan["profile"] != "plan":
        problems.append(
            "the first plan was never approved and armed: "
            f"{after_plan}"
        )
    if not after_plan["reviews"]:
        problems.append("the review channel was never asked")
    wire_items = after_plan.get("wire_items") or []
    domain_items = after_plan.get("domain_items") or []
    if not wire_items:
        problems.append("no plan.updated event reached the wire after approval")
    elif wire_items != domain_items:
        problems.append(
            "the live event is not the domain shape: "
            f"{wire_items!r} vs {domain_items!r}"
        )
    if after_off["armed"]:
        problems.append("the plan is still armed after /plan off")
    if after_off["profile"]:
        problems.append(
            f"the profile survived the withdrawal: {after_off['profile']!r} "
            "(a profile with nothing armed owns nothing)"
        )
    if after_off["resume"]:
        problems.append("the boot resume would still pick the given-up plan up")
    if not any("已放弃" in line for line in lines):
        problems.append(f"the terminal never said the plan was given up: {lines}")
    if any("未执行" in line and "已放弃" not in line for line in lines):
        problems.append(
            "the old hint ('还有 N 项未执行（重新进入 Plan 模式可以继续）') is "
            "still there next to the withdrawal -- it contradicts giving up"
        )
    if after_replan["reviews"] <= after_plan["reviews"]:
        problems.append(
            "the second planning round was refused: the review was never asked again"
        )
    if any("已有一份批准的计划尚未执行" in line for line in lines):
        problems.append("the dead end is still there: the second round was refused")
    return problems


def _check_note(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    lines = result["console"]
    note = result["note"]
    if PLAN_UNSUBMITTED_NOTE not in note:
        problems.append(
            f"the turn left no note about the missing plan: {note!r} "
            f"(wanted {PLAN_UNSUBMITTED_NOTE!r})"
        )
    if not any(PLAN_UNSUBMITTED_NOTE in line for line in lines):
        problems.append(f"the note never reached the terminal: {lines}")
    return problems


# ---- budget: the exit is asked for while the model is still exploring -------


def _asked_for_an_exit(entries: list[dict[str, Any]]) -> int:
    """The model call that first saw the ask (0 = never).

    Read off the wire: the ask is a user message in the request, so the
    first request that carries it is the call it reached.
    """
    for number, entry in enumerate(entries, start=1):
        request = entry.get("request") or {}
        messages = request.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            if str(message.get("content", "")) == PLAN_NUDGE:
                return number
    return 0


def _called(session: Any, name: str) -> bool:
    """Did the session's transcript record a successful call to this tool?"""
    for message in getattr(session, "messages", ()):
        if getattr(message, "role", "") != "tool":
            continue
        if getattr(message, "tool_name", "") != name:
            continue
        if 'ok="false"' not in (message.content or ""):
            return True
    return False


async def _run_budget(
    *, topic: str, model: dict[str, Any], out_root: Path
) -> dict[str, Any]:
    """A planning round where the exit is asked for before the budget is gone.

    The exploration budget is shrunk for the length of this run, so the ask
    lands in the first rounds whatever the model does.  The patch is put back
    afterwards, and it lives here in the script -- the product has no switch
    that exists only for a test.
    """
    run_dir = out_root / "budget"
    run_dir.mkdir(parents=True, exist_ok=True)
    ws = run_dir / "ws"
    ws.mkdir(exist_ok=True)
    _git_workspace(ws)

    wire = run_dir / "wire.jsonl"
    server = serve(upstream=model["base_url"], port=0, out_path=wire, cut_first=0)
    start(server)
    port = server.server_address[1]

    config_path = run_dir / "config.yaml"
    _temp_config(config_path, model, port, model["base_url"], 12)
    console = _RecordingConsole()
    review = _AutoReview()
    original_budget = plan_runner.PLANNING_MAX_ITERATIONS
    plan_runner.PLANNING_MAX_ITERATIONS = BUDGET_MAX_ITERATIONS
    try:
        app_cfg = ConfigLoader.load_app(str(config_path))
        _redact_api_key(config_path, model["api_key"])
        # The real registry with a scripted human: present_plan is registered
        # only with a human channel, and the review rides that channel.
        registry = _build_tool_registry(app_cfg, str(ws), None, user_prompt=review)
        engine = build_engine_from_config(app_cfg)
        sessions = SessionService(ws / "sessions")
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)
        session = sessions.create()
        started = time.monotonic()

        await handle_input(loop, f"/plan {topic}", session, sessions, console)
        elapsed = round(time.monotonic() - started, 1)
        outcome = getattr(loop, "last_outcome", None)
        status = str(getattr(outcome, "status", "") or "")
        note = str(getattr(outcome, "note", "") or "")
        asked = any(
            (message.content or "") == PLAN_NUDGE for message in session.messages
        )
        reviews = len([call for call in review.calls if call[3] == "plan_review"])
        exited = bool(reviews) or _called(session, "finish_research")
        await loop.aclose()
    finally:
        plan_runner.PLANNING_MAX_ITERATIONS = original_budget
        server.shutdown()
        server.server_close()

    entries = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    shutil.copy(ws / "sessions" / f"{session.session_id}.json", run_dir / "session.json")
    return {
        "name": "budget",
        "run_dir": str(run_dir),
        "elapsed": elapsed,
        "entries": entries,
        "console": list(console.lines),
        "asked": asked,
        "ask_at_call": _asked_for_an_exit(entries),
        "reviews": reviews,
        "exited": exited,
        "status": status,
        "note": note,
        "budget": BUDGET_MAX_ITERATIONS,
    }


def _check_budget(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not result["asked"]:
        problems.append(
            "the exit was never asked for: no PLAN_NUDGE in the transcript "
            f"(budget {result['budget']})"
        )
    if not result["exited"]:
        problems.append(
            "the round ended without an exit: neither present_plan nor "
            f"finish_research was called (status={result['status']!r}, "
            f"note={result['note']!r})"
        )
    return problems


SCENARIOS = {
    "swap": {"driver": "swap"},
    "note": {"driver": "note"},
    "budget": {"driver": "budget"},
}


async def main(args: argparse.Namespace) -> int:
    app_cfg = ConfigLoader.load_app(args.config)
    model_name = args.model or app_cfg.default_model
    model_cfg = app_cfg.models[model_name]
    model = {
        "name": model_cfg.name,
        "base_url": model_cfg.base_url,
        "api_key": model_cfg.api_key,
        "max_tokens": model_cfg.max_tokens,
    }
    out_root = (
        Path(args.out_dir)
        if args.out_dir
        else Path(f"/tmp/opencode/plan-life-{int(time.time())}")
    )
    # A reused out dir mixes two runs' artifacts, and the assertions read
    # them: the boot-resume check scans every session file under the out
    # dir, so a leftover armed session from an earlier run makes a clean run
    # look broken (measured: two runs into one dir reported two failures
    # that a clean run does not reproduce).  Nothing here is deleted, so the
    # safe move is to refuse rather than to clean.
    if args.out_dir and out_root.is_dir() and any(out_root.iterdir()):
        print(
            f"PLAN LIFECYCLE ERROR: the out dir is not empty: {out_root}\n"
            "  Each run needs its own out dir (the artifacts are kept, never "
            "deleted).  Use a fresh --out-dir, or drop the flag to get a "
            "timestamped one.",
            file=sys.stderr,
        )
        return 3
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"model={model['name']}  upstream={model['base_url']}")
    print(f"artifacts: {out_root}")

    selected = [args.only] if args.only else list(SCENARIOS)
    failures = 0
    for name in selected:
        print(SEP)
        if name == "swap":
            print("[swap] /plan <topic> → approve → /plan off → /plan <topic> again")
            result = await _run_swap(
                topic=args.topic,
                second_topic=args.second_topic,
                model=model,
                out_root=out_root,
            )
            problems = _check_swap(result)
        elif name == "budget":
            print("[budget] /plan <topic> → the exit is asked for before the budget")
            result = await _run_budget(
                topic=args.budget_topic, model=model, out_root=out_root
            )
            problems = _check_budget(result)
        else:
            print("[note] /plan <topic> → the turn ends without a submission")
            result = await _run_note(
                topic=args.note_topic, model=model, out_root=out_root
            )
            problems = _check_note(result)
        print(f"  requests={len(result['entries'])} elapsed={result['elapsed']}s")
        for line in result["console"]:
            print(f"  terminal| {line}")
        if name == "swap":
            print(f"  after_plan={json.dumps(result['after_plan'], ensure_ascii=False)}")
            print(f"  after_off={json.dumps(result['after_off'], ensure_ascii=False)}")
            print(f"  after_replan={json.dumps(result['after_replan'], ensure_ascii=False)}")
        elif name == "budget":
            print(
                f"  budget={result['budget']} ask_at_call={result['ask_at_call']} "
                f"reviews={result['reviews']}"
            )
            print(f"  turn status={result['status']} note={result['note']!r}")
        else:
            print(f"  turn status={result['status']} note={result['note']!r}")
        if problems:
            failures += 1
            for problem in problems:
                print(f"  FAIL: {problem}")
        else:
            print("  PASS")
    print(SEP)
    print(
        "PLAN LIFECYCLE: PASS"
        if failures == 0
        else f"PLAN LIFECYCLE: {failures} scenario(s) FAILED"
    )
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--only", choices=sorted(SCENARIOS), default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--second-topic", default=DEFAULT_SECOND_TOPIC)
    parser.add_argument("--note-topic", default=DEFAULT_NOTE_TOPIC)
    parser.add_argument("--budget-topic", default=DEFAULT_BUDGET_TOPIC)
    parsed = parser.parse_args()
    try:
        code = asyncio.run(main(parsed))
    except Exception as exc:  # noqa: BLE001 -- the live check reports, never hides
        import traceback

        traceback.print_exc()
        print(f"PLAN LIFECYCLE ERROR: {exc!r}", file=sys.stderr)
        code = 3
    raise SystemExit(code)
