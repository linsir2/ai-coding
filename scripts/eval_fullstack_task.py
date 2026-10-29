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
import re
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
from ai_coding.cli import (  # noqa: E402
    _build_loop,
    _build_tool_registry,
    _make_recorder,
)
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
        return p.returncode, (p.stdout + p.stderr)[-60_000:]
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"
    except FileNotFoundError as exc:
        return 127, str(exc)


#: A pytest section header, e.g. ``=========== FAILURES ===========``.
_PYTEST_SECTION = re.compile(r"^=+ (?P<title>[^=]+?) =+$")
#: A line pytest emits for a warning, with or without a ``path:line:`` prefix.
_PYTEST_WARNING = re.compile(r"^\s*(?:\S+:\d+: )?\w*Warning: ")
_PYTEST_COUNT = re.compile(r"(\d+) (failed|passed|error|errors)")


def _pytest_digest(text: str, limit: int = 4_000) -> str:
    """I2/D7: what a model actually needs out of a pytest run.

    Before this, the feedback was ``log[-1200:]``: on eval-08 stage F that was
    ~500 characters of deprecation warnings plus *one* of eighteen failures, so
    the model kept "fixing" the wrong thing.  The index of every failure is the
    part that matters; the first traceback is the part that explains it.
    """
    lines = text.splitlines()
    failed: list[str] = []
    for line in lines:
        if line.startswith("FAILED ") and line.strip() not in failed:
            failed.append(line.strip())
    sections: list[tuple[str, list[str]]] = []
    title = ""
    body: list[str] = []
    for line in lines:
        header = _PYTEST_SECTION.match(line)
        if header:
            if title or body:
                sections.append((title, body))
            title = header.group("title").strip()
            body = []
            continue
        body.append(line)
    if title or body:
        sections.append((title, body))

    def clean(raw: list[str]) -> list[str]:
        out: list[str] = []
        for line in raw:
            if _PYTEST_WARNING.match(line):
                continue
            if line.strip().startswith(("warnings summary", "PytestDeprecationWarning")):
                continue
            out.append(line)
        return out

    parts: list[str] = []
    if failed:
        parts.append("\n".join(failed))
    failures = next((clean(b) for t, b in sections if t == "FAILURES"), [])
    if failures:
        parts.append("=== 首个失败详情 ===")
        parts.append("\n".join(failures).strip())
    counts = "，".join(
        f"{n} {kind}" for n, kind in _PYTEST_COUNT.findall(" ".join(lines[-20:]))
    )
    if counts:
        parts.append(f"[{counts}]")
    if not parts:
        return _tail_at_line("\n".join(clean(lines)), limit)
    joined = "\n".join(part for part in parts if part)
    return joined if len(joined) <= limit else joined[: limit - 20] + "\n…（已截断）"


def _tail_at_line(text: str, limit: int) -> str:
    """The last ``limit`` characters, starting at a line boundary.

    A mid-line cut turns a traceback's first line into noise ("ecent call
    last)"), which is exactly what the model was handed for a failed install.
    """
    body = text if len(text) <= limit else text[-limit:]
    if len(text) > limit and "\n" in body:
        body = body.split("\n", 1)[1]
    return body


def self_check(ws: Path) -> dict:
    """Run the two hard gates the task itself demands."""
    out: dict[str, object] = {}
    # R21: the project gets its **own** interpreter.  Running its
    # ``pip install -r requirements.txt`` against ``sys.executable`` installed a
    # pinned ``pytest<8`` into this repo's venv, which then broke
    # ``pytest-asyncio`` for every other suite -- measured: the agent venv went
    # 8.4.2 -> 7.4.4 mid-run and both the eval gate and the repo's own tests
    # died.  A project is not allowed to mutate the harness it is measured by.
    py = str(ws / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable  # no venv yet: fall back, but say so in the log
        out["project_venv"] = False
    else:
        out["project_venv"] = True
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
        # pytest runs even when the install failed: the interpreter's own
        # site-packages often already has the dependency, and feeding the model
        # a pip traceback instead of the test failures hides the only thing it
        # can act on (measured: a pydantic-core source build failed on Python
        # 3.14 while the tests themselves ran fine).
        rc2, log2 = _run([py, "-m", "pytest", "-q", "--no-header"], be)
        out["be_pytest"] = rc2
        body = _pytest_digest(log2) if rc2 not in (0, None) else log2[-1200:]
        if rc != 0:
            body = (
                f"[依赖安装失败] pip install 返回 {rc}（测试仍然照常运行）：\n"
                f"{_tail_at_line(log, 1200)}\n\n{body}"
            )
        out["be_log"] = body
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
    # R21: a project-local interpreter, created up front.  Without it the gate
    # installs the project's pins into *this* venv (see `self_check`).
    if not (ws / ".venv" / "bin" / "python").exists():
        subprocess.run(
            [sys.executable, "-m", "venv", str(ws / ".venv")],
            check=False,
            capture_output=True,
        )

    sessions = SessionService(ws / "sessions")
    registry = _build_tool_registry(app_cfg, str(ws), None)
    # R22 §3.2: the harness used to build its loop with ``trace=None``, so a
    # long run left no trace at all — "what did the model actually receive"
    # had no evidence but the proxy log.  ``_make_recorder`` is the same
    # constructor the CLI and the TUI use; the loop points it at whichever
    # session runs (``agent_loop._begin_turn`` -> ``trace.session_id``).
    recorder = _make_recorder(app_cfg, str(ws), None)
    loop = _build_loop(
        build_engine_from_config(app_cfg),
        sessions,
        registry,
        app_cfg,
        str(ws),
        None,
        trace=recorder,
    )
    if recorder is not None:
        print(f"[eval] trace -> {recorder.store.root}")
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
