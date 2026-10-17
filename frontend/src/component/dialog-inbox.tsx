/**
 * Inbox dialog (D-2) — what the operator queued, and what to do about it.
 *
 * Before this the queue was a number in the sidebar (`3 queued`) with no way
 * to see what the three lines were, let alone fix a typo in one or drop one
 * that stopped mattering.  oc manages its inbox rows from the client
 * (reorder/edit/undo over server mutations); dsh splices its inbox by
 * position.  Here every row has a stable id, so the panel addresses rows, not
 * positions.
 *
 * Rows come from the caller, not from a fetch: the session route already folds
 * the table into state (mount snapshot, reload merge), so a second read here
 * would race it.  Mutations go through `session.inbox` and then update state
 * through the same actions the route uses -- one path for a row to change, no
 * matter which side asked.
 */

import { createSignal, onMount } from "solid-js"
import { DialogSelect } from "./dialog-select"
import type { IpcClient } from "../ipc"
import type { QueuedLine } from "../state/session"
import type { SelectAction, SelectOption } from "../util/dialog-select"

export function inboxRowLine(row: QueuedLine): { title: string; description: string } {
  const clip = row.text.length <= 80 ? row.text : `${row.text.slice(0, 79)}…`
  const local = row.id.startsWith("local-") ? "未落盘 · " : ""
  return { title: clip, description: `${local}id ${row.id}` }
}

export function DialogInbox(props: {
  client: IpcClient
  sessionId: string
  /**
   * Route-integrated mode: rows come from session state and mutations report
   * back through these.  Absent in palette mode (the app tree cannot reach
   * session state -- the two routes never mount together), where the dialog
   * reads the table itself and refreshes after every mutation instead.
   */
  rows?: () => QueuedLine[]
  onEdited?: (id: string, text: string) => void
  onDeleted?: (id: string) => void
  onSelectRow?: (row: QueuedLine) => void
}) {
  const [error, setError] = createSignal("")
  const [fetched, setFetched] = createSignal<QueuedLine[] | null>(null)

  const refresh = async (): Promise<void> => {
    try {
      const data = (await props.client.request("session.inbox", { op: "list" })) as {
        rows?: unknown
      }
      const rows = Array.isArray(data.rows) ? data.rows : []
      setFetched(
        rows
          .map((row) => {
            const record = row as Record<string, unknown>
            if (typeof record["id"] !== "string" || typeof record["text"] !== "string") return null
            return { id: record["id"] as string, text: record["text"] as string }
          })
          .filter((row): row is QueuedLine => row !== null),
      )
      setError("")
    } catch (cause) {
      setError(String(cause))
    }
  }

  const liveRows = (): QueuedLine[] => props.rows?.() ?? fetched() ?? []

  onMount(() => {
    if (props.rows === undefined) void refresh()
  })

  const options = (): SelectOption[] =>
    liveRows().map((row) => {
      const line = inboxRowLine(row)
      return { title: line.title, value: row.id, description: line.description }
    })

  /** One call shape for both mutations: the server first, then state. */
  const call = async (op: string, params: Record<string, unknown>): Promise<boolean> => {
    try {
      const result = (await props.client.request("session.inbox", {
        op,
        ...params,
      })) as { ok?: boolean; reason?: string }
      if (result.ok !== true) {
        setError(String(result.reason ?? "后端拒绝了这次修改"))
        return false
      }
      setError("")
      return true
    } catch (cause) {
      setError(String(cause))
      return false
    }
  }

  const remove = async (option: SelectOption) => {
    const row = liveRows().find((entry) => entry.id === option.value)
    if (!row) return
    if (await call("delete", { id: row.id })) {
      if (props.onDeleted !== undefined) props.onDeleted(row.id)
      else await refresh()
    }
  }

  const actions = (): readonly SelectAction[] => [
    {
      key: "ctrl+d",
      title: "delete",
      onTrigger: (option) => option && void remove(option),
    },
  ]

  return (
    <DialogSelect
      title="排队输入"
      options={options()}
      // Route mode only: Enter seeds the composer for editing (the palette
      // tree cannot reach it).  Palette mode leaves Enter unbound.
      onSelect={props.onSelectRow === undefined ? undefined : (option) => {
        const row = liveRows().find((entry) => entry.id === option.value)
        if (row !== undefined) props.onSelectRow?.(row)
      }}
      actions={actions()}
      footer={
        [
          error(),
          liveRows().length === 0 ? "没有排队输入" : "",
          props.onSelectRow === undefined
            ? "ctrl+d 删除 · esc close"
            : "enter 编辑 · ctrl+d 删除 · esc close",
        ]
          .filter(Boolean)
          .join(" · ") || undefined
      }
    />
  )
}
