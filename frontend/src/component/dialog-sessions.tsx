/**
 * Sessions dialog (M3) — DialogSessionList: saved sessions grouped by day,
 * the current one marked.  Selecting loads it through the real `session.load`
 * and hands the replayed state back to the app (which swaps the route).
 */

import { createSignal, onMount } from "solid-js"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { SessionState } from "../state/session"
import { messagesToState, type WireMessage } from "../state/replay"
import { dayCategory } from "../util/session-group"
import type { SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"

interface SessionRow {
  id: string
  title: string
  lastAccessTime?: string
  preview?: string
}

export function DialogSessions(props: {
  client: IpcClient
  currentId: string
  onSelect: (id: string, state: SessionState) => void
}) {
  const dialog = useDialog()
  const [rows, setRows] = createSignal<SessionRow[]>([])
  const [error, setError] = createSignal("")
  const now = new Date()

  onMount(async () => {
    try {
      const data = (await props.client.request("session.list")) as { sessions?: SessionRow[] }
      setRows(data.sessions ?? [])
    } catch (cause) {
      setError(String(cause))
    }
  })

  const options = (): SelectOption[] =>
    rows().map((row) => ({
      title: row.title || row.id.slice(0, 8),
      value: row.id,
      description: row.preview || undefined,
      category: dayCategory(row.lastAccessTime ?? "", now),
      current: row.id === props.currentId,
    }))

  const choose = async (option: SelectOption) => {
    try {
      const data = (await props.client.request("session.load", { id: option.value })) as {
        messages?: WireMessage[]
      }
      props.onSelect(option.value, messagesToState(data.messages ?? []))
      dialog.close()
    } catch (cause) {
      setError(String(cause))
    }
  }

  return (
    <DialogSelect
      title="Sessions"
      options={options()}
      onSelect={(option) => void choose(option)}
      footer={error() || "enter open · esc close"}
    />
  )
}
