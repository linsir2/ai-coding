/**
 * Session route (M0): transcript + streaming + composer.
 *
 * Message rows follow OpenCode's grammar:
 *   user       -> left ┃ border in agent color, raised background
 *   assistant  -> markdown/text padded left 3 (no border)
 *   streaming  -> an assistant row fed by text.delta events
 *
 * The event contract is the M0 server's echo turn; M2 swaps the backend for
 * the real AgentLoop without changing any of this file.
 */

import type { ScrollBoxRenderable } from "@opentui/core"
import { For, createSignal, onCleanup, onMount } from "solid-js"
import type { IpcClient } from "../ipc"
import { Prompt } from "../component/prompt"
import { theme } from "../theme/tokens"

interface ChatLine {
  role: "user" | "assistant"
  text: string
}

const USER_BORDER = {
  topLeft: "",
  topRight: "",
  bottomRight: "",
  horizontal: "▀",
  vertical: "┃",
  bottomLeft: "╹",
  topT: "",
  bottomT: "",
  leftT: "",
  rightT: "",
  cross: "",
}

export interface SessionProps {
  client: IpcClient
  sessionId: string
  model: string
  /** Text submitted from home; auto-sent once the route mounts. */
  initialPrompt?: string
}

export function Session(props: SessionProps) {
  const [lines, setLines] = createSignal<ChatLine[]>([])
  const [streaming, setStreaming] = createSignal("")
  const [busy, setBusy] = createSignal(false)
  let transcript: ScrollBoxRenderable | undefined

  onMount(() => {
    const unsubscribe = props.client.onEvent((event) => {
      switch (event.type) {
        case "turn.start":
          setBusy(true)
          break
        case "text.delta":
          setStreaming((current) => current + String(event.data["text"] ?? ""))
          break
        case "turn.finish": {
          const finalText = String(event.data["text"] ?? "")
          const streamed = streaming()
          setLines((current) => [
            ...current,
            { role: "assistant", text: finalText || streamed },
          ])
          setStreaming("")
          setBusy(false)
          break
        }
      }
      transcript?.scrollTo(transcript?.scrollHeight ?? 0)
    })
    onCleanup(unsubscribe)
    if (props.initialPrompt) {
      void submit(props.initialPrompt)
    }
  })

  const submit = async (text: string) => {
    setLines((current) => [...current, { role: "user", text }])
    try {
      await props.client.request("session.prompt", {
        id: props.sessionId,
        text,
      })
    } catch (error) {
      setLines((current) => [
        ...current,
        { role: "assistant", text: `[error] ${error}` },
      ])
      setBusy(false)
    }
  }

  return (
    <box flexDirection="column" width="100%" height="100%">
      <scrollbox
        ref={(element: ScrollBoxRenderable) => (transcript = element)}
        flexGrow={1}
        stickyScroll
        stickyStart="bottom"
        paddingLeft={2}
        paddingRight={2}
        paddingTop={1}
      >
        <For each={lines()}>
          {(line) =>
            line.role === "user" ? (
              <box
                marginTop={1}
                border={["left"]}
                customBorderChars={USER_BORDER}
                borderColor={theme.agentColor}
                backgroundColor={theme.backgroundRaised}
                paddingLeft={2}
                paddingRight={2}
                paddingTop={0}
                paddingBottom={0}
              >
                <text fg={theme.textBase}>{line.text}</text>
              </box>
            ) : (
              <box marginTop={1} paddingLeft={3}>
                <text fg={theme.textBase}>{line.text}</text>
              </box>
            )
          }
        </For>
        {streaming() ? (
          <box marginTop={1} paddingLeft={3}>
            <text fg={theme.textBase}>{streaming()}</text>
          </box>
        ) : null}
        <box height={1} />
      </scrollbox>
      <box paddingLeft={2} paddingRight={2} paddingBottom={1}>
        <Prompt
          placeholder={busy() ? "Working…" : 'Ask anything…'}
          metadata={`Build · ${props.model || "no model"}${busy() ? " · working" : ""}`}
          onSubmit={submit}
          autofocus={!busy()}
        />
      </box>
    </box>
  )
}
