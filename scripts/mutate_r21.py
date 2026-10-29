"""RIPR "P" (prove) for R21: every new test must be load-bearing.

Batch 0 is the bad-``tool_calls[].arguments`` fix (docs/plan-r21-six-fixes-and-
longtask.md §10).  It has four layers, and each one below is deleted or
inverted in turn; a mutation that survives means the suite does not actually
pin that layer.

Usage:  python scripts/mutate_r21.py [name-substring]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"

#: (name, file, old, new, tests that must go red)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "L1  the wire stops repairing the arguments",
        "ai_coding/ai/mapping.py",
        '        "function": {"name": ref.name, "arguments": normalize_arguments(ref.arguments)},',
        '        "function": {"name": ref.name, "arguments": ref.arguments or "{}"},',
        "tests/test_tool_call_arguments.py",
    ),
    (
        "L2  any JSON parses as a call",
        "ai_coding/domain/message.py",
        "    return isinstance(parsed, dict)",
        "    return True",
        "tests/test_tool_call_arguments.py",
    ),
    (
        "L3  normalize passes the broken string through",
        "ai_coding/domain/message.py",
        '    return raw if arguments_are_valid(raw) else "{}"',
        "    return raw",
        "tests/test_tool_call_arguments.py",
    ),
    (
        "L4  the dispatcher runs a bad call with {}",
        "ai_coding/core/dispatch.py",
        "        elif not arguments_are_valid(ref.arguments):",
        "        elif False:",
        "tests/test_tool_call_arguments.py tests/test_tool_dispatch.py",
    ),
    (
        "L5  a truncated step is executed anyway",
        "ai_coding/core/dispatch.py",
        "        if truncated:",
        "        if False:",
        "tests/test_tool_call_arguments.py",
    ),
    (
        "L6  the loop stops telling the dispatcher about truncation",
        "ai_coding/core/agent_loop.py",
        '            truncated=step.finish_reason == "length",',
        "            truncated=False,",
        "tests/test_tool_call_arguments.py",
    ),
    (
        "L7  the 400 goes back to being fatal",
        "ai_coding/ai/openai_engine.py",
        "        if status in (400, 422) and _looks_like_bad_arguments(exc):\n"
        "            return FailureKind.INVALID_ARGUMENTS",
        "        if False:\n"
        "            return FailureKind.INVALID_ARGUMENTS",
        "tests/test_tool_call_arguments.py",
    ),
    (
        "L8  the engine re-sends the same body",
        "ai_coding/ai/openai_engine.py",
        "                if kind is FailureKind.INVALID_ARGUMENTS and not rebuilt:",
        "                if False:",
        "tests/test_tool_call_arguments.py",
    ),
    (
        "P1  the headline goes back to counting only attested steps",
        "ai_coding/core/plan_runner.py",
        '        head = f"[plan] 完成 {len(self.completed)} 步{qualifier}"',
        '        done = sum(1 for entry in self.deliveries if entry.attested)\n'
        '        head = f"[plan] 完成 {done} 步{qualifier}"',
        "tests/test_plan_attestation.py",
    ),
    (
        "P2  the provenance qualifier disappears",
        "ai_coding/core/plan_runner.py",
        '        qualifier = f"（其中 {unattested} 步未经分步验收）" if unattested else ""',
        '        qualifier = ""',
        "tests/test_plan_attestation.py",
    ),
    (
        "P3  a single failure calls itself a repeat again",
        "ai_coding/domain/ledger.py",
        '        what = "调用失败" if count == 1 else "反复失败"',
        '        what = "反复失败"',
        "tests/test_ledger.py",
    ),
    (
        "P4  the plan seed says 目标 again",
        "ai_coding/core/plan_runner.py",
        'lines = [f"[计划标题] {task.goal or \'(未命名计划)\'}", "", "[计划步骤]"]',
        'lines = [f"[计划目标] {task.goal or \'(未命名计划)\'}", "", "[计划步骤]"]',
        "tests/test_plan_m1.py",
    ),
    (
        "S1  a plan round advertises the goal pair again",
        "ai_coding/core/agent_loop.py",
        '        if armed == "plan":',
        "        if False:",
        "tests/test_surface_tools_armed.py",
    ),
    (
        "S2  the surface cache forgets the arm",
        "ai_coding/core/agent_loop.py",
        "        key = (mode, readonly, session_rules, agent_rules, _armed_profile(session))",
        '        key = (mode, readonly, session_rules, agent_rules, "")',
        "tests/test_surface_tools_armed.py",
    ),
    (
        "A1  the wire writes a null readonly key",
        "ai_coding/server/bridge.py",
        "        if event.readonly is not None:",
        "        if True:",
        "tests/test_m1b_agent_history.py",
    ),
    (
        "A2  a readonly-only flip announces nothing",
        "ai_coding/core/agent_loop.py",
        "        if agent_moved or readonly_moved:",
        "        if agent_moved:",
        "tests/test_posture_result.py",
    ),
    (
        "A3  a readonly-only flip records a history row",
        "ai_coding/core/agent_loop.py",
        "            if agent_moved:\n"
        "                session.agent_history.append(",
        "            if True:\n"
        "                session.agent_history.append(",
        "tests/test_posture_result.py",
    ),
    (
        "A4  the reducer ignores the payload's readonly axis",
        "ai_coding/frontend/src/state/session.ts",
        '      const readonly =\n'
        '        typeof data["readonly"] === "boolean"'
        ' ? (data["readonly"] as boolean) : state.readonly',
        "      const readonly = state.readonly",
        "test/agent-picker.test.ts",
    ),
    (
        "A5  a readonly-only flip draws a Switched-agent notice",
        "ai_coding/frontend/src/state/session.ts",
        "      const switched =\n        from === to\n          ? state",
        "      const switched =\n        false\n          ? state",
        "test/agent-picker.test.ts",
    ),
    (
        "Q1  the quote check reads nothing back",
        "ai_coding/core/agent_loop.py",
        "        bad_quotes = (\n"
        "            []\n"
        "            if bad\n"
        "            else unsupported_quotes(\n"
        "                text, session.read_ledger, self._read_for_quote\n"
        "            )\n"
        "        )",
        "        bad_quotes = []",
        "tests/test_claim_check.py",
    ),
    (
        "Q2  a missing file becomes evidence",
        "ai_coding/core/agent_loop.py",
        "            if not candidate.is_file():\n"
        "                return None",
        "            if not candidate.is_file():\n"
        '                return "nowhere\\nnowhere\\nnowhere\\n"',
        "tests/test_claim_check.py",
    ),
    (
        "Q3  the paraphrase allowance is lifted",
        "ai_coding/domain/read_ledger.py",
        "    if len(quote.split()) < 10:\n        return False",
        "    if False:\n        return False",
        "tests/test_read_ledger.py",
    ),
    (
        "Q4  the range check stops at the file's end",
        "ai_coding/domain/read_ledger.py",
        "        if start < 1 or end > len(lines):\n            continue",
        "        if start < 1:\n            continue",
        "tests/test_read_ledger.py",
    ),
    (
        "Q5  the line-number prefix stops being stripped",
        "ai_coding/domain/read_ledger.py",
        '    text = re.sub(r"^\\s*\\d+\\s{2}", "", text, flags=re.MULTILINE)',
        "    pass",
        "tests/test_read_ledger.py",
    ),
    (
        "Q6  the ledger spelling stops resolving",
        "ai_coding/domain/read_ledger.py",
        "        resolved = rows[0].path if rows else path",
        "        resolved = path",
        "tests/test_read_ledger.py",
    ),
]


def run_tests(targets: str) -> bool:
    """True when the suite is green.

    A ``tests/...`` target runs pytest; a frontend target (``test/...``) runs
    bun from the frontend package, so one script covers both halves.
    """
    items = targets.split()
    if items and items[0].startswith("tests/"):
        command = [str(PY), "-m", "pytest", *items, "-x", "-q", "--no-header"]
        cwd = ROOT
    else:
        command = ["bun", "test", *items]
        cwd = ROOT / "ai_coding" / "frontend"
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else ""
    survived: list[str] = []
    chosen = [m for m in MUTATIONS if wanted.lower() in m[0].lower()]
    for name, relpath, old, new, targets in chosen:
        path = ROOT / relpath
        original = path.read_text()
        if old not in original:
            print(f"SKIP      {name}: anchor not found")
            survived.append(f"{name} (anchor drift)")
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            green = run_tests(targets)
        finally:
            path.write_text(original)
        status = "SURVIVED" if green else "killed"
        print(f"{status:9} {name}")
        if green:
            survived.append(name)
    print()
    if survived:
        print(f"{len(survived)} mutation(s) survived:")
        for name in survived:
            print(f"  - {name}")
        return 1
    print(f"all {len(chosen)} mutations killed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
