/**
 * Memory dialog (M7) — what the agent believes, and the two things an operator
 * can do about it.
 *
 * `/memory` used to print one line: forty-three notes, thirty-three scene
 * labels, and nothing you could read. That number is not actionable. You
 * could not see what the agent had been told, you could not mark the one
 * decision that must never be forgotten, and you could not remove something
 * you had taught it wrongly — the store has had `pin` and `delete` since R3 and
 * nothing in the UI reached them.
 *
 * The panel is deliberately the same shape as the MCP one (`DialogSelect`,
 * rows, actions, a footer): two dialogs that work the same way are two dialogs
 * you only learn once. Grouping is by **kind** and not by scene, because the
 * scenes are free text the extractor invents — thirty-three labels across
 * forty-three notes, several near-duplicates (`记忆模块` / `记忆系统` /
 * `记忆配置`). A taxonomy beats a tag cloud for grouping; the scene is a
 * subtitle and stays searchable.
 *
 * The list is read on open and again after every action, and that is all it
 * can be: the event bridge deliberately excludes memory (`bridge.py`: memory
 * is listed among what does not reach the frontend), so there is no live signal
 * to subscribe to and none is invented here. A turn that harvests a memory
 * while this panel is open is picked up the next time you act or reopen it —
 * which is the honest behaviour, not a missing feature.
 *
 * Deleting a memory is irreversible and cannot be re-harvested, so the row is
 * rewritten to "press ctrl+d again to confirm" exactly as the sessions panel
 * does — same muscle memory, same consequence acknowledged in the same words.
 */

import { createSignal, onMount } from "solid-js"
import type { IpcClient } from "../ipc"
import type { SelectAction, SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"
import type { MemoryNote } from "../util/mcp"
import { memoryRowLine } from "../util/mcp"

/** How much of a note's text a row shows; the panel is for scanning, and enter
 *  puts the whole thing in the footer for the ones that do not fit. */
const TITLE_LIMIT = 64

const CONFIRM_DELETE = "再按一次 ctrl+d 确认删除"
const CONFIRM_NONE = ""

export function DialogMemory(props: { client: IpcClient }) {
  const [notes, setNotes] = createSignal<MemoryNote[]>([])
  const [enabled, setEnabled] = createSignal(true)
  const [stats, setStats] = createSignal<{ total?: number; by_kind?: Record<string, number> }>({})
  const [error, setError] = createSignal("")
  const [armed, setArmed] = createSignal(CONFIRM_NONE)
  const [viewing, setViewing] = createSignal("")

  const refresh = async (): Promise<void> => {
    try {
      const data = (await props.client.request("memory.list")) as {
        notes?: MemoryNote[]
        enabled?: boolean
        stats?: { total?: number; by_kind?: Record<string, number> }
      }
      setNotes(data.notes ?? [])
      setEnabled(data.enabled !== false)
      setStats(data.stats ?? {})
    } catch (cause) {
      setError(String(cause))
    }
  }

  onMount(refresh)

  const clip = (text: string): string =>
    text.length <= TITLE_LIMIT ? text : `${text.slice(0, TITLE_LIMIT - 1)}…`

  const options = (): SelectOption[] =>
    notes().map((note) => {
      const row = memoryRowLine(note)
      const pending = armed() === note.id
      return {
        title: pending ? CONFIRM_DELETE : clip(row.title),
        value: note.id,
        description: pending ? undefined : row.description,
        category: row.category,
        danger: pending,
      }
    })

  /** One call shape for both mutations: report a vanished note, then re-read. */
  const call = async (method: string, params: Record<string, unknown>): Promise<boolean> => {
    try {
      const result = (await props.client.request(method, params)) as { ok?: boolean }
      if (result.ok !== true) {
        setError("这条记忆已经不在了")
        await refresh()
        return false
      }
      setError("")
      await refresh()
      return true
    } catch (cause) {
      setError(String(cause))
      return false
    }
  }

  // Arm, then fire: the second press is the one that deletes.
  const remove = async (option: SelectOption) => {
    if (armed() !== option.value) {
      setArmed(option.value)
      return
    }
    setArmed(CONFIRM_NONE)
    await call("memory.delete", { id: option.value })
  }

  const togglePin = async (option: SelectOption) => {
    const note = notes().find((entry) => entry.id === option.value)
    if (!note) return
    await call("memory.pin", { id: note.id, pinned: !note.pinned })
  }

  const actions = (): readonly SelectAction[] => [
    { key: "ctrl+d", title: "delete", onTrigger: (option) => option && void remove(option) },
    {
      // ctrl+p is already the select's own "previous" alias (and a duplicate
      // of `up`), so pin takes ctrl+t: mnemonic enough, and it moves nothing
      // in the palette, sessions or permissions.
      key: "ctrl+t",
      title: "pin/unpin",
      onTrigger: (option) => option && void togglePin(option),
    },
  ]

  /** The whole note, for the ones too long for a row. */
  const showFull = (option: SelectOption) => {
    const note = notes().find((entry) => entry.id === option.value)
    setViewing(note ? note.text : "")
  }

  const summary = (): string => {
    const kinds = stats().by_kind ?? {}
    const parts = ["fact", "decision", "preference", "open_thread"]
      .filter((kind) => kinds[kind])
      .map((kind) => `${kind} ${kinds[kind]}`)
    const pinned = notes().filter((note) => note.pinned).length
    return [
      `${notes().length} 条`,
      parts.length ? parts.join(" · ") : "",
      pinned ? `${pinned} 条钉住` : "",
      enabled() ? "" : "记忆已关闭",
    ]
      .filter(Boolean)
      .join(" · ")
  }

  return (
    <DialogSelect
      title="记忆"
      options={options()}
      onSelect={showFull}
      actions={actions()}
      footer={
        [
          viewing(),
          error(),
          summary(),
          notes().length === 0 ? "没有记忆" : "",
          "enter 全文 · ctrl+d delete · ctrl+t pin/unpin · esc close",
        ]
          .filter(Boolean)
          .join(" · ") || undefined
      }
    />
  )
}