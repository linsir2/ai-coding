#!/usr/bin/env python3
"""eval-06: same-context comparison -- ai-coding vs the evaluator's own run.

Runs a fixed set of layered tasks through the real AgentLoop headlessly, records
what came out (answer text, tool sequence, wall time, message count), so the same
questions can be answered by the evaluator under the same context and compared.

    .venv/bin/python scripts/eval_comparison.py --config config.local.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.factory import build_engine_from_config  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_registry  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.infra.logger_setup import setup_logging  # noqa: E402
from ai_coding.service.session_service import SessionService  # noqa: E402

TASKS = [
    (
        "T1-read",
        "阅读 ai_coding/core/compaction.py 与 ai_coding/core/context_compression.py，"
        "说明四层上下文压缩（L1/L2/L3/L4）各自的触发条件与执行顺序，"
        "并指出哪一层在什么情况下会完全不生效、为什么。",
    ),
    (
        "T2-bug",
        "在 ai_coding/ 下找出一个会导致运行时异常（不是风格问题、不是类型注解问题）的真实缺陷，"
        "给出文件路径、行号、触发条件与修复方式。只报一个，但要讲透。",
    ),
    (
        "T3-design",
        "如果要给本项目加一套「自动重试阶梯」（对标 Codex CLI 的 retry_delay："
        "哪些错误绝不重试、哪些只按服务端建议、哪些走指数退避），"
        "你会把它加在哪一层、为什么？请结合本项目的 turn_policy / agent_loop 结构说明，"
        "并指出与现有 stop_policy、turn_timeout_seconds 的关系。",
    ),
]


async def main(config_path: str, repo: Path) -> int:
    app_cfg = ConfigLoader.load_app(config_path)
    app_cfg.ai.max_tool_iterations = 40
    app_cfg.ai.turn_timeout_seconds = 900
    results = []
    for name, prompt in TASKS:
        with tempfile.TemporaryDirectory(prefix="cmp_") as tmp:
            ws = Path(tmp)
            sessions = SessionService(ws / "sessions")
            registry = _build_tool_registry(app_cfg, str(repo), None)
            loop = _build_loop(
                build_engine_from_config(app_cfg), sessions, registry,
                app_cfg, str(repo), None,
            )
            session = sessions.create()
            t0 = time.monotonic()
            answer = await loop.process_input(session, prompt)
            dt = time.monotonic() - t0
            tools = [m.tool_name for m in session.messages if m.role == "tool"]
            rec = {
                "task": name,
                "prompt": prompt,
                "seconds": round(dt, 1),
                "messages": len(session.messages),
                "tool_calls": len(tools),
                "tool_seq": tools,
                "answer": answer or "",
            }
            results.append(rec)
            print(f"\n{'='*66}\n[{name}] {dt:.1f}s  tools={len(tools)}  msgs={len(session.messages)}")
            print(f"tool_seq={tools}")
            print(f"answer({len(answer or '')} chars):\n{(answer or '')[:1400]}")
            await loop.aclose()
    out = Path("/workspace/eval-workspace/comparison_ai_coding.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {out}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.local.yaml"))
    parser.add_argument("--repo", default=str(ROOT))
    args = parser.parse_args()
    setup_logging()
    raise SystemExit(asyncio.run(main(args.config, Path(args.repo))))
