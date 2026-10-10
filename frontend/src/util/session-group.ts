/**
 * Session-list day buckets (M3) — OpenCode's DialogSessionList groups by day,
 * "Today" first.  A missing or unparseable timestamp is not invented into a
 * date: it lands in "Earlier".
 */

/** The group label for a session's last-access time, relative to `now`. */
export function dayCategory(timestamp: string, now: Date): string {
  if (!timestamp) return "Earlier"
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return "Earlier"
  const day = date.toISOString().slice(0, 10)
  return day === now.toISOString().slice(0, 10) ? "Today" : day
}
