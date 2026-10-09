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
import { Show, createSignal, onMount } from "solid-js"
import { CommandPalette } from "./component/command-palette"
import { DialogHelp } from "./component/dialog-help"
import { DialogModels, type ModelRow } from "./component/dialog-models"
import { DialogSessions } from "./component/dialog-sessions"
import { DialogSkills } from "./component/dialog-skills"
import { DialogStatus } from "./component/dialog-status"
import { type PermissionRequest } from "./component/permission"
import type { SessionTab } from "./component/session-tabs"
import { ToastProvider } from "./component/toast"
import { DialogProvider, useDialog } from "./dialog/context"
import type { IpcClient } from "./ipc"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import { loadSession } from "./state/replay"
import { initialState, type SessionState } from "./state/session"
import { theme } from "./theme/tokens"
import type { PermissionDecision } from "./util/permission"

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
  const [sessions, setSessions] = createSignal<SessionTab[]>([])
  const [permission, setPermission] = createSignal<PermissionRequest | null>(null)
  // The pending ``approval.request``'s resolver; the panel's answer resolves it.
  let resolvePermission: ((decision: PermissionDecision) => void) | null = null

  const decidePermission = (decision: PermissionDecision) => {
    const resolve = resolvePermission
    resolvePermission = null
    setPermission(null)
    resolve?.(decision)
  }

  const refreshSessions = async () => {
    try {
      const data = (await props.client.request("session.list")) as { sessions?: SessionTab[] }
      setSessions(data.sessions ?? [])
    } catch {
      /* the tabs keep their last list; the next turn retries */
    }
  }

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

  const switchTo = (id: string, state: SessionState) => {
    setSessionId(id)
    setHydrate({ key: id, state })
    setPendingPrompt("") // a switched session must not auto-send a stale home prompt
    setRoute("session")
  }

  const onSessionLoaded = (id: string, state: SessionState) => switchTo(id, state)

  const onSelectSession = async (id: string) => {
    try {
      switchTo(id, await loadSession(props.client, id))
    } catch {
      /* a failed load leaves the current session in place */
    }
  }

  const onNewSession = async () => {
    try {
      const created = (await props.client.request("session.create", {})) as { id: string }
      switchTo(created.id, initialState())
      void refreshSessions()
    } catch {
      /* ignore: the current session stays */
    }
  }

  const currentTitle = () => {
    const row = sessions().find((session) => session.id === sessionId())
    return row?.title || row?.preview || "New session"
  }

  // The session from initialize is not persisted until its first turn, so the
  // current one is synthesised into the strip.  The strip keeps only the most
  // recent few (OpenCode overflows/scrolls a 1800-line rail; M4 caps instead).
  const tabs = () => {
    const list = sessions()
    const recent = list.slice(0, 5)
    if (recent.some((session) => session.id === sessionId())) return recent
    return [{ id: sessionId(), title: "New session", preview: "" }, ...recent.slice(0, 4)]
  }

  const cycleSession = (delta: number) => {
    const list = tabs()
    if (list.length === 0) return
    const index = list.findIndex((session) => session.id === sessionId())
    const target = list[(((index < 0 ? 0 : index) + delta) % list.length + list.length) % list.length]
    if (target) void onSelectSession(target.id)
  }

  /** Jump to a tab by its strip index — the `<leader>1..5` quick slots. */
  const selectTab = (index: number) => {
    const target = tabs()[index]
    if (target) void onSelectSession(target.id)
  }

  // OpenCode binds `<leader>1..5` to tab select (config/keybind.ts:150); the
  // strip shows at most five tabs, so five quick slots cover it.
  const tabSelects = [0, 1, 2, 3, 4].map((index) => ({
    name: `session.tab.select.${index + 1}`,
    title: `Tab ${index + 1}`,
    group: "Session",
    desc: `Switch to session tab ${index + 1}`,
    run: () => selectTab(index),
  }))

  onMount(() => {
    void refreshSessions()
    // M5: the backend asks WARN-tier approvals as a server-initiated request;
    // answering it IS the JSON-RPC response, so the panel just resolves this.
    props.client.onRequest("approval.request", (params) => {
      // A newer request supersedes a stale panel (a timed-out one whose
      // backend already moved on): resolve the old waiter fail-closed.
      resolvePermission?.("no")
      return new Promise<PermissionDecision>((resolve) => {
        resolvePermission = resolve
        setPermission({
          toolName: String(params["toolName"] ?? ""),
          params: (params["params"] as Record<string, unknown> | undefined) ?? {},
          reason: String(params["reason"] ?? ""),
        })
      })
    })
    // A finished turn settles any pending approval; the backend has moved on.
    props.client.onEvent((event) => {
      if (event.type === "turn.finished" && permission() !== null) decidePermission("no")
    })
  })

  const startSession = (text: string) => {
    setPendingPrompt(text)
    setRoute("session")
  }

  useBindings(() => ({
    enabled: !dialog.isOpen() && permission() === null,
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
        name: "session.new",
        title: "New session",
        group: "Session",
        desc: "Start a fresh session",
        palette: true,
        run: () => void onNewSession(),
      },
      {
        name: "session.next",
        title: "Next session",
        group: "Session",
        desc: "Switch to the next tab",
        palette: true,
        run: () => cycleSession(1),
      },
      {
        name: "session.prev",
        title: "Previous session",
        group: "Session",
        desc: "Switch to the previous tab",
        palette: true,
        run: () => cycleSession(-1),
      },
      ...tabSelects,
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
      // OpenCode's leader bindings (config/keybind.ts:48,109,110,102,150,164).
      { key: "<leader>m", cmd: "dialog.models.show" },
      { key: "<leader>l", cmd: "dialog.sessions.show" },
      { key: "<leader>n", cmd: "session.new" },
      { key: "<leader>s", cmd: "dialog.status.show" },
      { key: "<leader>q", cmd: "app.exit" },
      ...tabSelects.map((command, index) => ({ key: `<leader>${index + 1}`, cmd: command.name })),
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
        <ToastProvider>
          <Session
            client={props.client}
            sessionId={sessionId()}
            model={modelName()}
            provider={providerName()}
            initialPrompt={pendingPrompt()}
            hydrate={hydrate()}
            sessions={tabs()}
            title={currentTitle()}
            workspace={props.hello.workspace}
            branch={props.hello.vcs?.branch ?? ""}
            version={props.hello.version}
            permission={permission()}
            onPermissionDecide={decidePermission}
            onSelectSession={(id) => void onSelectSession(id)}
            onNewSession={() => void onNewSession()}
            onTurnFinished={() => void refreshSessions()}
          />
        </ToastProvider>
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
