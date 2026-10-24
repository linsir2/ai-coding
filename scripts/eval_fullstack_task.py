#!/usr/bin/env python3
"""eval-05: a long, multi-round coding task -- build a full-stack app.

The task (self-set by the evaluator):

    "TaskFlow" -- a runnable full-stack task manager.
      backend : Python + FastAPI + SQLite, REST API (CRUD, status flow, tags,
                keyword search, stats) WITH pytest tests
      frontend: React + TypeScript + Vite (list, kanban, create/edit, stats)
      delivery: root README with run steps; backend pytest green;
                frontend `npm run build` ok; both services start

Driven headlessly through the real AgentLoop, one prompt per round, with the
self-check output fed back into the next round.  Usage:

    .venv/bin/python scripts/eval_fullstack_task.py --config config.local.yaml \
        --workspace /workspace/eval-workspace/taskflow --rounds 14
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.app.commands import handle_input  # noqa: E402
from ai_coding.app.console import DialogueConsole  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.infra.logger_setup import setup_logging  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

SEP = "=" * 68

TASK = """请在当前工作区从零构建一个可运行的全栈项目「TaskFlow」任务管理系统。

【后端】Python + FastAPI + SQLite（用 SQLAlchemy 或原生 sqlite3 均可）
- REST API：任务的增删改查；状态流转 todo -> in_progress -> done；标签（多标签）；
  关键字搜索；按状态/标签的统计接口
- 自带 pytest 测试，覆盖主要 API（至少 6 个测试）
- 目录建议 backend/，依赖写 backend/requirements.txt
- 监听 8000 端口

【前端】React + TypeScript + Vite
- 页面：任务列表、看板视图（按状态分列）、新建/编辑任务、统计面板
- 调用后端 API（注意跨域）
- 目录建议 frontend/，`npm run build` 必须成功
- 监听 5173 端口

【交付】根目录 README.md 写明后端/前端各自的启动步骤。

要求：
1. 代码要能真的跑起来，不要写伪代码或 TODO 占位
2. 后端测试必须能通过
3. 前端必须能 build 成功
4. 分步骤实现：先搭后端并跑通测试，再做前端并 build 通过，最后补 README
5. 每一轮只做一部分，做完用 bash 自检（后端 pytest、前端 npm run build）
"""


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout + p.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except FileNotFoundError as exc:
        return 127, str(exc)


def self_check(ws: Path) -> dict:
    """Run the two hard gates the task itself demands."""
    out: dict[str, object] = {}
    py = sys.executable
    be = ws / "backend"
    if (be / "requirements.txt").exists() or (be / "pyproject.toml").exists():
        rc, log = (
            _run([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"], be)
            if (be / "requirements.txt").exists()
            else (0, "no requirements.txt")
        )
        out["be_install"] = rc
        # No --timeout: pytest-timeout is not in this venv, and passing an
        # unknown flag makes pytest exit 4 (a false negative on the gate).
        rc2, log2 = (
            _run([py, "-m", "pytest", "-q", "--no-header"], be)
            if rc == 0
            else (rc, log)
        )
        out["be_pytest"] = rc2
        out["be_log"] = log2[-1200:]
    else:
        out["be_pytest"] = None
        out["be_log"] = "backend/ not created yet"
    fe = ws / "frontend"
    if (fe / "package.json").exists():
        if not (fe / "node_modules").exists():
            rc, log = _run(["npm", "install", "--no-audit", "--no-fund"], fe, 600)
            out["fe_install"] = rc
        rc2, log2 = _run(["npm", "run", "build"], fe, 600)
        out["fe_build"] = rc2
        out["fe_log"] = log2[-1200:]
    else:
        out["fe_build"] = None
        out["fe_log"] = "frontend/ not created yet"
    files = sorted(
        str(p.relative_to(ws)) for p in ws.rglob("*")
        if p.is_file()
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and ".git" not in p.parts
    )
    out["files"] = files
    return out


def _verdict(sc: dict) -> tuple[bool, str]:
    be = sc.get("be_pytest")
    fe = sc.get("fe_build")
    ok = be == 0 and fe == 0
    return ok, f"be_pytest={be} fe_build={fe} files={len(sc.get('files', []))}"


async def main(config_path: str, ws: Path, rounds: int, keep: bool = False) -> int:
    app_cfg = ConfigLoader.load_app(config_path)
    app_cfg.ai.max_tool_iterations = 30
    app_cfg.ai.turn_timeout_seconds = 1800
    app_cfg.ai.max_auto_continuations = 4
    if ws.exists() and not keep:
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / ".git").exists():
        (ws / ".git").mkdir()  # repo-ish root so worktree isolation can work
        subprocess.run(["git", "init", "-q"], cwd=ws, check=True)

    sessions = SessionService(ws / "sessions")
    registry = _build_tool_registry(app_cfg, str(ws), None)
    loop = _build_loop(
        build_engine_from_config(app_cfg), sessions, registry, app_cfg, str(ws), None
    )
    session = sessions.create()
    console = DialogueConsole()

    log: list[dict] = []
    t_start = time.monotonic()
    prompt = TASK
    for r in range(1, rounds + 1):
        print(f"\n{SEP}\n[round {r}] {prompt[:110]}...")
        t0 = time.monotonic()
        action, session = await handle_input(loop, prompt, session, sessions, console)
        dt = time.monotonic() - t0
        sc = self_check(ws)
        ok, verdict = _verdict(sc)
        tools = [m.tool_name for m in session.messages if m.role == "tool"]
        print(f"  action={action} {dt:.1f}s tools={len(tools)} {verdict}")
        print(f"  files({len(sc['files'])}): {', '.join(sc['files'][:14])}")
        log.append(
            {"round": r, "seconds": round(dt, 1), "tools": len(tools),
             "verdict": verdict, "ok": ok, "files": sc["files"],
             "be_log": sc.get("be_log", ""), "fe_log": sc.get("fe_log", ""),
             "prompt": prompt[:200]}
        )
        if ok:
            print(f"\n{SEP}\n  TASK COMPLETE at round {r}")
            break
        # Feed the failing gate back in as the next instruction.
        if sc.get("be_pytest") not in (0, None):
            prompt = (
                "后端测试还没通过。请阅读下面的 pytest 输出，定位并修复，然后重新跑 "
                "`python -m pytest -q` 确认全绿。修完再继续下一步。\n\n"
                + str(sc.get("be_log", ""))[-2500:]
            )
        elif sc.get("fe_build") not in (0, None):
            prompt = (
                "前端 build 还没通过。请阅读下面的构建输出，定位并修复，然后重新跑 "
                "`npm run build` 确认成功。修完再继续下一步。\n\n"
                + str(sc.get("fe_log", ""))[-2500:]
            )
        else:
            prompt = (
                "继续实现 TaskFlow 的剩余部分。每完成一部分就用 bash 自检"
                "（后端 pytest、前端 npm run build），把失败输出读进来修复。"
                "全部完成后写根目录 README.md（含后端/前端启动步骤）。"
            )
    else:
        print(f"\n{SEP}\n  ROUNDS EXHAUSTED")

    total = time.monotonic() - t_start
    (ws / "eval_task_log.json").write_text(
        json.dumps({"rounds": log, "total_seconds": round(total, 1)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  total {total:.1f}s, rounds={len(log)}")
    print(f"  log -> {ws / 'eval_task_log.json'}")
    await loop.aclose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    parser.add_argument("--workspace", default="/workspace/eval-workspace/taskflow")
    parser.add_argument("--rounds", type=int, default=14)
    parser.add_argument("--keep", action="store_true", help="reuse the existing workspace")
    args = parser.parse_args()
    setup_logging()
    raise SystemExit(
        asyncio.run(main(args.config, Path(args.workspace), args.rounds, args.keep))
    )
