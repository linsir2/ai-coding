/**
 * `/connect` decisions, kept out of the renderer (H3 §5.2).
 *
 * The wizard is four screens — provider, authentication, the one text field
 * that provider still needs, and the model name — and every choice on them is
 * decided here so it can be unit-tested without a terminal: which rows the
 * provider list shows, which authentication answers a provider offers, what
 * the next screen is, and what `model.connect` is sent.
 *
 * `model.providers` is the only source of the provider list (it mirrors the
 * backend's table); nothing in this file hard-codes an endpoint or a variable
 * name.
 */

import type { SelectOption } from "./dialog-select"

/** One row of `model.providers` (H3 §5.1). */
export interface ProviderInfo {
  id: string
  /** Empty for `generic`: there is no default endpoint to show. */
  baseUrl: string
  /** Empty when the endpoint does not ask for a key (ollama, generic). */
  envVar: string
  /** A startup-time snapshot, only used to pick the default answer. */
  hasEnv: boolean
}

export type AuthChoice = "env" | "typed" | "none"

export interface AuthOption {
  value: AuthChoice
  title: string
  hint: string
}

/** The wizard's screens, plus the terminal state `nextStep` closes on. */
export type ConnectStep = "provider" | "auth" | "key" | "model" | "done"

export interface ConnectValues {
  provider: string
  model: string
  baseUrl?: string
  apiKey?: string
}

/** The `model.connect` payload; `baseUrl`/`apiKey` are present only when they apply. */
export interface ConnectBody {
  provider: string
  model: string
  baseUrl?: string
  apiKey?: string
}

/** The id whose endpoint is not known in advance, so it has to be typed. */
export const GENERIC_PROVIDER = "generic"

const ANY_ENDPOINT = "any OpenAI-compatible"

/**
 * The provider rows, in the backend's order.  A provider with no default
 * endpoint (generic) says what it is instead of showing an empty description.
 */
export function providerOptions(providers: readonly ProviderInfo[]): SelectOption[] {
  return providers.map((provider) => ({
    title: provider.id,
    value: provider.id,
    description: provider.baseUrl || ANY_ENDPOINT,
  }))
}

/**
 * Which ways of authenticating this provider offers.
 *
 * A conventional variable that is already set gets both answers — use it, or
 * paste a key that overrides it.  A conventional variable that is not set
 * leaves only the typed key: offering "use $X" would fail at connect time.
 * No conventional variable means the endpoint does not ask for a key at all.
 */
export function authOptions(provider: Pick<ProviderInfo, "envVar" | "hasEnv">): AuthOption[] {
  if (!provider.envVar) {
    return [
      {
        value: "none",
        title: "no key needed",
        hint: "this endpoint does not ask for one",
      },
    ]
  }
  const typed: AuthOption = {
    value: "typed",
    title: "paste a key",
    hint: "saved to models.local.yaml (0600)",
  }
  if (!provider.hasEnv) return [typed]
  return [
    {
      value: "env",
      title: `use $${provider.envVar}`,
      hint: "already set in this environment",
    },
    typed,
  ]
}

/**
 * The next screen.  Only a typed key visits the key screen; an environment
 * reference and a keyless endpoint go straight to the model name.  `done` is
 * terminal, and so is any step this wizard does not know.
 */
export function nextStep(step: ConnectStep, choice?: string): ConnectStep {
  if (step === "provider") return "auth"
  if (step === "auth") return choice === "typed" ? "key" : "model"
  if (step === "key") return "model"
  return "done"
}

/**
 * The `model.connect` body.
 *
 * An empty key is omitted, not sent as `""`: the backend resolves the
 * provider's conventional variable when no key arrives, and an explicit empty
 * string would mean "this model has no key".  `baseUrl` is sent only for the
 * generic provider, whose endpoint cannot be defaulted; for every other
 * provider the backend already knows it.
 */
export function buildConnectBody(values: ConnectValues): ConnectBody {
  const body: ConnectBody = {
    provider: values.provider,
    model: values.model.trim(),
  }
  if (values.provider === GENERIC_PROVIDER) {
    body.baseUrl = (values.baseUrl ?? "").trim()
  }
  const apiKey = (values.apiKey ?? "").trim()
  if (apiKey) body.apiKey = apiKey
  return body
}
