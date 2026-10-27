/**
 * Skills dialog (M3) — the registry's skills with their description and phase.
 *
 * Opened from the route that owns the composer (Home or the session); the
 * selection hands the name back and the composer writes `@name` — the same
 * spelling the request assembler resolves back into a skill (OpenCode writes
 * `@${skill}` at `prompt/index.tsx:590` for the identical gesture).
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

export function DialogSkills(props: { client: IpcClient; onSelect: (name: string) => void }) {
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

  const choose = (option: SelectOption) => {
    props.onSelect(String(option.value ?? option.title))
    dialog.close()
  }

  return (
    <DialogSelect
      title="Skills"
      options={options()}
      onSelect={choose}
      footer={error() || "enter add to prompt"}
    />
  )
}
