/**
 * Root component: route state (home <-> session), global keys, initial hello.
 */

import { useKeyboard } from "@opentui/solid"
import { Show, createSignal } from "solid-js"
import type { IpcClient } from "./ipc"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import { theme } from "./theme/tokens"

export interface Hello {
  version: string
  workspace: string
  model: string
  sessionId: string
}

export function App(props: { client: IpcClient; hello: Hello }) {
  const [route, setRoute] = createSignal<"home" | "session">("home")
  const [sessionId, setSessionId] = createSignal(props.hello.sessionId)
  const [pendingPrompt, setPendingPrompt] = createSignal("")

  useKeyboard((key) => {
    // OpenCode: ctrl+c exits from anywhere (prompt clear lands later).
    if (key.ctrl && key.name === "c") {
      void props.client.request("shutdown").finally(() => {
        props.client.close()
        process.exit(0)
      })
    }
  })

  const startSession = (text: string) => {
    setPendingPrompt(text)
    setRoute("session")
  }

  return (
    <box width="100%" height="100%" backgroundColor={theme.backgroundBase}>
      <Show
        when={route() === "session"}
        fallback={
          <Home
            workspace={props.hello.workspace}
            version={props.hello.version}
            model={props.hello.model}
            onSubmit={startSession}
          />
        }
      >
        <Session
          client={props.client}
          sessionId={sessionId()}
          model={props.hello.model}
          initialPrompt={pendingPrompt()}
        />
      </Show>
    </box>
  )
}
