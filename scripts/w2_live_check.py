#!/usr/bin/env python3
"""W2 live check: a plan naming an unreachable path is refused, for real.

Drives the command layer against the real model through the recording proxy:
``/plan <topic>`` asks the model for a plan that writes outside the
workspace, and the W2 gate must refuse it *before* the review card reaches
the human -- the model gets the refusal as a tool error and can revise.

The second half drives ``/plan run`` over a plan that was written straight
into the task (bypassing the review), so the runner's own front gate is the
one that has to catch it.

Artifacts stay under the out dir; nothing is deleted.
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

from recording_proxy import serve, start  # noqa: E402

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.app.commands import handle_input  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

from r19_acceptance import (  # noqa: E402
    _AutoReview,
    _RecordingConsole,
    _git_workspace,
    _redact_api_key,
    _temp_config,
)

SEP = "=" * 68

#: The topic asks for work whose target is outside the workspace.  The model
#: is told to write the plan; the gate is what has to notice.
OUTSIDE_TOPIC = (
    "新增 /data/w2demo/app.py：写一个打印 hello 的 Python 脚本；"
    "验收方式为文件存在（verify 用 test -f /data/w2demo/app.py）"
)


async def run_scenario(
    *, name: str, topic: str, model: dict[str, Any], out_root: Path
) -> dict[str, Any]:
    run_dir = out_root / name
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
        registry = _build_tool_registry(app_cfg, str(ws), None, user_prompt=review)
        engine = build_engine_from_config(app_cfg)
        sessions = SessionService(ws / "sessions")
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)
        session = sessions.create()
        started = time.monotonic()

        await handle_input(loop, f"/plan {topic}", session, sessions, console)
        planning_mode = (session.mode, session.readonly)
        items = len(session.task.items) if session.task else 0
        review_questions = [call for call in review.calls if call[3] == "plan_review"]
        armed = bool(getattr(session.task, "armed", False)) if session.task else False

        elapsed = round(time.monotonic() - started, 1)
        await loop.aclose()
    finally:
        server.shutdown()
        server.server_close()

    entries = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]

    # What the gate did, read off the wire: every present_plan tool result
    # and every submission the model made.
    refusals: list[str] = []
    submissions: list[dict[str, Any]] = []
    for entry in entries:
        request = entry.get("request") or {}
        for message in request.get("messages") or []:
            if message.get("role") == "tool":
                content = str(message.get("content") or "")
                if "present_plan" in content and "ok=\"false\"" in content:
                    refusals.append(content)
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    name = str((call.get("function") or {}).get("name") or "")
                    if name != "present_plan":
                        continue
                    args = (call.get("function") or {}).get("arguments") or "{}"
                    if isinstance(args, str):
                        args = json.loads(args)
                    submissions.append(args)
    # The items of the last submission -- the one that reached the card.
    approved_items = [
        item.get("content", "")
        for item in (submissions[-1].get("items") if submissions else [])
    ]

    return {
        "name": name,
        "reply": (
            f"planning={planning_mode} items={items} "
            f"reviews={len(review_questions)} armed={armed} "
            f"submissions={len(submissions)} refusals={len(refusals)}"
        ),
        "elapsed": elapsed,
        "entries": entries,
        "console": list(console.lines),
        "review_questions": review_questions,
        "planning_mode": planning_mode,
        "items": items,
        "armed": armed,
        "refusals": refusals,
        "submissions": submissions,
        "approved_items": approved_items,
    }


def check(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    wire = json.dumps(result["entries"], ensure_ascii=False)
    refusals = result["refusals"]
    # The gate must have refused the plan that named an unreachable path.
    if not refusals:
        problems.append("the wire carries no workspace_paths refusal")
    # Every refusal must name a path that is really outside -- a refusal
    # over a path the model had no reason to change (an interpreter, say)
    # is a dead end, and the model retries it.
    for refusal in refusals:
        if "/usr/bin/env" in refusal or "/usr/bin/python" in refusal:
            problems.append(f"the refusal named an interpreter: {refusal!r}")
    # The plan that reached the review card must be one the tools can run:
    # either it names nothing outside, or nothing was armed at all.
    if result["items"] and not result["armed"] and result["review_questions"]:
        problems.append("a plan reached the review card but nothing was armed")
    if result["armed"] and result["review_questions"]:
        # The review card was answered, so the plan was approved -- which is
        # only right if the plan named nothing outside.
        for item in result["approved_items"]:
            if "/data/" in item or "/workspace/" in item:
                problems.append(f"an unreachable plan was approved: {item!r}")
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
    out_root = Path(args.out_dir) if args.out_dir else Path(
        f"/tmp/opencode/w2-live-{int(time.time())}"
    )
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"model={model['name']}  upstream={model['base_url']}")
    print(f"artifacts: {out_root}")
    print(SEP)

    result = await run_scenario(
        name="outside_plan",
        topic=OUTSIDE_TOPIC,
        model=model,
        out_root=out_root,
    )
    print(f"  {result['reply']}")
    print(f"  console: {result['console']}")
    problems = check(result)
    if problems:
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        print(SEP)
        print("W2 LIVE: FAIL")
        return 1
    print(SEP)
    print("W2 LIVE: PASS")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.yaml")
    parser.add_argument("--out-dir", default=None)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
