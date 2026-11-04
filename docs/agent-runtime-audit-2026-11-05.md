# Agent runtime audit — 2026-11-05

Scope: sessions `ad7360b0…`, `03f1ea61…`, `851d6baf…`, plus the real long-task
script `scripts/eval_longtask_mechanics.py` (qwen, live endpoint).

## Confirmed issues and fixes

| # | Symptom | Root cause | Status |
|---|---------|------------|--------|
| 1 | `proposed_plan` called repeatedly, turn never ends | plan text submitted as an unknown tool; fallback only ran after the turn returned | Fixed: loop closes on a plan body; commands wrap it as `<proposed_plan>` (`8e74b7d`) |
| 2 | "file already read" then endless retry | read coverage vetoed re-reads even when the model needed exact bytes after an edit | Fixed: removed the veto; re-read returns the current page (`9223ec2`) |
| 3 | downloads fail with read-only store | sandbox root is read-only; pnpm/npm stores live under `$HOME`, only `~/.cache` was writable | Fixed: bash/jobs point npm/pnpm/yarn/bun/pip/uv caches at `<ws>/.ai-coding/cache` (`639c9cd`) |
| 4 | all bash/edit/write blocked by `write_conflict` | a long-lived `npx vite` background job held the whole-workspace `*` hold until it settled; other sessions block too | Fixed: all background/promoted jobs skip the job-lifetime hold; the dispatcher still serializes launch |
| 5 | model cannot inspect a background job | job log path was relative to the app CWD, not the command CWD; model used `wait_agent` | Fixed: absolute log path, `job_output <id>` hint; conflict reports the blocker session |
| 6 | no-progress reported as "budget" | `MAX_ITERATIONS` was used for the 2-iteration no-success ladder | Fixed: UI distinguishes no-progress (`9223ec2`) |
| 7 | steer/Esc leaves "working" | steer ack had no turn id; a later finish was orphaned | Fixed: steer names the live turn; Esc with `interrupted:0` clears stale busy (`88c2e17`) |

## Long-task real run

`eval_longtask_mechanics.py --config config.local.yaml`:

- background subagent returned in 6.4s; ledger completed later
- 10 continuation turns stayed at 24 messages
- L3 persisted an oversized tool result
- explicit compaction removed 2 messages and did not fail
- watchdog bounded an 8s turn
- result: 6 passed, 0 failed

No `CompactionFailed` event appeared in the recent session traces. Compression
failure is therefore **not reproduced** in these logs; the explicit compaction
and L3 path passed. A larger stress case is still a follow-up, not a claimed fix.

## Design decision: long-lived services and the write lock

The old invariant was "a background job holds `*` until it settles". A dev
server can outlive every useful operation, so that invariant deadlocks the
workspace and blocks the very `job_kill` diagnosis that could release it.

The new policy is deliberately narrow but not a free pass:

- **all background/promoted jobs** take no job-lifetime hold; the dispatcher's
  launch claim still protects startup;
- finite installs/builds also run without a lifetime hold (keep them foreground
  when serialization matters);
- `job_kill` has no write claim, so it can stop a job;
- `write_conflict` names the blocking session, but no longer points at a
  running job as the holder (a background job cannot hold the workspace after
  2026-11-05).

Trade-off: a background job's writes after launch are not serialized with
other tools. This is the price of not letting a server own the workspace
forever; it is documented in `write_coordinator.py`, `dispatch.py`, and the
P2-B plan.

## Remaining follow-ups

1. Stress compaction with a real oversized context (not just explicit compact);
   record `CompactionFailed` / breaker behavior per session.
2. Add a UI action/clear path for a conflict when `job_kill` is unavailable
   (sub-agent stack has no job tools).
3. Keep package-manager caches durable across workspace moves; job ledger log
   paths are rebased on rescan.
4. Consider a configurable strict mode for users who want service writes
   serialized even at the cost of a blocked workspace.
