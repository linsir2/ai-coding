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
import { Prompt } from "../component/prompt"
import { SessionTabs, type SessionTab } from "../component/session-tabs"
import { Sidebar } from "../component/sidebar"
import { StatusBar } from "../component/status-bar"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { Action, SessionState } from "../state/session"
import { initialState, reduce } from "../state/session"

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
  const dimensions = useTerminalDimensions()
  const [state, setState] = createStore<SessionState>(initialState())
  const [thinking, setThinking] = createSignal(false)
  const [stats, setStats] = createSignal<Stats>({ tokens: 0, maxContextTokens: 0 })
  let transcript: ScrollBoxRenderable | undefined

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

  const submit = (text: string): boolean => {
    if (state.busy) return false
    apply({ kind: "submitted", text })
    void props.client.request("session.prompt", { id: props.sessionId, text }).catch((error) => {
      apply({ kind: "submit-failed", reason: String(error) })
    })
    return true
  }

  useBindings(() => ({
    enabled: !dialog.isOpen(),
    commands: [
      {
        name: "session.interrupt",
        title: "Interrupt turn",
        group: "Session",
        desc: "Stop the current turn",
        palette: true,
        run: () => {
          if (!state.busy) return
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
    ],
    bindings: [
      { key: "escape", cmd: "session.interrupt" },
      { key: "ctrl+t", cmd: "session.toggle.thinking" },
    ],
  }))

  onMount(() => {
    const unsubscribe = props.client.onEvent((event) => {
      apply({ kind: "wire", event })
      if (event.type === "turn.finished") {
        void refreshStats()
        props.onTurnFinished()
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
      <box paddingLeft={2} paddingRight={2} paddingBottom={1} paddingTop={1} flexShrink={0}>
        <Prompt
          placeholder={state.busy ? "Working…" : "Ask anything…"}
          metadata={`${[props.provider, props.model].filter(Boolean).join(" · ") || "no model"}${state.busy ? " · working" : ""}`}
          onSubmit={submit}
        />
      </box>
      <StatusBar
        workspace={props.workspace}
        branch={props.branch}
        tokens={stats().tokens}
        maxContextTokens={stats().maxContextTokens}
        version={props.version}
      />
    </box>
  )
}
