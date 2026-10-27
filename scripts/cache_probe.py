#!/usr/bin/env python3
"""Prompt-cache hit-rate probe — online, costs a few thousand tokens, exit 0 always.

Both configured providers (DashScope/Qwen and DeepSeek) cache the request prefix
automatically and report the hits in ``usage``.  No ``cache_control``, no
``prompt_cache_key``: the question this script answers is whether that is good
enough to skip designing explicit breakpoints.

    .venv/bin/python scripts/cache_probe.py                    # both models
    .venv/bin/python scripts/cache_probe.py --model qwen --steps 8
    .venv/bin/python scripts/cache_probe.py --mode churn        # see below

Four modes, each measured with the project's own engine so the request shape is
the real one:

``stable``   system prompt fixed, history growing.  What a well-behaved session
             looks like; this is the number to compare against.
``churn``    the same, but the *system prompt* changes every step.  This is the
             trap: instructions sit at the very front of the request, so anything
             volatile in them invalidates the whole prefix.  Measured on
             DeepSeek it took the hit rate from 79% to 17%.
``loop``     the real thing: a real AgentLoop, real tools, real engine, four
             turns.  Use this one after touching prompt assembly.
``diff``     no calls at all — it prints whether two consecutive requests differ
             in their stable prefix (instructions, tool payload, message head).

**When to re-run:** after changing what goes into the system prompt, the tool
surface, or the volatile tail (``core/request_builder.build_tail``).  The tail is
supposed to be appended at the *end* of the request precisely so the prefix stays
byte-identical; if it moves back into the system prompt, this script is how you
find out.  Numbers from the R11 round are in docs/plan-pipeline-analysis.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from ai_coding.ai.openai_engine import OpenAIChatEngine  # noqa: E402
from ai_coding.config.models import ModelConfig  # noqa: E402
from ai_coding.domain.message import ChatMessage  # noqa: E402

CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config.local.yaml"


def _model_config(models: dict, name: str) -> ModelConfig:
    # Through `_coerce_model` (M-H2): it fills `base_url` from `provider` and
    # resolves a `${VAR}` key.  Building `ModelConfig` by hand here would send
    # the literal "${DEEPSEEK_API_KEY}" as the key.
    from ai_coding.config.models import _coerce_model

    coerced = _coerce_model(models[name])
    return replace(coerced, max_tokens=128, temperature=0.0, streaming=True)


def _tools(count: int = 12) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": f"tool_{i}", "description": f"工具 {i}。" * 12,
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "路径。" * 6}},
                "required": ["path"]}}}
        for i in range(count)
    ]


def _report(rows: list[tuple[int, int]]) -> tuple[int, int, float]:
    prompt = sum(p for p, _ in rows)
    cached = sum(c for _, c in rows)
    return prompt, cached, (100 * cached / prompt if prompt else 0.0)


def _print(model: str, rows: list[tuple[int, int]]) -> float:
    prompt, cached, hit = _report(rows)
    print(f"  {model:<14} prompt={prompt:>7} cached={cached:>7} → 命中 {hit:5.1f}%")
    per_step = " ".join(f"{100 * c / p:.0f}%" if p else "-" for p, c in rows)
    print(f"                 逐 step: {per_step}")
    return hit


async def _probe(engine: OpenAIChatEngine, steps: int, churn: bool) -> list[tuple[int, int]]:
    tools = _tools()
    instructions = "你是一个编码助手。" + ("遵守项目约定。" * 200)
    history = [ChatMessage(role="user", content="帮我看看 loader 的实现", timestamp="")]
    rows: list[tuple[int, int]] = []
    for step in range(1, steps + 1):
        prompt = instructions
        if churn:
            # The trap: volatile state inside the system prompt.
            prompt += f"\n<task-state> 目标：第 {step} 轮\n- [x] 已做 {step}\n</task-state>"
        result = await engine.step(list(history), tools=tools, instructions=prompt)
        usage = result.usage or {}
        rows.append((int(usage.get("prompt_tokens", 0) or 0),
                     int(usage.get("cached_tokens", 0) or 0)))
        history.append(ChatMessage(role="assistant", content=f"第 {step} 步回答。" * 40,
                                   timestamp=""))
        history.append(ChatMessage(role="user", content=f"继续，第 {step} 轮。" * 30,
                                   timestamp=""))
    return rows


async def _loop_probe(models: dict, name: str, turns: int) -> float:
    """The real thing: a real AgentLoop with real tools and the real engine."""
    import tempfile

    from ai_coding.ai.openai_engine import OpenAIChatEngine as Engine
    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.security.approval import AutoApproveCallback
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.builder import build_tool_registry

    workspace = pathlib.Path(tempfile.mkdtemp()) / "ws"
    workspace.mkdir(parents=True)
    (workspace / "d.txt").write_text("".join(f"l{i}\n" for i in range(50)))
    root = workspace.parent
    engine = Engine(_model_config(models, name))
    loop = AgentLoop(
        engine, SessionService(root=root / "sessions"),
        build_tool_registry(AppConfig(), workspace=str(workspace), include_subagent=False),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=4),
        approval=AutoApproveCallback(), workspace=str(workspace),
    )
    session = SessionService(root=root / "s2").create(title="cache-probe")
    rows: list[tuple[int, int]] = []
    prompts = [
        "用 bash 跑 `wc -l d.txt`，告诉我行数。",
        "再跑一次同样的命令，确认结果一致。",
        "再读一次 d.txt 的第一行。",
        "最后总结一下刚才三次的结果。",
    ][:turns]
    try:
        for text in prompts:
            await loop.process_input(session, text)
            usage = loop.last_outcome.usage or {}
            rows.append((int(usage.get("prompt_tokens", 0) or 0),
                         int(usage.get("cached_tokens", 0) or 0)))
            print(f"    回合 {len(rows)}: prompt={rows[-1][0]:>6} "
                  f"cached={rows[-1][1]:>6} "
                  f"iters={loop.last_outcome.iterations}")
    finally:
        await loop.aclose()
    return _print(f"{name}/loop", rows)


async def _diff(models: dict, name: str) -> int:
    """Report whether two consecutive requests keep a stable prefix. No calls.

    The invariant that matters: the system prompt and the tool payload must be
    byte-identical across requests, and the message list must *extend* rather
    than change.  It does not have to grow at the front — the volatile tail
    (task-state, memory, hook context) is supposed to be appended at the end, so
    the shared leading messages can be as short as the first user turn.
    """
    import tempfile

    from ai_coding.config.models import AIConfig, AppConfig
    from ai_coding.core.agent_loop import AgentLoop
    from ai_coding.service.session_service import SessionService
    from ai_coding.tools.builder import build_tool_registry

    captured: list[tuple[list[dict], str, str]] = []
    original_build = OpenAIChatEngine.build_request_messages
    original_step = OpenAIChatEngine.step

    async def spy_step(self, messages, *, tools, instructions=None, on_delta=None):
        built = original_build(messages, instructions)
        captured.append((
            [dict(m) for m in built],
            instructions or "",
            json.dumps(tools, sort_keys=True, ensure_ascii=False),
        ))
        return await original_step(self, messages, tools=tools,
                                   instructions=instructions, on_delta=on_delta)

    OpenAIChatEngine.step = spy_step
    try:
        workspace = pathlib.Path(tempfile.mkdtemp())
        engine = OpenAIChatEngine(_model_config(models, name))
        loop = AgentLoop(
            engine, SessionService(root=workspace / "s"),
            build_tool_registry(AppConfig(), workspace=str(workspace), include_subagent=False),
            settings=AIConfig(stop_policy="natural", max_tool_iterations=2),
            workspace=str(workspace),
        )
        session = SessionService(root=workspace / "s2").create(title="diff")
        await loop.process_input(session, "第一句")
        await loop.process_input(session, "第二句")
        await loop.aclose()
    finally:
        OpenAIChatEngine.step = original_step

    first, last = captured[0], captured[-1]
    shared = 0
    for left, right in zip(first[0], last[0], strict=False):
        if left != right:
            break
        shared += 1
    grew = len(last[0]) > len(first[0])
    print(f"  {name}: 请求 {len(captured)} 次（每回合 {max(1, len(captured) // 2)} 个模型步）")
    print(f"    system prompt 相同:     {first[1] == last[1]} (长度 {len(first[1])})")
    print(f"    tools 载荷相同:         {first[2] == last[2]} ({len(first[2])} 字符)")
    print(f"    消息条数:               {len(first[0])} → {len(last[0])}（只增不改: {grew}）")
    print(f"    逐条相同的前导消息:     {shared} 条")
    print("    —— 前缀稳定的三件事：system prompt 不变、tools 不变、历史只往后长")
    print("       （尾部注入出现在末尾是设计如此，不是前缀破裂）")
    stable = first[1] == last[1] and first[2] == last[2] and grew
    return 0 if stable else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="", help="只跑一个模型（默认全部）")
    parser.add_argument("--steps", type=int, default=6)
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--mode", default="all",
                        choices=["all", "stable", "churn", "loop", "diff"])
    args = parser.parse_args()

    if not CONFIG.is_file():
        print(f"[cache_probe] 缺少 {CONFIG.name}，无法读取模型配置")
        return 2
    models = (yaml.safe_load(CONFIG.read_text()) or {}).get("models") or {}
    names = [args.model] if args.model else list(models)
    unknown = [n for n in names if n not in models]
    if unknown:
        print(f"[cache_probe] 配置里没有这些模型: {', '.join(unknown)}")
        return 2

    for name in names:
        print(f"\n### {name}")
        cfg = _model_config(models, name)
        if args.mode in ("all", "stable", "churn"):
            engine = OpenAIChatEngine(cfg)
            try:
                if args.mode in ("all", "stable"):
                    await engine.step  # noqa: B018 — keep the import honest
                    _print(f"{name}/stable", await _probe(engine, args.steps, churn=False))
                if args.mode in ("all", "churn"):
                    _print(f"{name}/churn", await _probe(engine, args.steps, churn=True))
            finally:
                await engine.aclose()
        if args.mode in ("all", "loop"):
            await _loop_probe(models, name, args.turns)
        if args.mode in ("all", "diff"):
            await _diff(models, name)

    print("\n判读：stable 60%+ 且 loop 与 stable 同量级 → 自动前缀缓存够用，不需要显式断点。")
    print("      churn 掉到 20% 以下 → 易变内容跑进了 system prompt，先修装配再谈断点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
