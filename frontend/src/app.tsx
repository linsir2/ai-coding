/**
 * Root component: route state (home <-> session), global keymap layer.
 *
 * Global commands live here (M0.5: just app.exit); more commands land with
 * the command palette (M3) and the full keybinding table (M5).
 */

import { useBindings } from "@opentui/keymap/solid"
import { Show, createSignal } from "solid-js"
import type { IpcClient } from "./ipc"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import { theme } from "./theme/tokens"

export interface Hello {
  version: string
  workspace: string
  model: string
  provider: string
  vcs: { branch: string }
  sessionId: string
}

export function App(props: { client: IpcClient; hello: Hello }) {
  const [route, setRoute] = createSignal<"home" | "session">("home")
  const [sessionId, setSessionId] = createSignal(props.hello.sessionId)
  const [pendingPrompt, setPendingPrompt] = createSignal("")

  useBindings(() => ({
    commands: [
      {
        name: "app.exit",
        desc: "Exit",
        run: () => {
          void props.client.request("shutdown").finally(() => {
            props.client.close()
            process.exit(0)
          })
        },
      },
    ],
    bindings: [{ key: "ctrl+c", cmd: "app.exit" }],
  }))

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
            provider={props.hello.provider}
            branch={props.hello.vcs?.branch ?? ""}
            onSubmit={startSession}
          />
        }
      >
        <Session
          client={props.client}
          sessionId={sessionId()}
          model={props.hello.model}
          provider={props.hello.provider}
          initialPrompt={pendingPrompt()}
        />
      </Show>
    </box>
  )
}
