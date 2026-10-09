/**
 * Models dialog (M3) — DialogModel: pick a model, mark the current one by key,
 * switch through the real `model.set` RPC (the handler resolves the key).
 */

import { createSignal } from "solid-js"
import { useDialog } from "../dialog/context"
import type { SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"

export interface ModelRow {
  name: string
  key: string
}

export function DialogModels(props: {
  models: ModelRow[]
  currentKey: string
  /** Returns true when the switch succeeded. */
  onSelect: (key: string, name: string) => Promise<boolean>
}) {
  const dialog = useDialog()
  const [error, setError] = createSignal("")

  const options = (): SelectOption[] =>
    props.models.map((model) => ({
      title: model.name,
      value: model.key,
      category: "Models",
      current: model.key === props.currentKey,
    }))

  const choose = async (option: SelectOption) => {
    const model = props.models.find((row) => row.key === option.value)
    if (!model) return
    if (await props.onSelect(model.key, model.name)) {
      dialog.close()
    } else {
      setError("could not switch model")
    }
  }

  return (
    <DialogSelect
      title="Models"
      options={options()}
      onSelect={(option) => void choose(option)}
      footer={error() || "enter switch · esc close"}
    />
  )
}
