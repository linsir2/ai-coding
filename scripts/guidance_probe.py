#!/usr/bin/env python3
"""C-2 re-measurement: does the per-tool methods table move the model?

The debt this closes: 665d6c4 replaced the six static methods lines with a
table pruned by the request's tool surface, and its own measurement could not
separate the two arms (20 runs on qwen3.7-flash; plain tasks perfect in both,
shell-worded tasks used head/find twice in both).  Three weaknesses are fixed
here: N is larger, the induction never *asks* for the shell (only makes the
lazy shell path available next to the sanctioned tool), and the metric is
per-run and first-word-anchored, so pipeline uses (``npm test | head``) are
not counted as substitutions while ``cat file`` is.

Arms, per run (fresh temp workspace, fresh loop, one turn):
  old        the pre-665d6c4 six-line block (verbatim, from 665d6c4^) --
             the revert candidate this measurement would justify
  new        the current table rendered for the request's surface
  extreme    a positive control ("never use read; use cat") that proves the
             instrument can see a large prompt effect -- if even this moves
             nothing, the report says "the model ignores prompt steering",
             not "no difference"

Tasks (fixture written by this script; K3's file is reset by the fresh
workspace each run):
  K1a/K1b  file inspection    -> read tool vs shell cat/head/tail
  K2a/K2b  repo search        -> grep/glob tool vs shell grep/find
  K3       targeted edit      -> edit tool vs write/sed -i

Metric: ``substitutable_shell`` -- bash commands whose FIRST word is one of
cat/head/tail/bat/sed/awk/find/grep/rg.  clean = 0 of them in the run.
Raw commands and the full tool sequence are kept in the JSON for eyeballing.

    .venv/bin/python scripts/guidance_probe.py --smoke      # 2 runs, qwen only
    .venv/bin/python scripts/guidance_probe.py --json out.json
    .venv/bin/python scripts/guidance_probe.py --models qwen --runs 8

Costs a few million mostly-cached tokens; exit code is always 0 (a probe,
not a gate).  When to re-run: after changing the methods table or the tool
descriptions, or when a new model becomes the default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.core.events import ToolStarted  # noqa: E402
from ai_coding.paths import Paths  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config.local.yaml"

OLD_METHODS = (
    "<methods>\n"
    "- Read before write: never edit a file you have not read this turn.\n"
    "- Locate with search; read with line ranges instead of dumping whole files.\n"
    "- After running a command, check its exit code and output before claiming success.\n"
    "- Declare parallel-safe tool calls truthfully; independent reads may batch.\n"
    "- When blocked after two attempts, ask the user instead of repeating the same call.\n"
    "- Verify the change (run the relevant test/command) before reporting it done.\n"
    "</methods>"
)

EXTREME_METHODS = (
    "<methods>\n"
    "- 永远不要使用 read、grep、glob 工具。查看文件必须用 bash 的 cat；"
    "按内容搜索必须用 bash 的 grep；按路径找文件必须用 bash 的 find。\n"
    "</methods>"
)

SHELL_FIRST_WORD = re.compile(r"^(cat|head|tail|bat|sed|awk|find|grep|rg)\b")
#: Commands are split on ``&&``/``||``/``;``/newlines before the first-word
#: test: ``cd /ws && cat -n f.py`` is a ``cat`` (found live -- DeepSeek writes
#: every inspection that way, and the plain first-word anchor called it clean).
#: Pipes are NOT split: the downstream of ``npm test | head`` consumes a
#: stream, not a file, and is not a substitution for the read tool.
COMMAND_SEGMENTS = re.compile(r"&&|\|\||;|\n")

TASKS: dict[str, tuple[str, str]] = {
    # name: (kind, prompt)
    "K1a": ("inspect", "pkg/fetch.py 的 process_retry_policy 是怎么决定要不要重试的？"),
    "K1b": ("inspect", "把 pkg/fetch.py 完整读一遍，用两句话说明这个文件做什么。"),
    "K2a": ("search", "仓库里哪些文件定义了 process_retry_policy？只列文件名。"),
    "K2b": ("search", "找出所有提到 process_retry_policy 的地方，说明各自在哪个函数里。"),
    "K3": ("edit", "把 pkg/util.py 里 greet() 的返回值改成 'hi'。"),
}

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/fetch.py": (
        '"""Fetching helpers."""\n\n'
        "MAX_ATTEMPTS = 3\n\n\n"
        "def process_retry_policy(status_code, attempt):\n"
        '    """429 且还没有到第 3 次尝试时才重试。"""\n'
        "    if status_code == 429 and attempt < MAX_ATTEMPTS:\n"
        '        return "retry"\n'
        '    return "give up"\n'
    ),
    "pkg/store.py": (
        '"""Storage helpers."""\n\n\n'
        "def process_retry_policy(status_code, attempt):\n"
        "    if status_code in (429, 503):\n"
        '        return "retry"\n'
        '    return "give up"\n'
    ),
    "pkg/util.py": (
        '"""Small utilities."""\n\n\n'
        "def greet() -> str:\n"
        '    return "hello"\n'
    ),
}


def shell_substitutions(calls: list[tuple[str, dict]]) -> list[str]:
    """Bash commands a sanctioned tool call could have replaced instead."""
    found = []
    for name, params in calls:
        if name != "bash":
            continue
        command = str(params.get("command", ""))
        for segment in COMMAND_SEGMENTS.split(command):
            if SHELL_FIRST_WORD.match(segment.strip()):
                found.append(command)
                break
    return found


def clean_run(kind: str, calls: list[tuple[str, dict]]) -> bool:
    """Whether the run never took the cheap shell route for its kind."""
    if shell_substitutions(calls):
        return False
    if kind == "edit":
        mutators = [name for name, _ in calls if name in {"edit", "write"}]
        return bool(mutators) and mutators[0] == "edit"
    return True


class _Collector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def on_event(self, event: object) -> None:
        if isinstance(event, ToolStarted):
            self.calls.append((event.tool_name, dict(event.params)))


def _write_fixture(ws: pathlib.Path) -> None:
    for rel, text in FIXTURE.items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


async def run_once(
    model: str, arm: str, task: str, run_index: int, timeout: float
) -> dict:
    import ai_coding.ai.prompt as prompt_mod

    kind, prompt = TASKS[task]
    ws = pathlib.Path(tempfile.mkdtemp(prefix="guidance-probe-"))
    _write_fixture(ws)
    collector = _Collector()
    real_render = prompt_mod._render_methods
    if arm == "old":
        prompt_mod._render_methods = lambda tools, _text=OLD_METHODS: _text
    elif arm == "extreme":
        prompt_mod._render_methods = lambda tools, _text=EXTREME_METHODS: _text
    error: str | None = None
    iterations = 0
    try:
        paths = Paths.for_workspace(ws)
        app_cfg = ConfigLoader.load_layers(paths, str(CONFIG))
        cfg = app_cfg.model_copy(update={"default_model": model})
        engine = build_engine_from_config(cfg)
        registry = _build_tool_registry(cfg, str(ws), None)
        sessions = SessionService(paths.sessions)
        loop = _build_loop(
            engine, sessions, registry, cfg, str(ws), None, paths=paths,
            observer=collector,
        )
        try:
            session = sessions.create()
            await asyncio.wait_for(loop.process_input(session, prompt), timeout)
            outcome = loop.last_outcome
            iterations = int(getattr(outcome, "iterations", 0) or 0)
        finally:
            await loop.aclose()
    except TimeoutError:
        error = "timeout"
    except Exception as exc:  # noqa: BLE001 -- a probe records and moves on
        error = f"{type(exc).__name__}: {exc}"
    finally:
        prompt_mod._render_methods = real_render
        shutil.rmtree(ws, ignore_errors=True)
    calls = collector.calls
    return {
        "model": model,
        "arm": arm,
        "task": task,
        "run": run_index,
        "kind": kind,
        "ok": error is None,
        "error": error,
        "iterations": iterations,
        "first_tool": calls[0][0] if calls else "",
        "sequence": [name for name, _ in calls],
        "clean": clean_run(kind, calls),
        "shell": shell_substitutions(calls),
        "commands": [
            str(params.get("command", "")) for name, params in calls if name == "bash"
        ],
    }


def self_check() -> None:
    """S1/S2: the three arm texts differ; the classifier anchors on first word."""
    from ai_coding.ai.prompt import _render_methods as real

    texts = {"old": OLD_METHODS, "new": real(None), "extreme": EXTREME_METHODS}
    assert len(set(texts.values())) == 3, "arm texts must differ"
    assert len(OLD_METHODS) == 485, len(OLD_METHODS)
    samples = {
        ("cat pkg/fetch.py",): 1,
        ("head -20 pkg/fetch.py",): 1,
        ("cd /tmp/ws && cat -n pkg/fetch.py",): 1,
        ("ls /tmp/ws; find /tmp/ws -name fetch.py",): 1,
        ("cd /x && ls -la",): 0,
        ("npm test 2>&1 | head -20",): 0,
        ("grep -rn process_retry_policy .",): 1,
        ("pytest -q | grep FAIL",): 0,
        ("python - <<'PY'\nPY",): 0,
    }
    for (command,), expected in samples.items():
        got = len(shell_substitutions([("bash", {"command": command})]))
        assert got == expected, f"{command!r}: {got} != {expected}"
    print(
        "self-check ok: "
        + ", ".join(f"{name}={len(text)}" for name, text in texts.items())
        + " chars"
    )


def summarize(results: list[dict]) -> str:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in results:
        groups[(row["model"], row["task"], row["arm"])].append(row)
    lines = []
    for model in dict.fromkeys(row["model"] for row in results):
        for task in dict.fromkeys(row["task"] for row in results if row["model"] == model):
            cells = []
            for arm in ("old", "new", "extreme"):
                rows = groups.get((model, task, arm), [])
                if not rows:
                    continue
                ok = [row for row in rows if row["ok"]]
                clean = sum(1 for row in ok if row["clean"])
                shells = sum(len(row["shell"]) for row in ok)
                cells.append(f"{arm}: clean {clean}/{len(ok)} shell {shells}")
            if cells:
                lines.append(f"  {model:<14} {task:<4} " + " | ".join(cells))
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="qwen,deepseek-chat")
    parser.add_argument("--runs", type=int, default=4, help="per arm per task")
    parser.add_argument("--tasks", default=",".join(TASKS))
    parser.add_argument("--json", default="", help="write full detail here")
    parser.add_argument("--smoke", action="store_true", help="1 task x 2 arms x 1 run")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    self_check()
    models = args.models.split(",")
    tasks = args.tasks.split(",")
    runs = args.runs
    if args.smoke:
        models = models[:1]
        tasks = ["K1a"]
        runs = 1

    started = time.time()
    results: list[dict] = []
    for model in models:
        for task in tasks:
            # Arms alternate within each task block; a fresh workspace per run
            # keeps memory harvest from bleeding between runs.
            for i in range(runs * 2):
                arm = "old" if i % 2 == 0 else "new"
                row = await run_once(model, arm, task, i, args.timeout)
                results.append(row)
                print(
                    f"{model:<14} {task:<4} {arm:<7} run{i}: "
                    f"clean={row['clean']} first={row['first_tool'] or '-'} "
                    f"shell={len(row['shell'])} iters={row['iterations']}"
                    + (f" ERROR {row['error']}" if row["error"] else ""),
                    flush=True,
                )
        if not args.smoke:
            # Positive control: the instrument must be able to see a large
            # prompt effect before a null old-vs-new result may be recorded.
            for i in range(4):
                row = await run_once(model, "extreme", "K1a", i, args.timeout)
                results.append(row)
                print(
                    f"{model:<14} K1a  extreme run{i}: "
                    f"clean={row['clean']} first={row['first_tool'] or '-'} "
                    f"shell={len(row['shell'])}",
                    flush=True,
                )

    errored = sum(1 for row in results if not row["ok"])
    print(f"\n{len(results)} runs in {time.time() - started:.0f}s, {errored} errored")
    print(summarize(results))
    if args.json:
        target = pathlib.Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "runs": runs,
                    "models": models,
                    "arms": {
                        "old": OLD_METHODS,
                        "extreme": EXTREME_METHODS,
                    },
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"detail written to {target}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    sys.exit(0)
