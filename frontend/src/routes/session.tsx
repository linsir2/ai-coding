/**
 * Session route (M2/M4): real AgentLoop turns + the session chrome (tabs,
 * sidebar, status bar).
 *
 * Data flow, one direction only:
 *   IpcClient.onEvent -> reduce(state, event) -> createStore -> <MessageView>
 *
 * Local state beyond the reducer: `thinking` (ctrl+t) and `stats` (the real
 * token usage the sidebar/status bar show, refreshed on mount and after every
 * turn).  The chrome is fed by the app shell (sessions list, title, location).
 */

import type { ScrollBoxRenderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { useTerminalDimensions } from "@opentui/solid"
import { Index, Show, createEffect, createSignal, on, onCleanup, onMount } from "solid-js"
import { createStore } from "solid-js/store"
import { MessageView } from "../component/message"
import { AskPanel } from "../component/ask"
import { PermissionPanel, type PermissionRequest } from "../component/permission"
import { Prompt } from "../component/prompt"
import { SessionTabs, type SessionTab } from "../component/session-tabs"
import { Sidebar } from "../component/sidebar"
import { StatusBar } from "../component/status-bar"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { Action, SessionState } from "../state/session"
import { initialState, reduce } from "../state/session"
import { useToast } from "../component/toast"
import { theme } from "../theme/tokens"
import { escStep } from "../util/esc"
import { loadSession } from "../state/replay"
import type { AskRequest } from "../util/ask"
import type { PermissionDecision } from "../util/permission"

export interface SessionProps {
  client: IpcClient
  sessionId: string
  model: string
  provider: string
  /** Text submitted from home; auto-sent once the route mounts. */
  initialPrompt?: string
  /** A replayed session (from the dialog or tabs); replaces the transcript. */
  hydrate?: { key: string; state: SessionState } | null
  sessions: SessionTab[]
  title: string
  workspace: string
  branch: string
  version: string
  /** A pending WARN-tier approval; replaces the prompt while non-null (M5). */
  permission: PermissionRequest | null
  onPermissionDecide: (decision: PermissionDecision) => void
  ask: AskRequest | null
  onAskAnswer: (answer: string) => void
  /** Leave through the shell's exit sequence (a command's `/quit`, I5). */
  onQuit: () => void
  onSelectSession: (id: string) => void
  onNewSession: () => void
  /** The tab list can go stale after a turn; the shell refreshes it. */
  onTurnFinished: () => void
}

interface Stats {
  tokens: number
  maxContextTokens: number
}

export function Session(props: SessionProps) {
  const dialog = useDialog()
  const toast = useToast()
  const dimensions = useTerminalDimensions()
  const [state, setState] = createStore<SessionState>(initialState())
  const [thinking, setThinking] = createSignal(false)
  const [stats, setStats] = createSignal<Stats>({ tokens: 0, maxContextTokens: 0 })
  const [interruptArmed, setInterruptArmed] = createSignal(false)
  const [suggesting, setSuggesting] = createSignal(false)
  let transcript: ScrollBoxRenderable | undefined
  let interruptTimer: ReturnType<typeof setTimeout> | undefined

  const disarmInterrupt = () => {
    if (interruptTimer !== undefined) {
      clearTimeout(interruptTimer)
      interruptTimer = undefined
    }
    setInterruptArmed(false)
  }

  const apply = (action: Action) => {
    const next = reduce(state, action)
    if (next !== state) setState(next)
  }

  const refreshStats = async () => {
    try {
      setStats((await props.client.request("session.stats")) as Stats)
    } catch {
      /* the sidebar keeps the last known numbers; the next turn retries */
    }
  }

  const dispatch = (text: string) => {
    // A "/"-prefixed line is a slash command: the backend's dispatcher
    // (app/commands.py::handle_input) decides what it means, so the TUI gains
    // the whole command layer through one entry.  Unknown commands fall
    // through it as plain text — the same verdict the model would get either
    // way.  Plain text keeps the live-turn path (bridge deltas, no console).
    const method = text.startsWith("/") ? "session.command" : "session.prompt"
    void props.client.request(method, { id: props.sessionId, text }).catch((error) => {
      apply({ kind: "submit-failed", reason: String(error) })
    })
  }

  const submit = (text: string): boolean => {
    // The retired Textual UI queued a submit that arrived mid-turn and ran it
    // when the turn ended; replicate that here.
    if (state.busy) {
      apply({ kind: "queued", text })
      return true
    }
    apply({ kind: "submitted", text })
    dispatch(text)
    return true
  }

  useBindings(() => ({
    enabled: !dialog.isOpen() && !suggesting() && props.permission === null && props.ask === null,
    commands: [
      {
        name: "session.interrupt",
        title: "Interrupt turn",
        group: "Session",
        desc: "Stop the current turn",
        palette: true,
        run: () => {
          if (!state.busy) return
          const step = escStep(interruptArmed())
          setInterruptArmed(step.armed)
          if (!step.interrupt) {
            interruptTimer = setTimeout(() => setInterruptArmed(false), 5000)
            return
          }
          disarmInterrupt()
          void props.client.request("session.interrupt")
        },
      },
      {
        name: "session.toggle.thinking",
        title: "Toggle thinking",
        group: "Session",
        desc: "Show or hide reasoning",
        palette: true,
        run: () => setThinking((value) => !value),
      },
      {
        name: "session.plan",
        title: "Plan",
        group: "Session",
        desc: "Plan a topic and execute it step by step",
        palette: true,
        // Bare /plan needs a topic: pre-fill it and let the user type one.
        // The old TUI asked in a modal; asking in the composer keeps the
        // slash-list selection a single keystroke.
        run: () => setPromptSeed("/plan "),
      },
      {
        name: "session.review",
        title: "Review",
        group: "Session",
        desc: "Review the current git diff",
        palette: true,
        run: () => {
          if (state.busy) {
            apply({ kind: "queued", text: "/review" })
            return
          }
          apply({ kind: "submitted", text: "/review" })
          dispatch("/review")
        },
      },
      {
        // Bare /permissions would print the report; the interesting half is
        // the argument (auto|confirm|clear), so pre-fill the command and let
        // the operator type it — the same trick /plan uses for its topic.
        name: "session.permissions",
        title: "Permissions",
        group: "Session",
        desc: "Show or switch the approval policy",
        palette: true,
        run: () => setPromptSeed("/permissions "),
      },
    ],
    bindings: [
      { key: "escape", cmd: "session.interrupt" },
      { key: "ctrl+t", cmd: "session.toggle.thinking" },
    ],
  }))

  // The slash list's /plan pre-fills the composer (bare /plan asks for a
  // topic); the seed lands the text in the textarea and focuses it.
  const [promptSeed, setPromptSeed] = createSignal("")

  onMount(() => {
    const unsubscribe = props.client.onEvent((event) => {
      apply({ kind: "wire", event })
      if (event.type === "turn.finished") {
        disarmInterrupt()
        void refreshStats()
        props.onTurnFinished()
        // /clear and /resume <id> end with the REPL's NEW_SESSION action; the
        // backend has already switched the live session, so the tabs follow.
        const action = event.data["action"]
        if (action === "new_session") {
          const id = String(event.data["sessionId"] ?? "")
          if (id) props.onSelectSession(id)
        }
        if (action === "quit") props.onQuit()
        if (event.data["reload"] === true) {
          // The command rewrote the persisted history shorter (/compact):
          // the transcript is a projection of it, so re-project — the queued
          // line survives (I9) and the explanation rides a toast because the
          // "[compact] …" notice is wiped by the very re-projection (D1).
          void (async () => {
            const before = Number(event.data["messagesBefore"] ?? 0)
            const after = Number(event.data["messagesAfter"] ?? 0)
            const loaded = await loadSession(props.client, props.sessionId)
            setState({ ...loaded, queued: state.queued })
            void refreshStats()
            toast.show({
              title: "Compacted",
              message: `${before} → ${after} messages · transcript resynced`,
              variant: "info",
            })
          })()
        }
        // One queued line per finished turn.
        const next = state.queued[0]
        if (next !== undefined) {
          apply({ kind: "queued-dispatched" })
          dispatch(next)
        }
      }
      if (event.type === "provider.retried") {
        toast.show({
          title: "Retry",
          message: `Attempt ${String(event.data["attempt"] ?? "")}: ${String(event.data["reason"] ?? "")}`,
          variant: "warning",
        })
      }
      if (event.type === "provider.fallback") {
        toast.show({
          title: "Fallback",
          message: `${String(event.data["fromModel"] ?? "")} → ${String(event.data["toModel"] ?? "")}`,
          variant: "warning",
        })
      }
      if (event.type === "background.done") {
        toast.show({
          title: "Sub-agent",
          message: `${String(event.data["name"] ?? "")} ${String(event.data["status"] ?? "")}`,
          variant: event.data["status"] === "completed" ? "success" : "error",
        })
      }
    })
    onCleanup(unsubscribe)
    void refreshStats()
    if (props.initialPrompt) {
      submit(props.initialPrompt)
    }
  })

  // A session picked in the dialog or a tab replays into this store.  Runs on
  // mount too: the route switches with the hydration already in place.
  createEffect(
    on(
      () => props.hydrate,
      (hydrated) => {
        if (hydrated && hydrated.key === props.sessionId) {
          setState(hydrated.state)
          void refreshStats()
        }
      },
    ),
  )

  return (
    <box flexDirection="column" width="100%" height="100%">
      <SessionTabs
        sessions={props.sessions}
        currentId={props.sessionId}
        onSelect={props.onSelectSession}
        onNew={props.onNewSession}
      />
      <box flexDirection="row" flexGrow={1} minHeight={0}>
        <scrollbox
          ref={(element: ScrollBoxRenderable) => (transcript = element)}
          flexGrow={1}
          stickyScroll
          stickyStart="bottom"
          paddingLeft={2}
          paddingRight={2}
          paddingTop={1}
        >
          <Index each={state.messages}>
            {(message) => <MessageView message={message()} thinking={thinking()} />}
          </Index>
          <box height={1} />
        </scrollbox>
        <Show when={dimensions().width >= 100}>
          <Sidebar
            title={props.title}
            tokens={stats().tokens}
            maxContextTokens={stats().maxContextTokens}
            workspace={props.workspace}
            branch={props.branch}
          />
        </Show>
      </box>
      <box paddingLeft={2} paddingRight={2} paddingBottom={1} paddingTop={1} flexShrink={0} flexDirection="column">
        <Show when={props.permission}>
          {(request) => (
            <PermissionPanel request={request()} onDecide={props.onPermissionDecide} />
          )}
        </Show>
        <Show when={props.ask}>
          {(request) => <AskPanel request={request()} onAnswer={props.onAskAnswer} />}
        </Show>
        <Show
          when={props.permission === null && props.ask === null}
          fallback={<></>}
        >
          <>
            <Show when={state.queued.length > 0}>
              <text fg={theme.textMuted}>{`${state.queued.length} queued`}</text>
            </Show>
            <Prompt
              placeholder={state.busy ? "Working…" : "Ask anything…"}
              metadata={`${[props.provider, props.model].filter(Boolean).join(" · ") || "no model"}${state.busy ? " · working" : ""}`}
              onSubmit={submit}
              onOverlay={setSuggesting}
              seed={promptSeed()}
              onSeedDone={() => setPromptSeed("")}
            />
          </>
        </Show>
      </box>
      <StatusBar
        workspace={props.workspace}
        branch={props.branch}
        tokens={stats().tokens}
        maxContextTokens={stats().maxContextTokens}
        version={props.version}
        interruptArmed={interruptArmed()}
      />
    </box>
  )
}
