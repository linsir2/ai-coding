"""RIPR "P" (prove): every R20 test must be load-bearing.

A test that passes with the implementation deleted is not a test, it is
decoration.  Each mutation below deletes or inverts one behaviour the plan
claims, then runs the tests that claim to cover it.  A mutation that survives
means the suite does not actually pin that behaviour.

Usage:  python scripts/mutate_r20.py
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
        "A1  note_read stops recording the range",
        "ai_coding/domain/session.py",
        "        self.read_ledger = merge_mark(\n"
        "            self.read_ledger, ReadMark(key, start, end, total)\n"
        "        )",
        "        pass",
        "tests/test_read_ledger.py",
    ),
    (
        "A7  the ledger stops normalising paths",
        "ai_coding/domain/read_ledger.py",
        '    text = str(path or "").strip().replace("\\\\", "/")\n'
        "    if not text:\n"
        '        return ""\n'
        "    if os.path.isabs(text):",
        '    return str(path or "")\n'
        "    if False:",
        "tests/test_read_ledger.py",
    ),
    (
        "A9  the render stops naming the gaps",
        "ai_coding/domain/read_ledger.py",
        '            tail = "（完整）" if not gaps else f"（缺 {missing}）"',
        '            tail = ""',
        "tests/test_read_ledger.py",
    ),
    (
        "B3  the count rung stops requiring new evidence",
        "ai_coding/core/interventions.py",
        "if snap.search_streak >= limits.search_streak_threshold"
        " and not snap.novel_findings:",
        "if snap.search_streak >= limits.search_streak_threshold:",
        "tests/test_interventions.py",
    ),
    (
        "B5  the hard stop stops being judged before the evidence gate",
        "ai_coding/core/interventions.py",
        "        if limits.search_streak_l3"
        " and snap.search_streak >= limits.search_streak_l3:\n"
        "            return True\n"
        "        if not limits.search_streak_threshold:",
        "        if not limits.search_streak_threshold:",
        "tests/test_interventions.py tests/test_interventions_loop.py",
    ),
    (
        "C1  the read cap is lifted",
        "ai_coding/tools/read_tool.py",
        "        page_end = min(end, offset + self._max_lines)",
        "        page_end = end",
        "tests/test_tool_read.py",
    ),
    (
        "C4  the range is not clamped to the file",
        "ai_coding/tools/read_tool.py",
        "        end = min(offset + limit, len(lines))"
        " if limit is not None else len(lines)",
        "        end = offset + limit if limit is not None else len(lines)",
        "tests/test_tool_read.py",
    ),
    (
        "C5  the explicit-limit suffix is dropped",
        "ai_coding/tools/read_tool.py",
        "        elif offset > 0 or"
        " (limit is not None and end < len(lines)):\n"
        '            output += f"\\n[showing lines'
        ' {offset + 1}-{page_end} of {len(lines)}]"',
        '        elif False:\n'
        '            output += ""',
        "tests/test_tool_read.py",
    ),
    (
        "P1  covers() goes back to intersection",
        "ai_coding/domain/read_ledger.py",
        "        if same_file(mark.path, path) and mark.start <= start"
        " and end <= mark.end:",
        "        if same_file(mark.path, path) and mark.start <= end"
        " and start <= mark.end:",
        "tests/test_read_ledger.py",
    ),
    (
        "P4  tier 2 stops checking the end of the file",
        "ai_coding/domain/read_ledger.py",
        "            if end > total",
        "            if False",
        "tests/test_read_ledger.py",
    ),
    (
        "P4b tier 2 measures the cited spelling, not the seen one",
        "ai_coding/domain/read_ledger.py",
        "            total = size_of(resolved)",
        "            total = size_of(path)",
        "tests/test_read_ledger.py",
    ),
    (
        "P5  tier 2 checks start instead of end",
        "ai_coding/domain/read_ledger.py",
        "            if end > total",
        "            if start > total",
        "tests/test_read_ledger.py",
    ),
    (
        "D3  covers() stops checking the range",
        "ai_coding/domain/read_ledger.py",
        "        if same_file(mark.path, path) and mark.start <= start"
        " and end <= mark.end:\n"
        "            return True\n"
        "    return False",
        "        return True\n"
        "    return False",
        "tests/test_read_ledger.py tests/test_claim_check.py",
    ),
    (
        "D7  tier 2 stops protecting grep-only citations",
        "ai_coding/domain/read_ledger.py",
        "    if any(same_file(seen_path, path) for seen_path in seen):\n"
        "        if size_of is None:",
        "    if False:\n"
        "        if size_of is None:",
        "tests/test_read_ledger.py tests/test_claim_check.py",
    ),
    (
        "D5  the ambiguity guard is removed",
        "ai_coding/domain/read_ledger.py",
        "    if names_more_than_one_file("
        "{normalize(path) for path, _, _ in claims}):\n"
        "        return []",
        "    if False:\n"
        "        return []",
        "tests/test_read_ledger.py tests/test_claim_check.py",
    ),
    (
        "D14 the ambiguity guard counts two spellings as two files",
        "ai_coding/domain/read_ledger.py",
        "    names = list(paths)\n"
        "    first = names.pop()\n"
        "    return any(not same_file(first, other) for other in names)",
        "    return len(paths) > 1",
        "tests/test_read_ledger.py",
    ),
    (
        "D9  the shorthand citation forms stop being recognised",
        "ai_coding/domain/read_ledger.py",
        "[Ll](?:ine)?\\s*(?P<estart>\\d+)",
        "[Zz](?:ine)?\\s*(?P<estart>\\d+)",
        "tests/test_read_ledger.py",
    ),
    (
        "D10 the correction stops being capped at one",
        "ai_coding/core/agent_loop.py",
        "        if session.claim_retries >= 1:\n"
        "            return False\n"
        "        claims = extract_claims(text)",
        "        claims = extract_claims(text)",
        "tests/test_claim_check.py",
    ),
    (
        "D11 the check stops running after the proofs",
        "ai_coding/core/agent_loop.py",
        "                    if self._has_a_round_left(index)"
        " and self._reject_unsupported_claims(\n"
        '                        session, step.text or "", index\n'
        "                    ):",
        "                    if False:",
        "tests/test_claim_check.py",
    ),
    (
        "D12 the citation rule leaves the prompt",
        "ai_coding/ai/prompt.py",
        '    "session: cite `path:line`.'
        ' If you have not read it, say \\"unconfirmed\\" "',
        '    "session: "',
        "tests/test_prompt.py",
    ),
    (
        "D13 the budget block stops offering an exit",
        "ai_coding/core/turn_policy.py",
        '                "证据不足的部分直接写「未确认」并说明缺什么，不要补全。",\n',
        "",
        "tests/test_turn_policy.py",
    ),
    (
        "A10 seen_paths stops accumulating",
        "ai_coding/core/agent_loop.py",
        "        session.note_seen(\n"
        "            path\n"
        "            for item in outcomes\n"
        "            if item.result.success and item.call.name != \"read\"\n"
        '            for path in paths_in(item.result.output or "",'
        " self.workspace)\n"
        "        )",
        "        pass",
        "tests/test_claim_check.py",
    ),
    (
        "A11 read content stops being excluded from seen_paths",
        "ai_coding/core/agent_loop.py",
        '            if item.result.success and item.call.name != "read"\n',
        "            if item.result.success\n",
        "tests/test_claim_check.py",
    ),
    (
        "C6  a zero limit stops saying so",
        "ai_coding/tools/read_tool.py",
        "        if page_end <= offset:",
        "        if False:",
        "tests/test_tool_read.py",
    ),
    (
        "A12  a credential read leaves a ledger trace",
        "ai_coding/tools/read_tool.py",
        "        if note_read is not None and not credential:",
        "        if note_read is not None:",
        "tests/test_tool_read.py",
    ),
]


def run_tests(targets: str) -> bool:
    """True when the suite is green."""
    proc = subprocess.run(
        [str(PY), "-m", "pytest", *targets.split(), "-x", "-q", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def main() -> int:
    survived: list[str] = []
    for name, relpath, old, new, targets in MUTATIONS:
        path = ROOT / relpath
        original = path.read_text()
        if old not in original:
            print(f"SKIP  {name}: anchor not found")
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
    print(f"all {len(MUTATIONS)} mutations killed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
