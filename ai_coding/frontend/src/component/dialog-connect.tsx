/**
 * Guided "connect a provider or model" (H3 §5.2).
 *
 * Four screens, one question each: the provider (a filterable list), the
 * authentication answer (a list), the one text field that provider still needs
 * — a key, or the endpoint for `generic` — and the model name.  The shape is
 * `dialog-mcp-add.tsx`'s: lists reuse `DialogSelect`, input screens hold one
 * field and take only what is typed into it, and enter belongs to the keymap
 * rather than to the input's `onSubmit`.
 *
 * Nothing here decides what a provider offers or what gets sent: that is
 * `util/connect.ts`, which is unit-tested.  This file lays the answers out and
 * hands the body to `model.connect`, which owns validating, persisting to the
 * sidecar and hot-adding the model.  It does not switch to the new model —
 * `/connect` registers it, `/models` switches to it (opencode's split).
 */

import { Show, createSignal, onMount } from "solid-js"
import { TextAttributes, type InputRenderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import type { IpcClient } from "../ipc"
import type { SelectOption } from "../util/dialog-select"
import type { AuthChoice, ConnectStep, ProviderInfo } from "../util/connect"
import {
  GENERIC_PROVIDER,
  authOptions,
  buildConnectBody,
  nextStep,
  providerOptions,
} from "../util/connect"
import { theme } from "../theme/tokens"
import { DialogSelect } from "./dialog-select"

interface Field {
  key: "apiKey" | "baseUrl" | "model"
  label: string
  placeholder: string
}

export function DialogConnect(props: { client: IpcClient; onDone: () => void }) {
  const [providers, setProviders] = createSignal<ProviderInfo[]>([])
  const [provider, setProvider] = createSignal<ProviderInfo | null>(null)
  const [auth, setAuth] = createSignal<AuthChoice>("none")
  const [step, setStep] = createSignal<ConnectStep>("provider")
  const [loading, setLoading] = createSignal(true)
  // Only ever what the operator typed into the current input: the elements are
  // keyed by field, so a new screen gets a new element rather than a cleared
  // one, and nothing ever writes to a live input.
  const [typed, setTyped] = createSignal("")
  const [apiKey, setApiKey] = createSignal("")
  const [baseUrl, setBaseUrl] = createSignal("")
  const [error, setError] = createSignal("")

  onMount(() => {
    void (async () => {
      try {
        const data = (await props.client.request("model.providers")) as {
          providers?: ProviderInfo[]
        }
        setProviders(data.providers ?? [])
        if (!data.providers?.length) setError("the backend listed no providers")
      } catch (cause) {
        setError(String(cause))
      } finally {
        setLoading(false)
      }
    })()
  })

  const isList = () => step() === "provider" || step() === "auth"

  const title = (): string => {
    const chosen = provider()
    if (!chosen || step() === "provider") return "Connect a provider"
    if (step() === "auth") return `Connect ${chosen.id} — how do you authenticate?`
    if (step() === "key") {
      return chosen.id === GENERIC_PROVIDER
        ? `Connect ${chosen.id} — endpoint`
        : `Connect ${chosen.id} — API key`
    }
    return `Connect ${chosen.id} — model name`
  }

  /**
   * The one text field the current screen asks for.  `generic` has no default
   * endpoint, so its key screen asks for the URL instead; that is the only
   * extra screen the four-step skeleton can need.
   */
  const field = (): Field | null => {
    const chosen = provider()
    if (!chosen) return null
    if (step() === "key") {
      return chosen.id === GENERIC_PROVIDER
        ? { key: "baseUrl", label: "Endpoint URL", placeholder: "https://api.example.com/v1" }
        : { key: "apiKey", label: "API key", placeholder: "sk-…" }
    }
    if (step() === "model") {
      return { key: "model", label: "Model name", placeholder: "gpt-5" }
    }
    return null
  }

  const authSelectOptions = (): SelectOption[] => {
    const chosen = provider()
    if (!chosen) return []
    return authOptions(chosen).map((option) => ({
      title: option.title,
      value: option.value,
      description: option.hint,
    }))
  }

  /**
   * `nextStep` is the skeleton; a generic provider needs one more question
   * before the model name, and `auth` is the only screen that can lead into it
   * (its key screen is otherwise unused — it has no key to ask for).
   */
  const advance = (from: ConnectStep, choice?: string) => {
    const next = nextStep(from, choice)
    const endpointFirst = from === "auth" && provider()?.id === GENERIC_PROVIDER
    setStep(endpointFirst ? "key" : next)
    setTyped("")
  }

  const pickProvider = (option: SelectOption) => {
    const chosen = providers().find((item) => item.id === option.value)
    if (!chosen) return
    setProvider(chosen)
    setAuth("none")
    setError("")
    advance("provider", option.value)
  }

  const pickAuth = (option: SelectOption) => {
    setAuth(option.value as AuthChoice)
    setError("")
    advance("auth", option.value)
  }

  const submit = () => {
    if (step() === "key") submitField()
    else if (step() === "model") void connect()
  }

  const submitField = () => {
    const current = field()
    if (!current) return
    const value = typed().trim()
    if (!value) {
      setError(current.key === "baseUrl" ? "an endpoint is required" : "a key is required")
      return
    }
    if (current.key === "baseUrl") setBaseUrl(value)
    else setApiKey(value)
    setError("")
    advance("key")
  }

  const connect = async () => {
    const chosen = provider()
    if (!chosen) return
    const name = typed().trim()
    if (!name) {
      setError("a model name is required")
      return
    }
    setError("")
    try {
      const result = (await props.client.request(
        "model.connect",
        buildConnectBody({
          provider: chosen.id,
          model: name,
          baseUrl: baseUrl(),
          apiKey: apiKey(),
        }) as unknown as Record<string, unknown>,
      )) as { ok?: boolean; reason?: string }
      if (result.ok === false) {
        setError(result.reason ?? "the provider was refused")
        return
      }
      // The list behind this dialog is the place the new row appears, so the
      // wizard hands it back instead of closing to the composer (M5's shape).
      props.onDone()
    } catch (cause) {
      setError(String(cause))
    }
  }

  // Enter is the keymap's on the input screens only: the list screens' own
  // `DialogSelect` already binds return, and two bindings would advance twice.
  useBindings(() => ({
    enabled: !isList(),
    commands: [{ name: "dialog.connect.submit", desc: "Continue", run: submit }],
    bindings: [{ key: "return", cmd: "dialog.connect.submit" }],
  }))

  return (
    <Show
      when={isList()}
      fallback={
        <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} gap={1}>
          <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
            {title()}
          </text>

          <text fg={theme.textBase}>{`\u203a ${field()?.label ?? ""}`}</text>

          <Show when={field()} keyed>
            {(current) => (
              <input
                ref={(element: InputRenderable) => element.focus()}
                placeholder={current.placeholder}
                placeholderColor={theme.textMuted}
                textColor={theme.textBase}
                focusedTextColor={theme.textBase}
                backgroundColor="transparent"
                focusedBackgroundColor="transparent"
                cursorColor={theme.textBase}
                onInput={setTyped}
              />
            )}
          </Show>

          <text fg={theme.textMuted}>
            {step() === "model" ? "enter connect · esc cancel" : "enter next · esc cancel"}
          </text>
          <Show when={error()}>
            <text fg={theme.feedbackError}>{error()}</text>
          </Show>
        </box>
      }
    >
      <Show
        when={step() === "provider"}
        fallback={
          <DialogSelect
            title={title()}
            options={authSelectOptions()}
            onSelect={pickAuth}
            footer={error() || "↑/↓ choose · enter next · esc cancel"}
          />
        }
      >
        <DialogSelect
          title="Connect a provider"
          options={providerOptions(providers())}
          onSelect={pickProvider}
          footer={
            error() ||
            (loading() ? "loading providers…" : "↑/↓ choose · enter next · esc cancel")
          }
        />
      </Show>
    </Show>
  )
}
