/**
 * Plan run eligibility, the pure half (W2/R11).
 *
 * An approved plan starts executing when its turn ends cleanly.  The approval
 * is a yes to *this* plan; the turn that carried it is what the plan was
 * written against, so a turn that ended aborted / error / timeout / budget /
 * max_iterations / stopped left the session somewhere else.  Dispatching a
 * run from that turn starts work against a state nobody looked at.
 *
 * The status arrives as wire data (server/bridge.py:184, server/handlers.py:504)
 * and is untyped there, so it is untyped here too; the comparison is by value.
 * The seven terminal statuses are the ones in server/domain/loop.py:16-25 —
 * "failed" is not among them (that is a job's status, core/jobs.py:107).
 */

export function shouldDispatchPlanRun(pending: boolean, status: unknown): boolean {
  return pending && status === "completed"
}
