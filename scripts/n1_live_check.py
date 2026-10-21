#!/usr/bin/env python3
"""N1 live check — the research exit, against the real model.

Two scenarios, behind ``scripts/recording_proxy.py`` with a fresh temp
workspace, so every byte of the traffic is on disk before it is asserted:

  research — drives the command layer for real: ``/plan <topic>`` enters the
  read-only posture, the model explores the workspace and closes the turn
  with ``finish_research(summary)``.  A research question must not be turned
  into a plan, so the assertions are: the conclusion exit was taken, no plan
  review was ever asked, the task carries no items, and nothing is armed.

  implement — the other half: an implementation request must not be answered
  with a conclusion.  The research exit is refused for it (the request asks
  for work), so the model hands over a plan instead: the assertions are that
  no conclusion succeeded, that a plan was submitted, and that the review
  was asked.

Usage:

    python scripts/n1_live_check.py [--config config.local.yaml]
        [--out-dir /tmp/opencode/n1-live] [--topic "..."]
        [--implement-topic "..."] [--only research|implement]

Artifacts (temp config, JSONL wire recording, session copy) stay under the
out dir; nothing is deleted.  Exit code 0 = the scenarios passed.
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
from ai_coding.service.session_service import SessionService  # noqa: E402

#: A research question: something to find out, nothing to implement.  It does
#: NOT name the tool -- the point is which exit the model picks on its own.
DEFAULT_TOPIC = (
    "调研这个仓库的会话是怎么落盘的：读代码找出答案并说明依据，"
    "不要提出实现改动，也不要写任何文件。"
)

#: An implementation request: the other half of the exit's contract.  The
#: research exit must not answer it -- the work belongs to a plan.
DEFAULT_IMPLEMENT_TOPIC = "给 src/mod1.py 加一个函数，计算两个数的和。"

#: What each scenario proved, in one line.
SCENARIO_VERDICT = {
    "research": "the research exit was taken and nothing was planned",
    "implement": "the implementation request got a plan, not a conclusion",
}


def _tool_calls(session) -> list[dict[str, Any]]:
    """The tool calls the model made, in the session's own transcript."""
    calls: list[dict[str, Any]] = []
    for message in session.messages:
        for call in getattr(message, "tool_calls", None) or ():
            calls.append(
                {
                    "name": getattr(call, "name", ""),
                    "arguments": getattr(call, "arguments", ""),
                }
            )
    return calls


def _tool_results(session) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for message in session.messages:
        if getattr(message, "role", "") == "tool":
            results.append(
                {
                    # the message's field is ``tool_name`` (a tool result is
                    # paired with its call), not ``name``
                    "name": getattr(message, "tool_name", "") or "",
                    "content": (getattr(message, "content", "") or "")[:2000],
                }
            )
    return results


async def run(
    *, topic: str, model: dict[str, Any], out_root: Path, scenario: str = "research"
) -> dict[str, Any]:
    run_dir = out_root / scenario
    run_dir.mkdir(parents=True, exist_ok=True)
    ws = run_dir / "ws"
    ws.mkdir(exist_ok=True)
    _git_workspace(ws)
    if scenario == "implement":
        # The implementation scenario needs a file to change, or the model
        # spends the round looking for one that is not there.
        (ws / "src").mkdir(exist_ok=True)
        (ws / "src" / "mod1.py").write_text(
            "def existing():\n    return 1\n", encoding="utf-8"
        )

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
        registry = _build_tool_registry(app_cfg, str(ws), None, user_prompt=review)
        engine = build_engine_from_config(app_cfg)
        sessions = SessionService(ws / "sessions")
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)
        session = sessions.create()
        started = time.monotonic()

        await handle_input(loop, f"/plan {topic}", session, sessions, console)
        elapsed = round(time.monotonic() - started, 1)

        calls = _tool_calls(session)
        results = _tool_results(session)
        task = session.task
        state = {
            "posture": (session.mode, session.readonly),
            "items": len(task.items) if task else 0,
            "armed": bool(getattr(task, "armed", False)),
            "review_questions": [c for c in review.calls if c[3] == "plan_review"],
            "finish_research_calls": [c for c in calls if c["name"] == "finish_research"],
            "present_plan_calls": [c for c in calls if c["name"] == "present_plan"],
            "tool_results": results,
            "console": list(console.lines),
        }
        await loop.aclose()
    finally:
        server.shutdown()
        server.server_close()

    entries = [
        json.loads(line)
        for line in wire.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    shutil.copy(ws / "sessions" / f"{session.session_id}.json", run_dir / "session.json")
    return {
        "name": scenario,
        "run_dir": str(run_dir),
        "elapsed": elapsed,
        "entries": entries,
        **state,
    }


def _succeeded(result: dict[str, Any], tool: str) -> bool:
    """Did this tool come back successful?  (``ok="false"`` is the wire's own
    way of saying the tool refused.)"""
    for entry in result["tool_results"]:
        if entry["name"] == tool and 'ok="false"' not in entry["content"]:
            return True
    return False


def check(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if result["name"] == "implement":
        # An implementation request belongs to present_plan: the research exit
        # must not answer it, or the work never happens.
        if _succeeded(result, "finish_research"):
            problems.append("the research exit answered an implementation request")
        if not result["present_plan_calls"]:
            problems.append("the model never submitted a plan for an implementation request")
        if not result["review_questions"]:
            problems.append("no plan review was asked (the plan was never handed over)")
        if result["posture"] != ("plan", True):
            problems.append(f"the planning posture was not held: {result['posture']}")
        return problems
    if not result["finish_research_calls"]:
        problems.append("the model never took the research exit")
    if result["present_plan_calls"]:
        problems.append("the model submitted a plan for a research question")
    if result["review_questions"]:
        problems.append("a plan review was asked (the research exit should ask nobody)")
    if result["items"]:
        problems.append(f"the task carries {result['items']} items (a conclusion plans nothing)")
    if result["armed"]:
        problems.append("the task is armed (a conclusion arms nothing)")
    if result["posture"] != ("plan", True):
        problems.append(f"the planning posture was not held: {result['posture']}")
    return problems


async def main(args: argparse.Namespace) -> int:
    app_cfg = ConfigLoader.load_app(args.config)
    model_cfg = app_cfg.models[app_cfg.default_model]
    model = {
        "name": model_cfg.name,
        "base_url": model_cfg.base_url,
        "api_key": model_cfg.api_key,
        "max_tokens": model_cfg.max_tokens,
    }
    out_root = (
        Path(args.out_dir)
        if args.out_dir
        else Path(f"/tmp/opencode/n1-live-{int(time.time())}")
    )
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"model={model['name']}  upstream={model['base_url']}")
    print(f"artifacts: {out_root}")

    scenarios = [
        ("research", args.topic),
        ("implement", args.implement_topic),
    ]
    if args.only:
        scenarios = [s for s in scenarios if s[0] == args.only]
    failed = 0
    for name, topic in scenarios:
        result = await run(topic=topic, model=model, out_root=out_root, scenario=name)
        problems = check(result)
        print("=" * 68)
        print(
            f"[{name}] posture={result['posture']} items={result['items']} "
            f"armed={result['armed']} reviews={len(result['review_questions'])} "
            f"finish_research={len(result['finish_research_calls'])} "
            f"present_plan={len(result['present_plan_calls'])} "
            f"elapsed={result['elapsed']}s"
        )
        for call in result["finish_research_calls"]:
            print(f"  finish_research: {call['arguments'][:400]}")
        for entry in result["tool_results"]:
            if entry["name"] == "finish_research":
                print(f"  result: {entry['content'][:400]}")
        for call in result["present_plan_calls"]:
            print(f"  present_plan: {call['arguments'][:400]}")
        for line in result["console"]:
            print(f"  console: {line}")
        print("=" * 68)
        if problems:
            failed = 1
            for problem in problems:
                print(f"FAIL [{name}] {problem}")
        else:
            print(f"PASS [{name}] {SCENARIO_VERDICT[name]}")
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.local.yaml")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--implement-topic", default=DEFAULT_IMPLEMENT_TOPIC)
    parser.add_argument("--only", default="", choices=["", "research", "implement"])
    raise SystemExit(asyncio.run(main(parser.parse_args())))
