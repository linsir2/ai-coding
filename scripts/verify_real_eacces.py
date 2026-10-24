"""M-A4 real-logic verification -- run as a NON-ROOT user.

The pytest cases inject PermissionError so they run anywhere.  This script is
the other half: it proves the *product* code really handles a real EACCES from
the operating system, with no injection at all.  Run it as an unprivileged user:

    setpriv --reuid=65534 --regid=65534 --clear-groups \
        env HOME=/tmp GIT_CONFIG_GLOBAL=/dev/null \
        /tmp/nbvenv/bin/python scripts/verify_real_eacces.py

``GIT_CONFIG_GLOBAL=/dev/null`` matters: git prints a warning to *stderr* when
it cannot read the user's config, and ``ai_coding.core.gitcmd.git`` returns
stdout+stderr together, so ``SnapshotStore.capture`` would otherwise read that
warning as the stash sha and silently record nothing.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_coding.core.snapshots import SnapshotStore, _remove  # noqa: E402
from ai_coding.security.grants import GrantStore  # noqa: E402

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
    return root


async def main() -> int:
    print(f"euid = {os.geteuid()}")
    if os.geteuid() == 0:
        print("REFUSE TO RUN: this script must run as a non-root user.")
        return 2

    base = Path(tempfile.mkdtemp(prefix="real_eacces_"))

    # ---- 1. _remove really catches an EACCES -------------------------------
    locked = base / "ro"
    locked.mkdir()
    victim = locked / "kept.txt"
    victim.write_text("x\n", encoding="utf-8")
    os.chmod(locked, 0o555)
    try:
        got = _remove(victim)
        check("_remove catches a real EACCES and reports failure",
              got is False and victim.exists(),
              f"_remove -> {got}, still exists -> {victim.exists()}")
    finally:
        os.chmod(locked, 0o755)

    # ---- 2. restore() really collects it into `failed` ---------------------
    root = repo(base)
    store = SnapshotStore(root)
    ref = await store.capture("s/1")
    check("capture produced a real ref", ref is not None, f"capture -> {ref!r}")
    if ref is None:
        print("\n  aborting: without a ref the restore below proves nothing")
        return 1
    locked2 = root / "ro"
    locked2.mkdir()
    (locked2 / "kept.txt").write_text("x\n", encoding="utf-8")
    (root / "after.txt").write_text("y\n", encoding="utf-8")
    os.chmod(locked2, 0o555)
    try:
        report = await store.restore("s/1", created={"ro/kept.txt"})
        check("restore reports the file it could not delete",
              "ro/kept.txt" in report.failed and (locked2 / "kept.txt").exists(),
              f"failed={report.failed} still_exists={(locked2 / 'kept.txt').exists()}")
    finally:
        os.chmod(locked2, 0o755)

    # ---- 3. GrantStore._write really swallows a write failure --------------
    os.environ["XDG_CONFIG_HOME"] = str(base / "config")
    gs = GrantStore(workspace=base / "ws")
    gs.path.parent.mkdir(parents=True, exist_ok=True)
    gs.path.parent.chmod(0o500)
    try:
        raised: list[str] = []
        try:
            gs.add("edit:a.py")
        except Exception as exc:  # noqa: BLE001
            raised.append(repr(exc))
        check("GrantStore swallows an unwritable store",
              not raised and not gs.path.exists(),
              f"raised={raised} exists={gs.path.exists()}")
    finally:
        gs.path.parent.chmod(0o700)

    print(f"\n  {len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  FAILED {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

