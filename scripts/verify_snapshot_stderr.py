"""N-1 real-logic verification -- run as a NON-ROOT user.

The pytest cases inject a polluted stream so they run anywhere.  This script is
the other half: it proves the *product* code really survives a non-fatal git
warning on stderr, with no injection at all.  Run it as an unprivileged user:

    setpriv --reuid=65534 --regid=65534 --clear-groups \
        env HOME=/tmp GIT_CONFIG_GLOBAL=/dev/null \
        /tmp/nbvenv/bin/python scripts/verify_snapshot_stderr.py

``GIT_CONFIG_GLOBAL=/dev/null`` matters: git prints a warning to *stderr* when
it cannot read the user's config, and that warning would ride along with the
``.git/info/exclude`` one and hide which line is which (the same reasoning
``verify_real_eacces.py`` records).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_coding.core.gitcmd import git  # noqa: E402
from ai_coding.core.snapshots import _SHA_RE, SnapshotStore  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n         {detail}" if detail else ""))


def repo(base: Path) -> Path:
    root = base / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init", "--allow-empty"], cwd=root, check=True)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "add"], cwd=root, check=True)
    # A dirty tree is what makes ``stash create`` print a sha at all.
    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    return root


async def main() -> int:
    print(f"euid = {os.geteuid()}")
    if os.geteuid() == 0:
        print("REFUSE TO RUN: this script must run as a non-root user.")
        return 2

    base = Path(tempfile.mkdtemp(prefix="snapshot_stderr_"))
    root = repo(base)

    # The repository stays writable for the reader, but one file git reads is
    # not: git then exits 0 and prints a non-fatal warning to stderr.
    exclude = root / ".git" / "info" / "exclude"
    exclude.write_text("ignored.txt\n", encoding="utf-8")
    os.chmod(exclude, 0o000)
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "stash", "create"],
            capture_output=True, text=True,
        )
        print(f"  git stash create: exit={proc.returncode}")
        print(f"    stdout={proc.stdout!r}")
        print(f"    stderr={proc.stderr!r}")
        check(
            "git stash create exits 0 and prints the sha despite the warning",
            proc.returncode == 0 and bool(proc.stdout.strip()) and bool(proc.stderr.strip()),
            f"exit={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )

        # The root cause: the merged stream is not a sha, so the old parser
        # would have taken the warning as the value.
        merged = (proc.stdout + proc.stderr).strip()
        check("the merged stream is NOT a sha (the old parser's trap)",
              not _SHA_RE.match(merged), f"merged={merged!r}")

        store = SnapshotStore(root)
        ref = await store.capture("s/1")
        check("capture returns a ref despite the stderr warning",
              ref == "refs/ai-coding/undo/s/1", f"capture -> {ref!r}")
        if ref is None:
            print("\n  aborting: without a ref the shape check proves nothing")
            return 1

        code, sha = await git(root, "rev-parse", ref, merge_stderr=False)
        check("the ref points at a hex sha",
              code == 0 and bool(_SHA_RE.match(sha)), f"rev-parse -> {code}, {sha!r}")
    finally:
        os.chmod(exclude, 0o644)

    print(f"\n  {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
