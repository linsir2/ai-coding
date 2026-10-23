/**
 * The agent list (T7) — the visible primaries, and nothing else.
 *
 * Only the LIST lives here.  The current agent's one truth is
 * ``SessionState.agent`` (hydrated from ``session.stats`` on mount, moved by
 * ``agent.switched`` at runtime); this module never stores a current, so the
 * two cannot disagree.
 */

export interface AgentSummary {
  id: string
  name: string
  kind: string
  source: string
  description: string
}

const str = (value: unknown): string => (typeof value === "string" ? value : "")

/**
 * Narrow wire rows into summaries: visible primaries only (a subagent is a
 * delegation target, not a switch target; hidden agents are not offered),
 * sorted by id so picker and shift+tab share one stable order.
 */
export function normalizeAgents(rows: unknown): AgentSummary[] {
  if (!Array.isArray(rows)) return []
  const out: AgentSummary[] = []
  for (const raw of rows) {
    if (typeof raw !== "object" || raw === null) continue
    const row = raw as Record<string, unknown>
    if (row["kind"] !== "primary" || row["hidden"] === true) continue
    const id = str(row["id"])
    if (!id) continue
    out.push({
      id,
      name: str(row["name"]) || id,
      kind: "primary",
      source: str(row["source"]),
      description: str(row["description"]),
    })
  }
  out.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  return out
}

/**
 * The next agent after `current` in the list order, wrapping at the end.
 * An unknown current (or none) starts at the head; an empty list selects
 * nothing.  shift+tab in both routes reads this one function.
 */
export function nextAgentId(list: readonly AgentSummary[], current: string | null): string | null {
  if (list.length === 0) return null
  const index = list.findIndex((row) => row.id === current)
  return list[(index + 1) % list.length]!.id
}
