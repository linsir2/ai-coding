/**
 * Models dialog (M3, live list since H3) — pick a model, mark the current one
 * by key, switch through the real `model.set` RPC.
 *
 * `props.models` (the `hello` snapshot) paints the first frame so the panel
 * never flashes empty; `model.list` then replaces it on mount, because a
 * `/connect` made earlier in this run is not in that snapshot.  The list is
 * handed to `DialogSelect` directly — wrapping it in `<Show>` to swap the
 * source would remount the component and take focus out of its search box.
 *
 * `ctrl+a` is the panel's action, not a bare letter: the search input has
 * focus, so a plain "a" is typed into the filter (the same reason the MCP
 * panel's connect is ctrl+a).
 */

import { createSignal, onMount } from "solid-js"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { SelectAction, SelectOption } from "../util/dialog-select"
import { DialogSelect } from "./dialog-select"

export interface ModelRow {
  name: string
  key: string
  /** Only `model.list` carries these; the `hello` first frame has name/key. */
  provider?: string
  baseUrl?: string
  hasKey?: boolean
}

export function DialogModels(props: {
  client: IpcClient
  models: ModelRow[]
  currentKey: string
  /** Returns true when the switch succeeded. */
  onSelect: (key: string, name: string) => Promise<boolean>
  /** Opens the guided connect wizard; the app wires the dialog itself. */
  onConnectNew: () => void
}) {
  const dialog = useDialog()
  const [rows, setRows] = createSignal<ModelRow[]>(props.models)
  const [liveCurrent, setLiveCurrent] = createSignal<string | undefined>(undefined)
  const [error, setError] = createSignal("")

  onMount(() => {
    void (async () => {
      try {
        const data = (await props.client.request("model.list")) as {
          current?: string
          models?: ModelRow[]
        }
        if (Array.isArray(data.models)) setRows(data.models)
        if (data.current) setLiveCurrent(data.current)
      } catch {
        // The first frame is still a usable list; the next open retries.
      }
    })()
  })

  const currentKey = (): string => liveCurrent() ?? props.currentKey

  const options = (): SelectOption[] =>
    rows().map((model) => ({
      title: model.name,
      value: model.key,
      description: model.provider,
      category: "Models",
      current: model.key === currentKey(),
    }))

  const choose = async (option: SelectOption) => {
    const model = rows().find((row) => row.key === option.value)
    if (!model) return
    if (await props.onSelect(model.key, model.name)) {
      dialog.close()
    } else {
      setError("could not switch model")
    }
  }

  const actions = (): readonly SelectAction[] => [
    { key: "ctrl+a", title: "connect", onTrigger: () => props.onConnectNew() },
  ]

  return (
    <DialogSelect
      title="Models"
      options={options()}
      actions={actions()}
      onSelect={(option) => void choose(option)}
      footer={error() || "ctrl+a connect · enter switch · esc close"}
    />
  )
}
