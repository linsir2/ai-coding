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
import { DialogMcp } from "./component/dialog-mcp"
import { DialogMcpAdd } from "./component/dialog-mcp-add"
import { DialogMemory } from "./component/dialog-memory"
import { DialogModels, type ModelRow } from "./component/dialog-models"
import { DialogSessions } from "./component/dialog-sessions"
import { DialogSkillPending } from "./component/dialog-skill-pending"
import { DialogPermissions } from "./component/dialog-permissions"
import { DialogStatus } from "./component/dialog-status"
import { type PermissionRequest } from "./component/permission"

import { ToastProvider } from "./component/toast"
import { DialogProvider, useDialog } from "./dialog/context"
import type { MentionOption } from "./util/mention"
import type { IpcClient } from "./ipc"
import { Home } from "./routes/home"
import { Session } from "./routes/session"
import { loadSession } from "./state/replay"
import { initialRoute, resumeNotice, withResumeNotice } from "./util/boot"
import { initialState, type SessionState } from "./state/session"
import { theme } from "./theme/tokens"
import { parseAskRequest, type AskRequest } from "./util/ask"
import { quitSequence } from "./util/exit"
import type { PermissionDecision } from "./util/permission"
import type { PromptImage } from "./util/attachment"
import type { PendingPrompt } from "./util/submission"

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
  /** 可达+有痕批次: the backend consumed an armed session at startup. */
  resumed?: boolean
  /** The one note the operator must see about that resume (race-free). */
  resumeNote?: string
}

/** The session.list row, trimmed to what this shell reads off it. */
interface SessionRow {
  id: string
  title: string
  preview: string
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
  const [pendingPrompt, setPendingPrompt] = createSignal<PendingPrompt | null>(null)
  const [modelName, setModelName] = createSignal(props.hello.model)
  // M3: bumped on every mcp.status.changed, so an open MCP panel re-reads its
  // rows when a supervisor settles a server behind its back.
  const [mcpRevision, setMcpRevision] = createSignal(0)
  const [providerName, setProviderName] = createSignal(props.hello.provider)
  const [modelKey, setModelKey] = createSignal(props.hello.modelKey)
  const [hydrate, setHydrate] = createSignal<{ key: string; state: SessionState } | null>(null)
  const [sessions, setSessions] = createSignal<SessionRow[]>([])
  const [permission, setPermission] = createSignal<PermissionRequest | null>(null)
  // The pending ``approval.request``'s resolver; the panel's answer resolves it.
  let resolvePermission: ((decision: PermissionDecision) => void) | null = null

  // One dialog shows at a time and opening replaces what is there, so the
  // wizard hands the panel back rather than closing to the composer: the row
  // the add produced is the thing the operator wants to see (M5).
  const openMcpPanel = () =>
    dialog.open(
      () => (
        <DialogMcp
          client={props.client}
          revision={mcpRevision}
          onConnectNew={() =>
            dialog.open(
              () => <DialogMcpAdd client={props.client} onDone={openMcpPanel} />,
              "medium",
            )
          }
        />
      ),
      "large",
    )

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
      const data = (await props.client.request("session.list")) as { sessions?: SessionRow[] }
      setSessions(data.sessions ?? [])
    } catch {
      /* the sidebar title keeps its last value; the next turn retries */
    }
  }

  const [skills, setSkills] = createSignal<MentionOption[]>([])
  const [files, setFiles] = createSignal<MentionOption[]>([])
  const [commands, setCommands] = createSignal<string[]>([])
  // The composer's @ list. skill_review can add skills mid-session, so the
  // table is refreshed with the same cadence as the session list.  r17/T4:
  // the workspace's files ride the same list (skills first, files after).
  const refreshSkills = async () => {
    try {
      const data = (await props.client.request("skills.list")) as { skills?: MentionOption[] }
      setSkills(data.skills ?? [])
    } catch {
      /* the composer keeps the last list; the panel refetches on open */
    }
    try {
      const data = (await props.client.request("files.find", {})) as { files?: string[] }
      setFiles((data.files ?? []).map((name) => ({ name })))
    } catch {
      /* same: the last list survives a failed walk */
    }
    // r19/F5: the dispatcher's vocabulary, so a submission is classified as a
    // command by the one authority rather than by guessing at the slash.
    try {
      const data = (await props.client.request("slash.list", {})) as { commands?: string[] }
      setCommands(data.commands ?? [])
    } catch {
      /* an empty vocabulary means every line is treated as prompt text */
    }
  }

  // One definition each, shared by the palette command and the session header's
  // click: two copies of this JSX would drift (A4 §4 step 10).
  const openSessions = () =>
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
    )

  const openPermissions = () =>
    dialog.open(() => <DialogPermissions client={props.client} liveSessionId={sessionId()} />)

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
    setPendingPrompt(null) // a switched session must not auto-send a stale home prompt
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

  onMount(() => {
    void refreshSessions()
    void refreshSkills()
    // 可达+有痕批次: land on the resumed session.  The boot resume runs in
    // the backend; its approvals and transcript live in the session route,
    // so the shell opens it and renders the note the response carried --
    // with the session mounted, the run's live turns stream in as they did
    // before the restart.
    if (initialRoute(props.hello) === "session") {
      void (async () => {
        try {
          const state = await loadSession(props.client, props.hello.sessionId)
          switchTo(props.hello.sessionId, withResumeNotice(state, resumeNotice(props.hello)))
        } catch {
          /* an unloadable resume leaves the shell on Home */
        }
      })()
    }
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
        // 19 §3.2: one parser for every ask — the plan review's detail/intent
        // ride the same payload the generic panel ignores.
        setAsk(parseAskRequest(params))
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
      // M3/M4: a supervisor settling a server, or a sign-in finishing, changes
      // what the MCP panel should say, so the open dialog re-reads its rows.
      if (event.type === "mcp.status.changed" || event.type === "mcp.login.finished") {
        setMcpRevision((n) => n + 1)
        return
      }
      if (event.type !== "turn.finished") return
      void refreshSkills()
      if (permission() !== null) decidePermission("no")
      if (ask() !== null) answerAsk("")
      // `/quit` as a command: the backend already stopped on its side, so the
      // socket is closing — leave through the same sequence (I5).
      if (event.data["action"] === "quit") exitWith()
    })
  })

  const startSession = (text: string, images: PromptImage[] = [], mode: "normal" | "plan" = "normal") => {
    // r18/F13: Home is a second submit entry, so it has to carry the images the
    // way Session does.  Home's onSubmit used to take one argument, Prompt
    // passed two, and the second was dropped without a word.
    // 20 §批次1: the mode the Home row showed rides along; the Session route
    // lands the posture (`planPrelude`) before it sends the prompt.
    setPendingPrompt({ text, images, mode })
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
        run: () => openSessions(),
      },
      {
        // Read-only by design: MCP is connected once at startup, so there is
        // nothing for a reconnect button to call (MCP-MCP §6).
        name: "mcp.list",
        title: "MCP servers",
        group: "Agent", // OpenCode's own category for this command (app.tsx:895)
        desc: "Show configured MCP servers and what came of them",
        palette: true,
        run: openMcpPanel,
      },
      {
        // M8: `skill_review` runs on a timer, by default on, and used to write
        // generated SKILL.md files into the source tree unattended. They now
        // wait here for a human.
        name: "dialog.skillPending.show",
        title: "Pending skills",
        group: "Skill",
        desc: "Review skills the agent generated and nobody has approved",
        palette: true,
        run: () =>
          dialog.open(() => <DialogSkillPending client={props.client} />, "large"),
      },
      {
        // M7: what the agent believes. `/memory` existed in the CLI as one line
        // of counts; here it is the panel, because a count is not something you
        // can act on.
        name: "dialog.memory.show",
        title: "Memory",
        group: "Agent",
        desc: "What the agent remembers, with pin and delete",
        palette: true,
        run: () => dialog.open(() => <DialogMemory client={props.client} />, "large"),
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
        // The session route registers its own (closer) version of /review,
        // which runs it in the live session.  On Home there is no session
        // layer: start one with the command as its first prompt — the
        // session's dispatch routes a "/"-prefixed line to session.command,
        // the backend's command dispatcher.  (Plan entry lives only in the
        // session now: 19 Q1=A keeps it un-advertised until a session exists.)
        name: "session.review",
        title: "Review",
        group: "Session",
        desc: "Review the current git diff",
        palette: true,
        run: () => startSession("/review"),
      },
      {
        // Workspace-global, so it is its own dialog at the app layer rather than
        // a panel inside the session route (A4 §2).  liveSessionId is only the
        // transport session.command needs; the policy itself is not per session.
        name: "dialog.permissions.show",
        title: "Permissions",
        group: "System",
        desc: "Show or switch the approval policy",
        palette: true,
        run: () => openPermissions(),
      },
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
      // ctrl+p is VS Code's quick open and it wins inside its terminal, so the
      // palette lives on the leader like every other dialog: <leader>p.
      { key: "<leader>p", cmd: "command.palette.show" },
      { key: "ctrl+c", cmd: "app.exit" },
      // OpenCode's leader bindings (config/keybind.ts:48,109,110,102,150,164).
      { key: "<leader>m", cmd: "dialog.models.show" },
      { key: "<leader>l", cmd: "dialog.sessions.show" },
      { key: "<leader>n", cmd: "session.new" },
      { key: "<leader>s", cmd: "dialog.status.show" },
      { key: "<leader>q", cmd: "app.exit" },
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
            client={props.client}
            skills={skills()}
            files={files()}
            commands={commands()}
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
            title={currentTitle()}
            workspace={props.hello.workspace}
            branch={props.hello.vcs?.branch ?? ""}
            version={props.hello.version}
            permission={permission()}
            onPermissionDecide={decidePermission}
            skills={skills()}
            files={files()}
            commands={commands()}
            ask={ask()}
            onAskAnswer={answerAsk}
            onQuit={exitWith}
            onSelectSession={(id) => void onSelectSession(id)}
            onOpenSessions={openSessions}
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
