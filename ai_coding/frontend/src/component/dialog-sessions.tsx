/**
 * Sessions dialog (M3, delete added in A3.3) — saved sessions grouped by day,
 * the current one marked.  Selecting loads it through the real `session.load`
 * and hands the replayed state back to the app (which swaps the route).
 *
 * Deletion is a row action, not a dialog of its own, and it is armed before it
 * fires: one press rewrites the row to "press again to confirm", a second press
 * on the same row calls `session.delete`.  OpenCode does exactly this in
 * `dialog-session-list.tsx`; a modal for a list row would cost a keystroke and
 * a screen for a decision the row itself can carry.
 */

import { createSignal, onMount } from "solid-js"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { SessionState } from "../state/session"
import { loadSession } from "../state/replay"
import { dayCategory } from "../util/session-group"
import type { SelectAction, SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"

/** The row is rewritten, not re-listed, while it waits for the second press. */
const CONFIRM = "press ctrl+d again to confirm"

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
  /** Fired after every successful delete; `next` is set when it was the live one. */
  onDeleted?: (deleted: string, next: string) => void
}) {
  const dialog = useDialog()
  const [rows, setRows] = createSignal<SessionRow[]>([])
  const [armed, setArmed] = createSignal("")
  const [error, setError] = createSignal("")
  // The "还没有会话" sentence is only true after the list has answered; a fetch
  // in flight (or a failed one) must not be reported as "no sessions".
  const [loaded, setLoaded] = createSignal(false)
  const now = new Date()

  const listRows = async (): Promise<SessionRow[]> => {
    const data = (await props.client.request("session.list")) as { sessions?: SessionRow[] }
    return data.sessions ?? []
  }

  onMount(async () => {
    try {
      const rows = await listRows()
      setRows(rows)
      setLoaded(true)
    } catch (cause) {
      setError(String(cause))
    }
  })

  const options = (): SelectOption[] =>
    rows().map((row) => {
      const pending = armed() === row.id
      return {
        title: pending ? CONFIRM : row.title || row.id.slice(0, 8),
        value: row.id,
        description: pending ? undefined : row.preview || undefined,
        category: dayCategory(row.lastAccessTime ?? "", now),
        current: row.id === props.currentId,
        danger: pending,
      }
    })

  // Arm, then fire.  The second press is the one that deletes.
  const remove = async (option: SelectOption) => {
    if (armed() !== option.value) {
      setArmed(option.value)
      return
    }
    setArmed("")
    try {
      const result = (await props.client.request("session.delete", { id: option.value })) as {
        ok?: boolean
        sessionId?: string
      }
      if (!result.ok) {
        setError("that session is gone already — reloading the list")
        setRows(await listRows())
        return
      }
      setRows(rows().filter((row) => row.id !== option.value))
      // Always tell the app: even a non-live delete changes the list the tab
      // strip draws from.  When the live one went, `next` names its replacement
      // — J6, because the next turn would otherwise write into a deleted id.
      props.onDeleted?.(option.value, result.sessionId ?? "")
    } catch (cause) {
      setArmed("")
      setError(String(cause))
    }
  }

  const actions: SelectAction[] = [
    { key: "ctrl+d", title: "delete", onTrigger: (option) => option && void remove(option) },
  ]

  const choose = async (option: SelectOption) => {
    try {
      props.onSelect(option.value, await loadSession(props.client, option.value))
      dialog.close()
    } catch (cause) {
      setError(String(cause))
    }
  }

  return (
    <DialogSelect
      title="Sessions"
      options={options()}
      actions={actions}
      onSelect={(option) => void choose(option)}
      emptyText={loaded() ? "还没有会话（发一条消息就会创建）" : undefined}
      footer={error() || armed() ? CONFIRM : "enter open · ctrl+d delete · esc close"}
    />
  )
}
