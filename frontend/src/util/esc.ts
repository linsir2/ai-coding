export function escStep(armed: boolean): { armed: boolean; interrupt: boolean } {
  if (!armed) return { armed: true, interrupt: false }
  return { armed: false, interrupt: true }
}
