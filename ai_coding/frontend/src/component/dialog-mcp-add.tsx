/**
 * Guided "connect an MCP server" (M5).
 *
 * One screen, three steps: kind, the fields that kind asks for, and the one
 * permission question that cannot be defaulted.  Arrows and enter belong to the
 * keymap; the input takes only what is typed into it and is never written to,
 * which is the arrangement `dialog-select` already ships for its search line.
 * Every choice here was made by measuring against the real TUI — the reasoning
 * is on the input itself, where it applies.
 *
 * Nothing in this file knows how a server is connected: it collects the shape
 * the config wants and hands it to `mcp.add`, which owns connecting (adopt on
 * the live manager, persist to the sidecar).  OAuth is deliberately not a
 * question — a remote server that answers 401 goes to needs_auth and its row
 * signs in (M4), the same flow either way.
 */

import { For, createSignal, onMount, Show } from "solid-js"
import { TextAttributes, type InputRenderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import type { IpcClient } from "../ipc"
import { mcpAddBody } from "../util/mcp"
import { theme } from "../theme/tokens"

type Kind = "local" | "remote"
type Step = "kind" | "field" | "trust"

interface Field {
  key: "name" | "command" | "url" | "token"
  label: string
  placeholder: string
  /** A secret: shown as typed, never echoed back into a later step. */
  secret?: boolean
}

const FIELDS: Record<Kind, Field[]> = {
  local: [
    { key: "name", label: "Name (the tools will be <name>__<tool>)", placeholder: "fs" },
    { key: "command", label: "Command and arguments", placeholder: "python -m my_mcp_server" },
  ],
  remote: [
    { key: "name", label: "Name (the tools will be <name>__<tool>)", placeholder: "github" },
    { key: "url", label: "Endpoint URL", placeholder: "https://api.githubcopilot.com/mcp/" },
    // Optional on purpose: a server that speaks OAuth still wants this left
    // blank, and one that refuses dynamic client registration — GitHub's,
    // by its own documentation — cannot be reached any other way.
    {
      key: "token",
      label: "API token (optional — a server that wants a token in the Authorization header)",
      placeholder: "ghp_…   leave blank if the server does its own login",
      secret: true,
    },
  ],
}

const KINDS: { value: Kind; title: string; hint: string }[] = [
  { value: "local", title: "local — run a command on this machine", hint: "npx / uvx / python -m: a stdio server" },
  { value: "remote", title: "remote — a server over HTTP", hint: "a hosted endpoint; sign in when it asks" },
]

const TRUST = [
  { value: false, title: "no — ask me every time (default)", hint: "each call of this server's tools prompts" },
  { value: true, title: "yes — skip the prompts for this server", hint: "writes auto_approve; the deny list still applies" },
]

export function DialogMcpAdd(props: { client: IpcClient; onDone: () => void }) {
  const [kind, setKind] = createSignal<Kind | null>(null)
  const [step, setStep] = createSignal<Step>("kind")
  const [cursor, setCursor] = createSignal(0)
  const [fieldIndex, setFieldIndex] = createSignal(0)
  const [values, setValues] = createSignal<Partial<Record<Field["key"], string>>>({})
  const [typed, setTyped] = createSignal("")
  const [error, setError] = createSignal("")
  // Asked before anything else: `mcp.enabled: false` is the shipped default,
  // and the backend's `adopt` refuses at the *end* of the wizard.  Collecting
  // a kind, a name, a command and a trust answer first is four questions the
  // operator could have been spared.
  const [checked, setChecked] = createSignal(false)
  const [enabled, setEnabled] = createSignal(true)
  let input: InputRenderable | undefined

  onMount(() => {
    void (async () => {
      try {
        const listed = (await props.client.request("mcp.list")) as { enabled?: boolean }
        setEnabled(listed.enabled !== false)
      } catch {
        // An unreadable switch is not a reason to block the wizard; `mcp.add`
        // is still the authority and will refuse with the same sentence.
      }
      setChecked(true)
    })()
  })

  const field = (): Field | null => {
    const k = kind()
    return k ? FIELDS[k][fieldIndex()] : null
  }

  useBindings(() => ({
    enabled: true,
    // Escape is the dialog provider's (context.tsx binds it to close), so there
    // is no cancel command here.  Enter belongs to the keymap on every step and
    // the input has no onSubmit at all, which is exactly ``dialog-select``'s
    // arrangement for its search line — an input that handles enter itself lost
    // the leading keystrokes of every field, ".venv/bin" arriving on screen as
    // "env/bin" while the whole value was still what got submitted.
    commands: [
      { name: "mcp.add.up", desc: "Previous", run: () => setCursor((c) => Math.max(0, c - 1)) },
      { name: "mcp.add.down", desc: "Next", run: () => setCursor((c) => Math.min(1, c + 1)) },
      { name: "mcp.add.choose", desc: "Choose", run: submit },
    ],
    bindings: [
      { key: "up", cmd: "mcp.add.up" },
      { key: "down", cmd: "mcp.add.down" },
      { key: "return", cmd: "mcp.add.choose" },
    ],
  }))

  const submit = () => {
    if (step() === "kind") pickKind()
    else if (step() === "trust") void pickTrust()
    else submitField()
  }

  const pickKind = () => {
    const chosen = KINDS[cursor()]
    setKind(chosen.value)
    setCursor(0)
    setTyped("")
    setStep("field")
  }

  const submitField = () => {
    const current = field()
    if (!current) return
    const value = typed().trim()
    if (!value) return
    setValues((existing) => ({ ...existing, [current.key]: value }))
    setTyped("")
    if (fieldIndex() + 1 < FIELDS[kind()!].length) {
      setFieldIndex(fieldIndex() + 1)
    } else {
      setCursor(0)
      setStep("trust")
    }
  }

  const nameInvalid = (): boolean => {
    const name = (values().name ?? "").trim()
    return name.length > 0 && !/^[A-Za-z0-9_-]+$/.test(name)
  }

  const pickTrust = async () => {
    const answers = values()
    const k = kind()!
    const body = mcpAddBody(k, answers, TRUST[cursor()].value)
    if (nameInvalid()) {
      setError("the name may only hold letters, digits, - and _")
      setFieldIndex(0)
      setStep("field")
      return
    }
    try {
      const result = (await props.client.request("mcp.add", body)) as {
        ok?: boolean
        reason?: string
      }
      if (result.ok === false) {
        setError(result.reason ?? "the server was refused")
        return
      }
      // The row lands on the panel behind this dialog and the status events
      // drive it from connecting onwards, so there is nothing to poll.
      props.onDone()
    } catch (cause) {
      setError(String(cause))
    }
  }

  if (checked() && !enabled()) {
    return (
      <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} gap={1}>
        <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
          Connect an MCP server
        </text>
        <text fg={theme.feedbackWarning}>
          MCP is switched off — nothing you add here could run.
        </text>
        <text fg={theme.textMuted}>
          Set `mcp.enabled: true` in your config file, restart, and come back.
        </text>
        <text fg={theme.textMuted}>esc close</text>
      </box>
    )
  }

  return (
    <box flexDirection="column" width="100%" paddingLeft={4} paddingRight={4} gap={1}>
      <text fg={theme.textBase} attributes={TextAttributes.BOLD}>
        {step() === "kind"
          ? "Connect an MCP server — what kind?"
          : step() === "field"
            ? `Connect an MCP server — ${kind()} (${fieldIndex() + 1}/${FIELDS[kind()!].length})`
            : "Trust this server's tools?"}
      </text>

      <Show when={step() === "kind" || step() === "trust"}>
        <For each={step() === "kind" ? KINDS : TRUST}>
          {(option, index) => (
            <box flexDirection="column">
              <text
                fg={index() === cursor() ? theme.textBase : theme.textMuted}
                attributes={index() === cursor() ? TextAttributes.BOLD : undefined}
              >
                {`${index() === cursor() ? "▸" : " "} ${option.title}`}
              </text>
              <Show when={index() === cursor()}>
                <text fg={theme.textMuted}>{`    ${option.hint}`}</text>
              </Show>
            </box>
          )}
        </For>
      </Show>

      <Show when={step() === "field"}>
        <text fg={theme.textBase}>{`\u203a ${field()!.label}`}</text>
      </Show>

      {/* One input per field, and nothing ever writes to it: its text is only
          what the operator typed, and the next field gets a new element rather
          than a cleared one.  Writing to a live input (``value``,
          ``deleteLine()``) leaves it rendering text it no longer holds, and
          setting ``focused`` as a prop rather than focusing it the way
          ``dialog-select``'s search line is focused had the same effect — both
          measured against the real TUI, both on a value the backend then stored
          in full.  ``keyed`` on the field is what makes it fresh.

          It is deliberately a direct child of the column, not a row beside a
          "\u203a " marker: a row box laid the input out narrower than its text
          and the line painted short.

          Known and not ours: a long value typed at a fast clip still paints its
          first characters short — the palette's own search box does the same
          with the same string, so it is opentui's input repaint, not this
          dialog.  What gets submitted and stored is the whole value. */}
      <Show when={step() === "field" ? field() : null} keyed>
        {(current) => (
          <input
            ref={(element: InputRenderable) => {
              input = element
              element.focus()
            }}
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
        {step() === "field"
          ? "enter next · esc cancel"
          : "↑/↓ choose · enter next · esc cancel"}
      </text>
      <Show when={error()}>
        <text fg={theme.feedbackError}>{error()}</text>
      </Show>
    </box>
  )
}
