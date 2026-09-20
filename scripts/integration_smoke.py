#!/usr/bin/env python3
"""Real-network integration smoke test for ai-coding (M3 wiring).

Builds the full AgentLoop exactly like the CLI (including the real L4
summarizer + memory extractor) against a live DeepSeek endpoint, then runs:

  1. a tool-calling turn  -> asserts an assistant `tool_calls` entry persisted
  2. a follow-up turn      -> asserts history replay does not 400 and recall works
  3. direct L4 + extractor -> asserts the real LLM helpers return summaries/notes

Requires a git-ignored ``config.local.yaml`` (see config.example.yaml + the API
key).  Usage:

    .venv/bin/python scripts/integration_smoke.py [--config config.local.yaml]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_coding.ai.factory import build_service_from_config  # noqa: E402
from ai_coding.cli import _build_loop, _build_tool_stack  # noqa: E402
from ai_coding.config.loader import ConfigLoader  # noqa: E402
from ai_coding.infra.logger_setup import setup_logging  # noqa: E402

setup_logging()
SEP = "=" * 56


async def main(config_path: str) -> int:
    app_cfg = ConfigLoader.load_app(config_path)
    print(f"model     : {app_cfg.default_model}")
    print(f"base_url  : {app_cfg.models[app_cfg.default_model].base_url}")

    with tempfile.TemporaryDirectory(prefix="it_ws_") as ws:
        ws_dir = Path(ws)
        (ws_dir / "sample.txt").write_text(
            "hello world from workspace\n", encoding="utf-8"
        )

        sdk_tools = _build_tool_stack(app_cfg, str(ws_dir), None)
        service = build_service_from_config(app_cfg, tools=sdk_tools)
        sessions = __import__(
            "ai_coding.service.session_service", fromlist=["SessionService"]
        ).SessionService(ws_dir / "sessions")
        loop = _build_loop(service, sessions, app_cfg, str(ws_dir), None)

        session = sessions.create()

        # 1) tool turn
        print(SEP + " [1] tool turn ")
        await loop.process_input(session, "用read工具读取sample.txt文件，告诉我内容")
        roles = [m.role for m in session.messages]
        tool_calls = next(
            (m.tool_calls for m in session.messages if getattr(m, "tool_calls", None)),
            None,
        )
        print(f"roles={roles} tool_msgs={sum(1 for r in roles if r=='tool')}")
        assert "assistant" in roles and "tool" in roles, "tool result not persisted"
        assert tool_calls, "assistant tool_calls not persisted"
        print(f"tool_calls={tool_calls}")

        # 2) follow-up replay
        print(SEP + " [2] follow-up replay ")
        t2 = await loop.process_input(session, "上面读到的内容用一句话总结")
        print(f"replied({len(t2)} chars): {t2[:100]!r}")
        assert t2, "empty follow-up reply"

        # 3) real LLM helpers (flash)
        print(SEP + " [3] L4 summarizer + memory extractor ")
        from ai_coding.ai.llm_helpers import build_extractor, build_summarizer
        from ai_coding.domain.message import ChatMessage

        cfg = service.model_config
        summarizer = build_summarizer(cfg)
        extractor = build_extractor(cfg)
        long_text = "[user] 我们要把配置从 deepseek-chat 迁移到 flash\n" * 40
        summary = await summarizer(long_text)
        msgs = [ChatMessage(role="user", content="我用 Python 写后端，喜欢简洁代码", timestamp="t")]
        notes = await extractor(msgs, None)
        print(f"summary={'OK' if summary else 'EMPTY'} len={len(summary) if summary else 0}")
        print(f"notes   ={notes[:3]}")
        assert summary, "L4 summarizer returned empty"
        assert notes, "memory extractor returned no notes"

    print(SEP + " SMOKE OK ")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default=str(ROOT / "config.local.yaml"), help="YAML config path"
    )
    args = parser.parse_args()
    try:
        code = asyncio.run(main(args.config))
    except AssertionError as exc:  # noqa: PERF203
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        code = 2
    except Exception as exc:  # noqa: BLE001
        print(f"SMOKE ERROR: {exc!r}", file=sys.stderr)
        code = 3
    raise SystemExit(code)
