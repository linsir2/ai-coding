/**
 * Skills dialog (M3) — DialogSkill: the registry's skills with their
 * description and phase.  Read-only: this project has no "attach skill to
 * prompt" action (the model loads skills itself), so selecting just closes.
 */

import { createSignal, onMount } from "solid-js"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"

interface SkillRow {
  name: string
  description: string | null
  phase: string
}

export function DialogSkills(props: { client: IpcClient }) {
  const dialog = useDialog()
  const [rows, setRows] = createSignal<SkillRow[]>([])
  const [error, setError] = createSignal("")

  onMount(async () => {
    try {
      const data = (await props.client.request("skills.list")) as { skills?: SkillRow[] }
      setRows(data.skills ?? [])
    } catch (cause) {
      setError(String(cause))
    }
  })

  const options = (): SelectOption[] =>
    rows().map((skill) => ({
      title: skill.name,
      value: skill.name,
      description: skill.description || undefined,
      category: skill.phase || "Skills",
    }))

  return (
    <DialogSelect
      title="Skills"
      options={options()}
      onSelect={() => dialog.close()}
      footer={error() || "esc close"}
    />
  )
}
