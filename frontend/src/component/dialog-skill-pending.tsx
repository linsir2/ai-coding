/**
 * Pending-skill approval (M8).
 *
 * `skill_review` runs on a timer — on by default, every 10 turns — and used to
 * write a generated `SKILL.md` straight into the user's source tree: no diff,
 * no prompt, and the only trace a line in the log. A generated skill is not
 * just a file, it is an instruction the model will follow from then on.
 *
 * Generated skills now wait in `skills/.pending/`. This is where a human reads
 * why the model thinks the skill is worth keeping, and moves it in or throws
 * it away. `a` approves, `d` rejects.
 */

import { createSignal, onMount } from "solid-js"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { SelectAction, SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"

interface PendingRow {
  name: string
  reason: string
}

export function DialogSkillPending(props: { client: IpcClient }) {
  const dialog = useDialog()
  const [rows, setRows] = createSignal<PendingRow[]>([])
  const [error, setError] = createSignal("")
  const [busy, setBusy] = createSignal("")

  const load = async () => {
    try {
      const data = (await props.client.request("skill.pending.list")) as {
        pending?: PendingRow[]
      }
      setRows(data.pending ?? [])
      setError("")
    } catch (cause) {
      setError(String(cause))
    }
  }

  onMount(load)

  const decide = async (name: string, method: "approve" | "reject") => {
    setBusy(`${method} ${name}…`)
    try {
      await props.client.request(`skill.pending.${method}`, { name })
      await load()
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy("")
    }
  }

  const options = (): SelectOption[] =>
    rows().map((row) => ({
      title: row.name,
      value: row.name,
      description: row.reason || "(no reason given)",
      category: "待审批",
    }))

  const actions = (): SelectAction[] => [
    { key: "a", title: "approve", onTrigger: (option) => option && void decide(String(option.value), "approve") },
    { key: "d", title: "reject", onTrigger: (option) => option && void decide(String(option.value), "reject") },
  ]

  return (
    <DialogSelect
      title="待审批的技能"
      options={options()}
      actions={actions()}
      onSelect={(value) => decide(String(value), "approve")}
      footer={busy() || error() || "a 批准 · d 拒绝 · esc 关闭"}
    />
  )
}