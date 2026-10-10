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
import { AskPanel } from "./component/ask"
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
import type { AskRequest } from "./util/ask"
import { quitSequence } from "./util/exit"
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

function Shell(props: { client: IpcClient; hello: Hello; renderer: CliRenderer }) {
  const dialog = useDialog()
  // One exit path (I5): the socket closes, the renderer is torn down so the
  // terminal leaves raw mode, then the process goes.  `/quit` from a command,
  // ctrl+c, and a dropped backend connection all land here.
  const exitWith = (message?: string) =>
    quitSequence({
      close: () => props.client.close(),
      destroy: () => props.renderer.destroy(),
      exit: () => process.exit(0),
      error: (line) => console.error(line),
    }, message ? { message } : undefined)
  const requestExit = () => {
    // Ask the backend to stop first; it may already be gone (a command's
    // `/quit`), and the finally keeps the sequence running either way.
    void props.client
      .request("shutdown")
      .catch(() => undefined)
      .finally(() => exitWith())
  }
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

  const [ask, setAsk] = createSignal<AskRequest | null>(null)
  // The pending ``user_prompt.ask``'s resolver; the panel's answer resolves it.
  let resolveAsk: ((answer: string) => void) | null = null

  const answerAsk = (answer: string) => {
    const resolve = resolveAsk
    resolveAsk = null
    setAsk(null)
    resolve?.(answer)
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

  // J6, client half: the server moved the live session when it was deleted, and
  // the replacement is empty — same shape as a freshly created one.
  const onSessionDeleted = async (deleted: string, next: string) => {
    const wasLive = sessionId() === deleted // read before switchTo moves it
    if (next) switchTo(next, initialState())
    void refreshSessions()
    if (wasLive) dialog.close()
  }

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
    // The other direction: the model's own question (ask_user tool).
    // Answering it IS the JSON-RPC response, so the panel just resolves this.
    props.client.onRequest("user_prompt.ask", (params) => {
      // A newer question supersedes a stale panel; the old waiter gets the
      // empty answer so the model decides itself.
      resolveAsk?.("")
      return new Promise<string>((resolve) => {
        resolveAsk = resolve
        setAsk({
          question: String(params["question"] ?? ""),
          options: Array.isArray(params["options"])
            ? params["options"].filter((option): option is string => typeof option === "string")
            : [],
        })
      })
    })
    // A finished turn settles any pending approval or question; the backend
    // has moved on (the tool result says "nobody answered").
    props.client.onEvent((event) => {
      if (event.type === "connection.closed") {
        // The backend is gone (crash, external kill).  Our own close() sets the
        // client's `closed` flag, so this only fires for an unexpected drop.
        exitWith("ai-coding: backend connection closed")
        return
      }
      if (event.type !== "turn.finished") return
      if (permission() !== null) decidePermission("no")
      if (ask() !== null) answerAsk("")
      // `/quit` as a command: the backend already stopped on its side, so the
      // socket is closing — leave through the same sequence (I5).
      if (event.data["action"] === "quit") exitWith()
    })
  })

  const startSession = (text: string) => {
    setPendingPrompt(text)
    setRoute("session")
  }

  useBindings(() => ({
    enabled: !dialog.isOpen() && permission() === null && ask() === null,
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
            () => (
              <DialogSessions
                client={props.client}
                currentId={sessionId()}
                onSelect={onSessionLoaded}
                onDeleted={(_deleted, next) => void onSessionDeleted(_deleted, next)}
              />
            ),
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
        // The session route registers its own (closer) versions of these two,
        // which run them in the live session.  On Home there is no session
        // layer: start one with the command as its first prompt — the
        // session's dispatch routes a "/"-prefixed line to session.command,
        // the backend's command dispatcher.
        name: "session.plan",
        title: "Plan",
        group: "Session",
        desc: "Plan a topic and execute it step by step",
        palette: true,
        run: () => startSession("/plan "),
      },
      {
        name: "session.review",
        title: "Review",
        group: "Session",
        desc: "Review the current git diff",
        palette: true,
        run: () => startSession("/review"),
      },
      {
        name: "session.permissions",
        title: "Permissions",
        group: "Session",
        desc: "Show or switch the approval policy",
        palette: true,
        run: () => startSession("/permissions "),
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
        run: () => requestExit(),
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
            ask={ask()}
            onAskAnswer={answerAsk}
            onQuit={exitWith}
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
      <Shell client={props.client} hello={props.hello} renderer={props.renderer} />
    </DialogProvider>
  )
}
