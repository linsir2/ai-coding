/**
 * Session route (M2): real AgentLoop turns over the IPC event bridge.
 *
 * Data flow, one direction only:
 *   IpcClient.onEvent -> reduce(state, event) -> createStore -> <MessageView>
 *
 * Local state is limited to two things the reducer does not own:
 *   busy   (in the store: reducer decides it from submitted/turn.finished)
 *   thinking (a signal: OC's global thinking=hide/show toggle, bound to ctrl+t)
 *
 * Busy semantics (user-approved): the prompt stays editable while a turn
 * runs; Enter is ignored and the text stays in the box.  Esc interrupts
 * through the server's two-layer cancel (tool batch + turn future).
 */

import type { ScrollBoxRenderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { Index, createSignal, onCleanup, onMount } from "solid-js"
import { createStore } from "solid-js/store"
import { MessageView } from "../component/message"
import { Prompt } from "../component/prompt"
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
}

export function Session(props: SessionProps) {
  const [state, setState] = createStore<SessionState>(initialState())
  const [thinking, setThinking] = createSignal(false)
  let transcript: ScrollBoxRenderable | undefined

  const apply = (action: Action) => {
    const next = reduce(state, action)
    if (next !== state) setState(next)
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
    commands: [
      {
        name: "session.interrupt",
        desc: "Interrupt the running turn",
        run: () => {
          if (!state.busy) return
          void props.client.request("session.interrupt")
        },
      },
      {
        name: "session.toggle.thinking",
        desc: "Toggle thinking",
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
    })
    onCleanup(unsubscribe)
    if (props.initialPrompt) {
      submit(props.initialPrompt)
    }
  })

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
        <Index each={state.messages}>
          {(message) => <MessageView message={message()} thinking={thinking()} />}
        </Index>
        <box height={1} />
      </scrollbox>
      <box paddingLeft={2} paddingRight={2} paddingBottom={1} flexShrink={0}>
        <Prompt
          placeholder={state.busy ? "Working…" : "Ask anything…"}
          metadata={`${[props.provider, props.model].filter(Boolean).join(" · ") || "no model"}${state.busy ? " · working" : ""}`}
          onSubmit={submit}
        />
      </box>
    </box>
  )
}
