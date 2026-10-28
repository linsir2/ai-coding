#!/usr/bin/env python3
"""R20 §7: re-run T2 against the fixed loop, same question, same model.

T2 is the question whose trace produced the root-cause report
(``docs/r20-t2-rootcause.md``):

    在 ai_coding/ 下找出一个会导致运行时异常（不是风格问题、不是类型注解问题）
    的真实缺陷，给出文件路径、行号、触发条件与修复方式。只报一个，但要讲透。

The run is headless through the real ``AgentLoop``, and the result file has the
same shape as the baseline (``eval-workspace/i2c-T2/t2-result.json``) so the two
can be compared field by field:

    .venv/bin/python scripts/eval_t2_r20.py --out /workspace/eval-workspace/r20-T2

Then:

    .venv/bin/python scripts/eval_claims.py \
        --session <out>/sessions/<id>.json --answer <out>/t2-result.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
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
from ai_coding.service.session_service import SessionService  # noqa: E402

TASK = (
    "在 ai_coding/ 下找出一个会导致运行时异常（不是风格问题、不是类型注解问题）"
    "的真实缺陷，给出文件路径、行号、触发条件与修复方式。只报一个，但要讲透。"
)

#: The baseline run's knobs (docs/r20-t2-rootcause.md §0): a 40-iteration budget
#: and the I2 compression settings the root cause was measured under.
OVERRIDES = {
    "max_tool_iterations": 40,
    "snip_keep_tail": 30,
    "keep_recent_tool_results": 8,
    "l2_window_chars": 1200,
    "l2_window_budget": 12_000,
    "summary_delta_tokens": 4000,
}


def _config(config_path: Path):
    """The local config with the baseline's orchestration knobs.

    The knobs are set on the loaded object rather than written back to a YAML:
    a generated file would not be in ``DEFAULT_NAMES``, so the loader would not
    find it and the run would start on empty defaults.
    """
    loaded = ConfigLoader.load_app(str(config_path))
    for key, value in OVERRIDES.items():
        setattr(loaded.ai, key, value)
    return loaded


async def run(out_dir: Path, config_path: Path) -> dict:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    (out_dir / ".git").mkdir()  # repo-ish root so worktree isolation can work

    cfg = _config(config_path)
    # The trace is what makes the run auditable: it captures the request the
    # model actually saw, so the <read-ledger> block can be checked rather than
    # inferred from the persisted session (which never holds tail blocks).
    if getattr(cfg, "tracing", None) is not None:
        cfg.tracing.enabled = True
    workspace = str(ROOT)
    sessions = SessionService(out_dir / "sessions")
    registry = _build_tool_registry(cfg, workspace, None)
    session = sessions.create()
    loop = _build_loop(
        build_engine_from_config(cfg),
        sessions,
        registry,
        cfg,
        workspace,
        None,
        trace=_make_recorder(cfg, workspace, session),
    )
    console = DialogueConsole()

    started = time.monotonic()
    action, session = await handle_input(loop, TASK, session, sessions, console)
    seconds = round(time.monotonic() - started, 1)

    answer = loop.last_outcome.text or ""
    tool_names = [m.tool_name for m in session.messages if m.role == "tool"]
    result = {
        "seconds": seconds,
        "messages": len(session.messages),
        "tool_calls": len(tool_names),
        "tool_seq": tool_names,
        "answer": answer,
        "session_id": session.session_id,
        "checkpoint_consumed": int(
            getattr(session, "compaction_consumed", 0) or 0
        ),
        "checkpoint_chars": len(
            getattr(session, "compaction_checkpoint", "") or ""
        ),
        "action": str(action),
        "reason": loop.last_outcome.reason,
    }
    (out_dir / "t2-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(f"seconds={seconds} tool_calls={len(tool_names)} action={action}")
    print(f"reason={loop.last_outcome.reason}")
    print(f"answer_chars={len(answer)}")
    print(f"session={out_dir / 'sessions' / (session.session_id + '.json')}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/workspace/eval-workspace/r20-T2"))
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config.local.yaml"
    )
    args = parser.parse_args()
    asyncio.run(run(args.out, args.config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
