/**
 * The agent picker (T7) — the TUI `/agents` listing.
 *
 * No list of its own: rows come from state/agents (the visible primaries),
 * the current marker from SessionState.agent, and the layout from
 * DialogSelect.  Selection hands the id back; the caller runs
 * session.agent.select.
 */

import type { AgentSummary } from "../state/agents"
import { DialogSelect } from "./dialog-select"

export function AgentPicker(props: {
  agents: readonly AgentSummary[]
  current: string | null
  onSelect: (id: string) => void
}) {
  return (
    <DialogSelect
      title="Agents"
      options={props.agents.map((row) => ({
        title: row.name,
        value: row.id,
        description: row.description || undefined,
        category: row.source || undefined,
        current: row.id === props.current,
      }))}
      onSelect={(option) => props.onSelect(option.value)}
      footer="enter switch · esc close"
    />
  )
}
