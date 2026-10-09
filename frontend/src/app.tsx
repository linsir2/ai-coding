/**
 * Root component (M3): route state, the dialog host, and the global command
 * layer.
 *
 * DialogProvider owns the overlay; this shell owns the app signals a dialog
 * can change (model, session, hydration) and registers every command with its
 * palette metadata.  The base layer is gated on `!dialog.isOpen()` so a dialog
 * swallows esc/ctrl+c/ctrl+t instead of the app acting on them.
 */

import type { CliRenderer } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { Show, createSignal } from "solid-js"
import { CommandPalette } from "./component/command-palette"
import { DialogHelp } from "./component/dialog-help"
import { DialogModels, type ModelRow } from "./component/dialog-models"
import { DialogSessions } from "./component/dialog-sessions"
import { DialogSkills } from "./component/dialog-skills"
import { DialogStatus } from "./component/dialog-status"
import { DialogProvider, useDialog } from "./dialog/context"
import type { IpcClient } from "./ipc"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import type { SessionState } from "./state/session"
import { theme } from "./theme/tokens"

export interface Hello {
  version: string
  workspace: string
  model: string
  modelKey: string
  provider: string
  vcs: { branch: string }
  sessionId: string
  sessionTitle: string
  models: ModelRow[]
}

function Shell(props: { client: IpcClient; hello: Hello }) {
  const dialog = useDialog()
  const [route, setRoute] = createSignal<"home" | "session">("home")
  const [sessionId, setSessionId] = createSignal(props.hello.sessionId)
  const [pendingPrompt, setPendingPrompt] = createSignal("")
  const [modelName, setModelName] = createSignal(props.hello.model)
  const [providerName, setProviderName] = createSignal(props.hello.provider)
  const [modelKey, setModelKey] = createSignal(props.hello.modelKey)
  const [hydrate, setHydrate] = createSignal<{ key: string; state: SessionState } | null>(null)

  const switchModel = async (key: string, name: string): Promise<boolean> => {
    try {
      const result = (await props.client.request("model.set", { name: key })) as { provider?: string }
      setModelKey(key)
      setModelName(name)
      if (result.provider) setProviderName(result.provider)
      return true
    } catch {
      return false
    }
  }

  const onSessionLoaded = (id: string, state: SessionState) => {
    setSessionId(id)
    setHydrate({ key: id, state })
    setPendingPrompt("") // a loaded session must not auto-send a stale home prompt
    setRoute("session")
  }

  const startSession = (text: string) => {
    setPendingPrompt(text)
    setRoute("session")
  }

  useBindings(() => ({
    enabled: !dialog.isOpen(),
    commands: [
      {
        name: "command.palette.show",
        title: "Commands",
        group: "System",
        desc: "List available commands",
        run: () => dialog.open(() => <CommandPalette />),
      },
      {
        name: "dialog.models.show",
        title: "Switch model",
        group: "Model",
        desc: "Change the active model",
        palette: true,
        run: () =>
          dialog.open(() => (
            <DialogModels models={props.hello.models} currentKey={modelKey()} onSelect={switchModel} />
          )),
      },
      {
        name: "dialog.sessions.show",
        title: "Switch session",
        group: "Session",
        desc: "Open a saved session",
        palette: true,
        run: () =>
          dialog.open(
            () => <DialogSessions client={props.client} currentId={sessionId()} onSelect={onSessionLoaded} />,
            "large",
          ),
      },
      {
        name: "dialog.skills.show",
        title: "Skills",
        group: "Skill",
        desc: "List available skills",
        palette: true,
        run: () => dialog.open(() => <DialogSkills client={props.client} />, "large"),
      },
      {
        name: "dialog.status.show",
        title: "Status",
        group: "System",
        desc: "Show session and runtime status",
        palette: true,
        run: () =>
          dialog.open(() => (
            <DialogStatus
              client={props.client}
              sessionTitle={props.hello.sessionTitle}
              sessionId={sessionId()}
              provider={providerName()}
              model={modelName()}
              workspace={props.hello.workspace}
            />
          )),
      },
      {
        name: "dialog.help.show",
        title: "Help",
        group: "System",
        desc: "Show commands and keybindings",
        palette: true,
        run: () => dialog.open(() => <DialogHelp />, "large"),
      },
      {
        name: "app.exit",
        title: "Exit",
        group: "System",
        desc: "Exit the app",
        palette: true,
        run: () => {
          void props.client.request("shutdown").finally(() => {
            props.client.close()
            process.exit(0)
          })
        },
      },
    ],
    bindings: [
      { key: "ctrl+p", cmd: "command.palette.show" },
      { key: "ctrl+c", cmd: "app.exit" },
    ],
  }))

  return (
    <box width="100%" height="100%" backgroundColor={theme.backgroundBase}>
      <Show
        when={route() === "session"}
        fallback={
          <Home
            workspace={props.hello.workspace}
            version={props.hello.version}
            model={modelName()}
            provider={providerName()}
            branch={props.hello.vcs?.branch ?? ""}
            onSubmit={startSession}
          />
        }
      >
        <Session
          client={props.client}
          sessionId={sessionId()}
          model={modelName()}
          provider={providerName()}
          initialPrompt={pendingPrompt()}
          hydrate={hydrate()}
        />
      </Show>
    </box>
  )
}

export function App(props: { client: IpcClient; hello: Hello; renderer: CliRenderer }) {
  return (
    <DialogProvider renderer={props.renderer}>
      <Shell client={props.client} hello={props.hello} />
    </DialogProvider>
  )
}
