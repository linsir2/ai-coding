#!/usr/bin/env python3
"""批次 4a live check — the plan's lifecycle: giving one up, and being heard.

Two scenarios, behind ``scripts/recording_proxy.py`` with a fresh temp
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

Usage:

    python scripts/plan_lifecycle_live_check.py [--config config.local.yaml]
        [--out-dir /tmp/opencode/plan-life] [--only swap|note]
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
from ai_coding.core.turn_policy import PLAN_UNSUBMITTED_NOTE  # noqa: E402
from ai_coding.server.world import find_armed_session  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

SEP = "=" * 68

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
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)
        session = sessions.create()
        started = time.monotonic()

        # 1. plan + approve
        await handle_input(loop, f"/plan {topic}", session, sessions, console)
        task = session.task
        after_plan = {
            "armed": bool(getattr(task, "armed", False)),
            "profile": str(getattr(task, "armed_profile", "") or ""),
            "items": len(task.items) if task else 0,
            "reviews": len([c for c in review.calls if c[3] == "plan_review"]),
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


SCENARIOS = {
    "swap": {"driver": "swap"},
    "note": {"driver": "note"},
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
    parsed = parser.parse_args()
    try:
        code = asyncio.run(main(parsed))
    except Exception as exc:  # noqa: BLE001 -- the live check reports, never hides
        import traceback

        traceback.print_exc()
        print(f"PLAN LIFECYCLE ERROR: {exc!r}", file=sys.stderr)
        code = 3
    raise SystemExit(code)
