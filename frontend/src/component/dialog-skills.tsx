/**
 * Skills dialog (M3) — DialogSkill: the registry's skills with their
 * description and phase.
 *
 * The panel is opened from two places. On Home there is no composer, so a
 * selection just closes it. Inside a session the selection hands the name to the
 * composer, which writes `@name` — the same spelling the request assembler
 * resolves back into a skill (OpenCode writes `@${skill}` at
 * `prompt/index.tsx:590` for the identical gesture). `onSelect` is absent on
 * Home, which is what makes the two cases the same component.
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

export function DialogSkills(props: { client: IpcClient; onSelect?: (name: string) => void }) {
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
    props.onSelect?.(String(option.value ?? option.title))
    dialog.close()
  }

  return (
    <DialogSelect
      title="Skills"
      options={options()}
      onSelect={choose}
      footer={error() || (props.onSelect ? "enter add to prompt" : "esc close")}
    />
  )
}
