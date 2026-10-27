/**
 * TDD (H3): the `/connect` decisions — the pure half of the wizard.
 *
 * Every choice the four screens make lives in `util/connect.ts`: which rows the
 * provider list shows, which authentication answers a provider offers, what the
 * next screen is, and what `model.connect` receives.  The renderer only lays
 * those out, so these pin the behaviour without a terminal.
 */

import { describe, expect, test } from "bun:test"
import { filterOptions } from "../src/util/dialog-select"
import {
  authOptions,
  buildConnectBody,
  keyIsOptional,
  nextStep,
  providerOptions,
  type ProviderInfo,
} from "../src/util/connect"

const provider = (overrides: Partial<ProviderInfo> = {}): ProviderInfo => ({
  id: "openai",
  baseUrl: "https://api.openai.com/v1",
  envVar: "OPENAI_API_KEY",
  hasEnv: false,
  ...overrides,
})

describe("providerOptions", () => {
  test("maps a provider to its id, value and endpoint", () => {
    const options = providerOptions([
      provider(),
      provider({ id: "deepseek", baseUrl: "https://api.deepseek.com/v1" }),
    ])
    expect(options.map((option) => option.title)).toEqual(["openai", "deepseek"])
    expect(options.map((option) => option.value)).toEqual(["openai", "deepseek"])
    expect(options[0]!.description).toBe("https://api.openai.com/v1")
  })

  test("a provider with no default endpoint says what it is", () => {
    const [generic] = providerOptions([provider({ id: "generic", baseUrl: "", envVar: "" })])
    expect(generic!.description).toBe("any OpenAI-compatible")
  })

  test("the rows are what DialogSelect's search filters", () => {
    const options = providerOptions([
      provider(),
      provider({ id: "deepseek", baseUrl: "https://api.deepseek.com/v1" }),
    ])
    expect(filterOptions(options, "deep").map((option) => option.title)).toEqual(["deepseek"])
  })
})

describe("authOptions", () => {
  test("a set environment variable offers both using it and pasting a key", () => {
    const options = authOptions(provider({ hasEnv: true }))
    expect(options.map((option) => option.value)).toEqual(["env", "typed"])
    expect(options[0]!.title).toContain("OPENAI_API_KEY")
    expect(options[0]!.hint.length).toBeGreaterThan(0)
  })

  test("an unset environment variable only offers a typed key", () => {
    expect(authOptions(provider({ hasEnv: false })).map((option) => option.value)).toEqual([
      "typed",
    ])
  })

  test("a provider with no conventional variable needs no key", () => {
    expect(authOptions(provider({ envVar: "", hasEnv: false })).map((o) => o.value)).toEqual([
      "none",
    ])
    expect(authOptions(provider({ envVar: "", hasEnv: true })).map((o) => o.value)).toEqual([
      "none",
    ])
  })
})

describe("nextStep", () => {
  test("only a typed key visits the key screen", () => {
    expect(nextStep("provider", "openai")).toBe("auth")
    expect(nextStep("auth", "openai", "env")).toBe("model")
    expect(nextStep("auth", "openai", "typed")).toBe("key")
    expect(nextStep("auth", "ollama", "none")).toBe("model")
  })

  test("the steps close on done", () => {
    expect(nextStep("key", "openai", "sk-…")).toBe("model")
    expect(nextStep("model", "openai", "gpt-5")).toBe("done")
    expect(nextStep("done", "openai")).toBe("done")
  })

  test("generic asks for its endpoint first, then an optional key", () => {
    // A self-hosted OpenAI-compatible server has no endpoint we could default
    // and may or may not want a key, so both questions are asked.
    expect(nextStep("provider", "generic")).toBe("baseUrl")
    expect(nextStep("baseUrl", "generic")).toBe("key")
    expect(nextStep("key", "generic")).toBe("model")
    expect(keyIsOptional("generic")).toBe(true)
    expect(keyIsOptional("openai")).toBe(false)
  })
})

describe("buildConnectBody", () => {
  test("an empty key is omitted so the backend resolves the environment", () => {
    const body = buildConnectBody({ provider: "openai", model: "gpt-5", apiKey: "" })
    expect(body).toEqual({ provider: "openai", model: "gpt-5" })
    expect("apiKey" in body).toBe(false)
  })

  test("a whitespace-only key counts as empty", () => {
    expect(buildConnectBody({ provider: "openai", model: "gpt-5", apiKey: "   " })).toEqual({
      provider: "openai",
      model: "gpt-5",
    })
  })

  test("a typed key is carried through", () => {
    expect(
      buildConnectBody({ provider: "openai", model: "gpt-5", apiKey: " sk-1 " }),
    ).toEqual({ provider: "openai", model: "gpt-5", apiKey: "sk-1" })
  })

  test("only the generic provider carries an endpoint", () => {
    expect(
      buildConnectBody({
        provider: "generic",
        model: "local-model",
        baseUrl: " https://api.example.com/v1 ",
      }),
    ).toEqual({
      provider: "generic",
      model: "local-model",
      baseUrl: "https://api.example.com/v1",
    })
    expect(
      buildConnectBody({
        provider: "openai",
        model: "gpt-5",
        baseUrl: "https://ignored.example.com/v1",
      }),
    ).toEqual({ provider: "openai", model: "gpt-5" })
  })
})
