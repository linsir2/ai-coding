/**
 * Plan run eligibility, the pure half (W2/R11).
 *
 * An approved plan starts executing when its turn ends with the plan intact.
 * The approval is a yes to *this* plan, and present_plan only raises the card
 * after a successful submission, so a pending approval implies the plan exists
 * and the task is armed.  Two endings qualify:
 *
 * - ``completed`` -- the ordinary path (approval closes the turn).
 * - ``max_iterations`` -- r22: a large plan can be submitted and approved after
 *   the planning budget is gone.  The turn still holds a valid plan, and
 *   refusing the run left the operator with an approved plan that never
 *   executed ("approved, then nothing").  The run uses its own step sessions,
 *   so it is not contaminated by the cut turn.
 *
 * The other endings still refuse: aborted / stopped / error / timeout left
 * the session somewhere the plan was not written against, and ``budget`` (the
 * token ceiling) means the model never reached its exit.
 *
 * The status arrives as wire data (server/bridge.py:184, server/handlers.py:504)
 * and is untyped there, so it is untyped here too; the comparison is by value.
 * The seven terminal statuses are the ones in server/domain/loop.py:16-25 —
 * "failed" is not among them (that is a job's status, core/jobs.py:107).
 */

const RUNNABLE_ENDINGS = new Set(["completed", "max_iterations"])

export function shouldDispatchPlanRun(pending: boolean, status: unknown): boolean {
  return pending && typeof status === "string" && RUNNABLE_ENDINGS.has(status)
}
