#!/usr/bin/env python3
"""R19 live acceptance — stage 1 takes over the wire (13/15), against the real model.

Five scenarios, each behind ``scripts/recording_proxy.py`` with a fresh temp
workspace, so every byte of request/response traffic is on disk before it is
asserted:

  1. simple  — one tool turn.  Structural invariants that must hold on every
               request no matter what the model does: ``<methods>`` in the
               system prompt, one ``<environment>`` block leading the tail,
               with the real ``分支:`` line from the git workspace.
  2. ladder  — a pressure prompt that keeps the model retrying the same
               missing-file read.  Asserts the intervention ledger engaged
               (strategy >= L1) and prints the level/outcome/fires trail plus
               the denials visible on the wire — the live counterpart of
               ``tests/test_orchestration_wire.py``.
  3. broken  — the proxy cuts the first stream right after its first content
               delta.  Asserts the continuation request carries the persisted
               ``[stream-interrupted]`` note (B) and the turn still completes.
  4. closing — a tool use with ``max_tool_iterations=1`` forces the iteration-
               budget ending.  Asserts the closing call carries the closing
               prompt and offers no tools (G).
  5. plan_mode — drives the command layer for real (19 §3.2/§3.3): `/plan
               <topic>` enters the read-only posture and plans, the model
               submits through the real ``present_plan`` tool whose review
               channel is answered by a scripted approver, and the run is
               dispatched after the command returns (the frontend's terminal
               mirror).  Asserts posture, review, approval on the wire and the
               executed steps.

Usage:

    .venv/bin/python scripts/r19_acceptance.py [--config config.local.yaml]
        [--out-dir /tmp/opencode/r19-accept] [--only simple|ladder|broken]

Artifacts (temp config, JSONL wire recordings, session copies) stay under the
out dir; nothing is deleted.  Exit code 0 = all selected scenarios passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from recording_proxy import serve, start  # noqa: E402

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.app.commands import handle_input  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.core.agent_loop import _CLOSING_PROMPT  # noqa: E402
from ai_coding.domain.message import ToolResult  # noqa: E402
from ai_coding.security.user_prompt import UserAnswer  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402
from ai_coding.tools.base import BaseTool, ToolContext  # noqa: E402

SEP = "=" * 68


class _AskUser(BaseTool):
    """Headless stand-in for the UI-bound ask_user (same name, same signal)."""

    parallel_safe = True

    @property
    def name(self) -> str:
        return "ask_user"

    @property
    def description(self) -> str:
        return "ask the user a question"

    @property
    def params_json_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"question": {"type": "string"}}}

    async def execute(self, params: dict[str, Any], ctx: ToolContext | None = None) -> ToolResult:
        return ToolResult(success=True, output="用户答复：继续")


def _git_workspace(ws: Path) -> None:
    (ws / "sample.txt").write_text("hello world from r19\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=ws, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=r19@acceptance",
            "-c",
            "user.name=r19",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=ws,
        check=True,
    )


def _temp_config(
    path: Path, model: dict[str, Any], proxy_port: int, upstream: str, max_iterations: int
) -> None:
    suffix = urlsplit(upstream).path  # keep the client's path suffix identical
    path.write_text(
        "models:\n"
        "  live:\n"
        f"    name: {model['name']}\n"
        f'    base_url: "http://127.0.0.1:{proxy_port}{suffix}"\n'
        f"    api_key: {model['api_key']}\n"
        "    streaming: true\n"
        f"    max_tokens: {model.get('max_tokens', 2048)}\n"
        "    temperature: 0.2\n"
        "    timeout: 120\n"
        "    max_retries: 0\n"
        "default_model: live\n"
        "ai:\n"
        f"  max_tool_iterations: {max_iterations}\n",
        encoding="utf-8",
    )


def _redact_api_key(config_path: Path, api_key: str) -> None:
    """The key goes on the wire, not into the kept artifacts."""
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(text.replace(api_key, "<redacted>"), encoding="utf-8")


def _messages(entry: dict) -> list[dict]:
    request = entry.get("request") or {}
    messages = request.get("messages")
    return messages if isinstance(messages, list) else []


def _conversation(entries: list[dict]) -> list[dict]:
    return [
        e
        for e in entries
        if _messages(e) and "<methods>" in str(_messages(e)[0].get("content", ""))
    ]


def _flat(entry: dict) -> str:
    return json.dumps(_messages(entry), ensure_ascii=False)


def _env_ok(entry: dict) -> bool:
    envs = [m for m in _messages(entry) if str(m.get("content", "")).startswith("<environment>")]
    if len(envs) != 1:
        return False
    body = envs[0]["content"]
    return "工作区: " in body and "日期: " in body


async def _run_scenario(
    *,
    name: str,
    prompt: str,
    model: dict[str, Any],
    out_root: Path,
    cut_first: int = 0,
    max_iterations: int = 12,
) -> dict[str, Any]:
    run_dir = out_root / name
    run_dir.mkdir(parents=True, exist_ok=True)
    ws = run_dir / "ws"
    ws.mkdir(exist_ok=True)
    _git_workspace(ws)

    wire = run_dir / "wire.jsonl"
    server = serve(upstream=model["base_url"], port=0, out_path=wire, cut_first=cut_first)
    start(server)
    port = server.server_address[1]

    config_path = run_dir / "config.yaml"
    _temp_config(config_path, model, port, model["base_url"], max_iterations)
    try:
        app_cfg = ConfigLoader.load_app(str(config_path))
        _redact_api_key(config_path, model["api_key"])
        registry = _build_tool_registry(app_cfg, str(ws), None)
        registry.register(_AskUser())
        engine = build_engine_from_config(app_cfg)
        sessions = SessionService(ws / "sessions")
        loop = _build_loop(engine, sessions, registry, app_cfg, str(ws), None)
        session = sessions.create()
        started = time.monotonic()
        reply = await loop.process_input(session, prompt)
        elapsed = round(time.monotonic() - started, 1)
        await loop.aclose()
    finally:
        server.shutdown()
        server.server_close()

    entries = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    persisted = sessions.load(session.session_id)
    shutil.copy(ws / "sessions" / f"{session.session_id}.json", run_dir / "session.json")
    return {
        "name": name,
        "run_dir": str(run_dir),
        "reply": reply,
        "elapsed": elapsed,
        "entries": entries,
        "ledger": persisted.intervention_state,
    }


class _AutoReview:
    """Stands in for the review card: approves any plan review instantly.

    The real channel is ``user_prompt.ask`` with ``intent="plan_review"``;
    answering it IS the tool result (19 §3.2), so a scripted approver is the
    headless frontend."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def ask(self, question, options=(), *, detail="", intent=""):
        self.calls.append((question, tuple(options), detail, intent))
        if intent == "plan_review":
            return UserAnswer(answered=True, text='{"choice":"approve"}')
        return UserAnswer(answered=False, reason="not_applicable")


class _RecordingConsole:
    """The console surface ``handle_input`` writes through, offline."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def delta(self, token: str) -> None:
        return None

    def info(self, message: str) -> None:
        self.lines.append(message)

    def error(self, message: str) -> None:
        self.lines.append(message)

    def rule(self, title: str = "") -> None:
        return None

    def step(self, title: str) -> Any:
        return None


async def _run_plan_scenario(
    *, name: str, topic: str, model: dict[str, Any], out_root: Path
) -> dict[str, Any]:
    """Drive /plan → forced write attempt → /plan run, recording the wire."""
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
        # The real registry with a scripted human: present_plan (and the real
        # ask_user) register only when a user_prompt exists (builder), and the
        # review rides that channel — no stub in the way.
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

        # The frontend dispatches the run at the terminal turn.finished once the
        # card answered approve; headless, that moment is right here.
        await handle_input(loop, "/plan run", session, sessions, console)
        elapsed = round(time.monotonic() - started, 1)
        after_run = (session.mode, session.readonly)
        receipts = len(session.step_deliveries)
        await loop.aclose()
    finally:
        server.shutdown()
        server.server_close()

    entries = [json.loads(line) for line in wire.read_text(encoding="utf-8").splitlines()]
    persisted = sessions.load(session.session_id)
    shutil.copy(ws / "sessions" / f"{session.session_id}.json", run_dir / "session.json")
    return {
        "name": name,
        "run_dir": str(run_dir),
        "reply": (
            f"planning={planning_mode} items={items} "
            f"after_run={after_run} receipts={receipts}"
        ),
        "elapsed": elapsed,
        "entries": entries,
        "ledger": persisted.intervention_state,
        "console": list(console.lines),
        "review_questions": review_questions,
        "planning_mode": planning_mode,
        "items": items,
        "after_run": after_run,
        "receipts": receipts,
        "persisted": (persisted.mode, persisted.readonly),
    }


def _check_plan_mode(result: dict[str, Any]) -> list[str]:
    problems = _check_simple(result)
    conv = _conversation(result["entries"])
    # P2: the planning turn's system prompt carries the posture segment.
    if not any("<plan-mode>" in _messages(e)[0]["content"] for e in conv):
        problems.append("no request carried the <plan-mode> segment")
    if result["planning_mode"] != ("plan", True):
        problems.append(f"planning posture was never set: {result['planning_mode']}")
    if result["items"] < 1:
        problems.append("the planning turn wrote no todo items")
    # 19 §3.2: the plan went through the real review channel…
    if not result.get("review_questions"):
        problems.append("present_plan never reached the review channel")
    else:
        question, options, detail, _intent = result["review_questions"][-1]
        if "批准" not in question or "开始执行" not in options:
            problems.append(f"the review prompt looks wrong: {question!r} {options!r}")
        if not detail.strip():
            problems.append("the review carried no plan markdown")
    # …the approval came back as the tool result (on the wire)…
    if "计划已批准" not in "".join(_flat(e) for e in conv):
        problems.append("the approved tool result never appeared on the wire")
    # …and the removed "计划就绪" claim stayed removed (19 §3.5).
    if any("计划就绪" in line for line in result.get("console", [])):
        problems.append("the retired '计划就绪' notice is back")
    # P4: /plan run disarms and executes through the runner.
    if result["after_run"] != ("normal", False):
        problems.append(f"/plan run did not disarm: {result['after_run']}")
    if result["persisted"] != ("normal", False):
        problems.append(f"the file does not read back disarmed: {result['persisted']}")
    if not any("只做这一步" in _flat(e) for e in conv):
        problems.append("no plan step ran through the engine")
    return problems


def _check_simple(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    conv = _conversation(result["entries"])
    if not conv:
        problems.append("no conversation request was recorded")
    for i, entry in enumerate(conv):
        if not _env_ok(entry):
            problems.append(f"r{i + 1}: environment block missing/misplaced")
        if "分支: " not in _flat(entry):
            problems.append(f"r{i + 1}: the real git branch line is absent")
    return problems


def _check_ladder(result: dict[str, Any]) -> list[str]:
    problems = _check_simple(result)
    ledger = result["ledger"] or {}
    strategy = ledger.get("strategy") or {}
    level = int(strategy.get("level", 0) or 0)
    if level < 1:
        problems.append(f"the strategy ladder never engaged (ledger={ledger})")
    gate_hits = sum(_flat(e).count("[strategy-gate]") for e in _conversation(result["entries"]))
    if gate_hits == 0:
        problems.append("no [strategy-gate] message visible on the wire")
    return problems


def _check_broken(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    conv = _conversation(result["entries"])
    if len(conv) < 2:
        problems.append(f"expected a continuation request, got {len(conv)} conversation calls")
        return problems
    if not conv[0].get("aborted"):
        problems.append("the proxy never actually cut the first stream")
    if "[stream-interrupted]" not in _flat(conv[1]):
        problems.append("the continuation request carries no [stream-interrupted] note")
    if not (result["reply"] or "").strip():
        problems.append("the turn produced no reply after the continuation")
    return problems


def _check_closing(result: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    conv = _conversation(result["entries"])
    if len(conv) < 2:
        problems.append(f"expected a closing request, got {len(conv)} conversation calls")
        return problems
    closing = conv[-1]
    if "tools" in closing["request"]:
        problems.append("the closing request still offered tools (G)")
    if _CLOSING_PROMPT not in str(_messages(closing)[-1].get("content", "")):
        problems.append("the last request is not the closing call")
    if not (result["reply"] or "").strip():
        problems.append("the budget ending produced no closing text")
    return problems


SCENARIOS: dict[str, dict[str, Any]] = {
    "simple": {
        "prompt": "用 read 工具读取 sample.txt，然后用一句话说明文件内容。",
        "check": _check_simple,
        "cut_first": 0,
    },
    "ladder": {
        "prompt": (
            "现在只允许使用 read 工具（禁止使用 todo_write、bash 或任何其他工具）。"
            "请依次读取这些文件：missing_a.txt、missing_b.txt、missing_c.txt、"
            "missing_d.txt、missing_e.txt、missing_f.txt（它们都不存在）。每个文件"
            "单独读取一次，读完一个再读下一个，失败也不要停下。全部尝试完之后用一个"
            "表格汇报每个文件的读取结果。"
        ),
        "check": _check_ladder,
        "cut_first": 0,
    },
    "broken": {
        "prompt": "写一段约 200 字的散文，描述秋天的海。直接输出正文。",
        "check": _check_broken,
        "cut_first": 1,
    },
    "closing": {
        "prompt": "用 bash 运行 echo hello 证明工具可用，看到结果后直接总结。",
        "check": _check_closing,
        "cut_first": 0,
        "max_iterations": 1,  # any tool use exhausts the budget -> closing call
    },
    "plan_mode": {
        "prompt": (
            "新增 PLAN_DEMO.md：写明本仓库的一句话用途；"
            "验收方式为文件存在（verify 用 test -f PLAN_DEMO.md）"
        ),
        "check": _check_plan_mode,
        "cut_first": 0,
        "driver": "plan",  # handled by _run_plan_scenario (command layer)
    },
}


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
        else Path(f"/tmp/opencode/r19-accept-{int(time.time())}")
    )
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"model={model['name']}  upstream={model['base_url']}")
    print(f"artifacts: {out_root}")

    selected = [args.only] if args.only else list(SCENARIOS)
    failures = 0
    for name in selected:
        spec = SCENARIOS[name]
        print(SEP)
        print(f"[{name}] {spec['prompt'][:60]}...")
        if spec.get("driver") == "plan":
            result = await _run_plan_scenario(
                name=name, topic=spec["prompt"], model=model, out_root=out_root
            )
        else:
            result = await _run_scenario(
                name=name,
                prompt=spec["prompt"],
                model=model,
                out_root=out_root,
                cut_first=spec["cut_first"],
                max_iterations=spec.get("max_iterations", 12),
            )
        problems = spec["check"](result)
        conv = _conversation(result["entries"])
        gate = sum(_flat(e).count("[strategy-gate]") for e in conv)
        denied = sum(_flat(e).count("refused by runtime guard") for e in conv)
        print(
            f"  requests={len(result['entries'])} conversation={len(conv)} "
            f"gate_msgs={gate} denials={denied} elapsed={result['elapsed']}s"
        )
        print(f"  ledger={json.dumps(result['ledger'], ensure_ascii=False)}")
        print(f"  reply({len(result['reply'])} chars): {result['reply'][:80]!r}")
        if problems:
            failures += 1
            for problem in problems:
                print(f"  FAIL: {problem}")
        else:
            print("  PASS")
    print(SEP)
    print(
        "R19 ACCEPTANCE: PASS"
        if failures == 0
        else f"R19 ACCEPTANCE: {failures} scenario(s) FAILED"
    )
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--only", choices=sorted(SCENARIOS), default="")
    parsed = parser.parse_args()
    try:
        code = asyncio.run(main(parsed))
    except Exception as exc:  # noqa: BLE001 -- the acceptance driver reports, never hides
        import traceback

        traceback.print_exc()
        print(f"R19 ERROR: {exc!r}", file=sys.stderr)
        code = 3
    raise SystemExit(code)
