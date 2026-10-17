/**
 * Session route (M2/M4): real AgentLoop turns + the session chrome (tabs,
 * sidebar, status bar).
 *
 * Data flow, one direction only:
 *   IpcClient.onEvent -> reduce(state, event) -> createStore -> <MessageView>
 *
 * Local state beyond the reducer: `alwaysThinking` (ctrl+t) and `stats` (the real
 * token usage the sidebar/status bar show, refreshed on mount and after every
 * turn).  The chrome is fed by the app shell (sessions list, title, location).
 */

import type { ScrollBoxRenderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { useTerminalDimensions } from "@opentui/solid"
import { Index, Show, createEffect, createSignal, on, onCleanup, onMount } from "solid-js"
import { createStore } from "solid-js/store"
import { MessageView } from "../component/message"
import { DialogSkills } from "../component/dialog-skills"
import { AskPanel } from "../component/ask"
import { PlanReviewPanel } from "../component/plan-review"
import { PermissionPanel } from "../component/permission"
import { Prompt } from "../component/prompt"
import { buildSubmission, commandName, planPrelude, type PendingPrompt } from "../util/submission"
import { SessionHeader } from "../component/session-header"
import { Sidebar } from "../component/sidebar"
import { StatusBar } from "../component/status-bar"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { PromptImage } from "../util/attachment"
import type { MentionOption } from "../util/mention"
import type { Action, SessionState } from "../state/session"
import { initialState, jobsFromEvent, reduce } from "../state/session"
import { useToast } from "../component/toast"
import { theme } from "../theme/tokens"
import { escStep } from "../util/esc"
import { loadSession, resumeSession } from "../state/replay"
import { isPlanApproval, type AskRequest } from "../util/ask"
import type { PermissionDecision, PermissionRequest } from "../util/permission"

export interface SessionProps {
  client: IpcClient
  sessionId: string
  model: string
  provider: string
  /** Text submitted from home; auto-sent once the route mounts. */
  initialPrompt?: PendingPrompt | null
  /** The dispatcher's vocabulary (r19/F5): decides whether a line is a command. */
  commands?: string[]
  /** A replayed session (from the dialog or tabs); replaces the transcript. */
  hydrate?: { key: string; state: SessionState } | null
  title: string
  workspace: string
  branch: string
  version: string
  /** A pending WARN-tier approval; replaces the prompt while non-null (M5). */
  permission: PermissionRequest | null
  onPermissionDecide: (decision: PermissionDecision, note?: string) => void
  ask: AskRequest | null
  onAskAnswer: (answer: string) => void
  /** Leave through the shell's exit sequence (a command's `/quit`, I5). */
  onQuit: () => void
  /** Follow the live session when the backend switched it (/clear, /resume). */
  onSelectSession: (id: string) => void
  /** Open the sessions panel — the strip is a header now, not a tab rail. */
  onOpenSessions: () => void
  /** The tab list can go stale after a turn; the shell refreshes it. */
  onTurnFinished: () => void
  /** The registry's skills, for the composer's `@` suggestions (r16/E-2). */
  skills: MentionOption[]
  /** Workspace files/directories for the same list (r17/T4). */
  files: MentionOption[]
}

interface Stats {
  tokens: number
  maxContextTokens: number
  /** P5 (18 §10.5): posture payload; absent key = old backend -> no marker. */
  mode?: "normal" | "plan"
  readonly?: boolean
}

export function Session(props: SessionProps) {
  const dialog = useDialog()
  const toast = useToast()
  const dimensions = useTerminalDimensions()
  const [state, setState] = createStore<SessionState>(initialState())
  const [alwaysThinking, setAlwaysThinking] = createSignal(false)
  const [stats, setStats] = createSignal<Stats>({ tokens: 0, maxContextTokens: 0 })
  const [interruptArmed, setInterruptArmed] = createSignal(false)
  const [suggesting, setSuggesting] = createSignal(false)
  let transcript: ScrollBoxRenderable | undefined
  let interruptTimer: ReturnType<typeof setTimeout> | undefined
  // 19 §3.3: set when the review card answers approve; the run dispatches at
  // the next terminal turn.finished (the review tool blocks until the answer,
  // so that event is the first moment the runner can legally start).
  let pendingPlanRun = false

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

  const dispatch = (text: string, images: PromptImage[] = []) => {
    // A "/"-prefixed line is a slash command: the backend's dispatcher
    // (app/commands.py::handle_input) decides what it means, so the TUI gains
    // the whole command layer through one entry.  Unknown commands fall
    // through it as plain text — the same verdict the model would get either
    // way.  Plain text keeps the live-turn path (bridge deltas, no console).
    // r18/F14: the method, the payload and what stays in the composer all come
    // from one decision, so the transcript cannot show a picture the model
    // never received.
    const { method, payload } = buildSubmission(props.commands ?? [], props.sessionId, text, images)
    void props.client
      .request(method, payload)
      .then((ack: unknown) => {
        // r20/F5: the backend refuses a paste it cannot store (bad mime, corrupt
        // payload, over the budget) and says so in the ack.  The transcript has
        // already drawn the picture, so without this the user watches an image
        // go out that the model never received.  The rules stay on the backend;
        // the client only relays the verdict.
        const rejected = Array.isArray((ack as { rejected?: unknown } | null)?.rejected)
          ? ((ack as { rejected: { name: string; reason: string }[] }).rejected)
          : []
        if (rejected.length > 0) apply({ kind: "attachment-rejected", items: rejected })
        // A command can rewrite the history. `/compact` moves the context
        // meter's boundary, so without this the sidebar kept reporting the
        // pre-compaction occupancy until the next turn happened to refresh it —
        // which reads as "the compaction did nothing".
        if (method === "session.command") return refreshStats()
      })
      .catch((error) => {
        apply({ kind: "submit-failed", reason: String(error) })
      })
  }

  const submit = (text: string, images: PromptImage[] = []): boolean | string => {
    // The retired Textual UI queued a submit that arrived mid-turn and ran it
    // when the turn ended; replicate that here.  The queue holds text only, so
    // a submission carrying images is refused while busy (the composer keeps
    // them and says why) rather than silently dropped.
    if (state.busy) {
      if (images.length > 0) return false
      // steering 前端化: plain text typed mid-turn goes INTO the run -- the
      // loop drains the FIFO at its next iteration boundary (R7e-2) -- while
      // commands still queue (command semantics) and images queue too (no
      // steer path for them).  Echo lands when the backend accepts; a
      // refusal or a dead socket falls back to the queue, never a drop.
      if (commandName(props.commands ?? [], text)) {
        apply({ kind: "queued", text })
        return true
      }
      void props.client
        .request("session.steer", { text })
        .then((ack: unknown) => {
          const accepted = (ack as { accepted?: boolean }).accepted !== false
          if (accepted) apply({ kind: "submitted", text, images: [] })
          else apply({ kind: "queued", text })
        })
        .catch(() => apply({ kind: "queued", text }))
      return true
    }
    // r18/F14: a command sends no images, so it shows none either and hands
    // them back with a reason instead of letting the transcript imply they
    // were sent.
    const submission = buildSubmission(props.commands ?? [], props.sessionId, text, images)
    if (submission.kept.length > 0) return submission.withheldReason
    apply({ kind: "submitted", text, images })
    dispatch(text, images)
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
        run: () => setAlwaysThinking((value) => !value),
      },
      {
        name: "session.resume",
        title: "Resume interrupted turn",
        group: "Session",
        desc: "Continue the crashed turn from the persisted history",
        palette: true,
        // 08/Q4: the recovery notice names this key, so the transcript tells
        // the user exactly how to continue.  The notice is the only thing that
        // can ask for this -- without one there is no interrupted turn, and
        // the backend refuses anyway -- so a stray keypress is a no-op.
        run: () => {
          if (state.busy) return
          const resumable = state.messages.some((message) =>
            message.parts.some((part) => part.kind === "notice" && part.action === "resume"),
          )
          if (!resumable) return
          apply({ kind: "resumed" })
          // The turn's wire events stream back like a prompted turn's; a
          // refusal (stale keypress) surfaces the same way a failed submit
          // does, which also releases `busy`.
          void resumeSession(props.client, props.sessionId).catch((error) =>
            apply({ kind: "submit-failed", reason: String(error) }),
          )
        },
      },
      {
        name: "session.mode.cycle",
        title: "Toggle plan mode",
        group: "Session",
        desc: "Switch between Build and read-only Plan mode",
        palette: true,
        // 19 §3.1: the mode row's one action.  Mid-turn it queues like
        // /review does — the backend's posture queue lands the flip at the
        // turn's close either way.  内化批次: an idle flip dispatches silently
        // — the backend notice tells the story; the transcript must not
        // dress an internal command as a user line.
        run: () => {
          const next = stats().mode === "plan" ? "/plan off" : "/plan"
          if (state.busy) {
            apply({ kind: "queued", text: next })
            return
          }
          dispatch(next)
        },
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
        name: "dialog.skills.show",
        title: "Skills",
        group: "Skill",
        desc: "List available skills",
        palette: true,
        // Registered here rather than at the app layer (r16/E-1): the command
        // seeds `@name` into *this* route's composer, so every composer-owning
        // route registers its own variant. Registering both layers used to
        // show two identical "Skills" rows in the palette.
        run: () =>
          dialog.open(
            () => (
              <DialogSkills client={props.client} onSelect={(name: string) => setPromptSeed(`@${name} `)} />
            ),
            "large",
          ),
      },
    ],
    bindings: [
      { key: "escape", cmd: "session.interrupt" },
      { key: "ctrl+t", cmd: "session.toggle.thinking" },
      { key: "shift+tab", cmd: "session.mode.cycle" },
      { key: "<leader>r", cmd: "session.resume" },
    ],
  }))

  // The suggestion lists seed the composer (e.g. ``@skill``); the seed lands
  // the text in the textarea and focuses it.
  const [promptSeed, setPromptSeed] = createSignal("")

  onMount(() => {
    // The snapshot first: a job that started before this client connected has
    // no job.changed history to replay, and a panel that only ever shows new
    // jobs is worse than none.
    void props.client
      .request("session.jobs")
      .then((data: unknown) => {
        const rows = jobsFromEvent(data as Record<string, unknown>)
        if (rows !== null) apply({ kind: "jobs", jobs: rows })
      })
      .catch(() => {
        /* the panel just stays empty; a turn's job.changed will fill it */
      })
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
          // The live session switched (/clear, /resume), or /undo|/redo
          // rewrote this session's history in place (r17/T1): re-project.
          void (async () => {
            const loaded = await loadSession(props.client, props.sessionId)
            setState({ ...loaded, queued: state.queued })
            void refreshStats()
          })()
        }
        {
          // /undo hands the undone user message back so it can be edited and
          // re-sent, the gesture the old TUI had.
          const undo = event.data["undo"] as { text?: unknown } | null | undefined
          if (undo && typeof undo.text === "string") setPromptSeed(undo.text)
        }
        {
          // A same-session history rewrite reports itself for the toast.  It
          // is separate from `reload` above: only a switch re-projects.
          const compacted = event.data["compacted"] as
            | { before?: unknown; after?: unknown }
            | null
            | undefined
          const before = typeof compacted?.before === "number" ? compacted.before : 0
          const after = typeof compacted?.after === "number" ? compacted.after : 0
          if (compacted && before > after) {
            toast.show({
              title: "Compacted",
              message: `${before} → ${after} messages`,
              variant: "info",
            })
            void refreshStats()
          }
        }
        // 19 §3.3: an approved plan starts executing here — the mode disarm
        // already landed with the turn's close, so the internal run command
        // finds an idle loop.  The echo line mirrors the /review dispatch.
        let ranPlanRun = false
        if (pendingPlanRun) {
          pendingPlanRun = false
          ranPlanRun = true
          // 内化批次: silent — the run's own receipts and the posture notice
          // are the story; no "┃ /plan run" line dresses it as user input.
          dispatch("/plan run")
        }
        // One queued line per finished turn (deferred when the plan run won
        // this end-of-turn slot).
        const next = ranPlanRun ? undefined : state.queued[0]
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
      if (event.type === "job.changed") {
        // D-1b: the panel's rows. Folded into state (not the transcript) so the
        // sidebar line and the dialog read the same array.
        const rows = jobsFromEvent(event.data)
        if (rows !== null) apply({ kind: "jobs", jobs: rows })
      }
      if (event.type === "background.done") {
        toast.show({
          title: "Sub-agent",
          message: `${String(event.data["name"] ?? "")} ${String(event.data["status"] ?? "")}`,
          variant: event.data["status"] === "completed" ? "success" : "error",
        })
      }
      if (event.type === "compaction.failed") {
        toast.show({
          title: "Compaction failed",
          message: String(event.data["error"] ?? ""),
          variant: "error",
        })
      }
    })
    onCleanup(unsubscribe)
    void refreshStats()
    if (props.initialPrompt) {
      const prompt = props.initialPrompt
      // 20 §批次1: a Home submission made with PLAN armed lands the posture
      // first over the internal command channel; the prompt waits for it
      // (bare /plan is synchronous, no model call). The echo matches the
      // command face, so the transcript says what the wire did.
      const prelude = planPrelude(prompt, props.commands ?? [], props.sessionId)
      if (prelude) {
        // 内化批次: the posture lands silently; the "已进入计划模式" notice
        // is the transcript's account of it (a "┃ /plan" echo dressed the
        // internal command as the user's own line).
        void props.client
          .request(prelude.method, prelude.payload)
          .finally(() => submit(prompt.text, prompt.images))
      } else {
        submit(prompt.text, prompt.images)
      }
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
      <SessionHeader
        title={props.title}
        id={props.sessionId}
        onOpen={props.onOpenSessions}
      />
      <box flexDirection="row" flexGrow={1} minHeight={0}>
        {/* Composer sits INSIDE the left column (OpenCode routes/session/
            index.tsx:1365-1372), so its border stops at the sidebar edge
            instead of running underneath it. */}
        <box flexDirection="column" flexGrow={1} minHeight={0}>
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
              {(message) => <MessageView message={message()} alwaysOpen={alwaysThinking()} />}
            </Index>
            <box height={1} />
          </scrollbox>
        <box paddingLeft={2} paddingRight={2} paddingBottom={1} paddingTop={1} flexShrink={0} flexDirection="column">
          <Show when={props.permission}>
            {(request) => (
              <PermissionPanel request={request()} onDecide={props.onPermissionDecide} />
            )}
          </Show>
          <Show when={props.ask}>
            {(request) => (
              <Show
                when={request().intent === "plan_review"}
                fallback={<AskPanel request={request()} onAnswer={props.onAskAnswer} />}
              >
                <PlanReviewPanel
                  request={request()}
                  onAnswer={(answer) => {
                    if (isPlanApproval(answer)) pendingPlanRun = true
                    props.onAskAnswer(answer)
                  }}
                />
              </Show>
            )}
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
                mode={stats().mode}
                onSubmit={submit}
                onOverlay={setSuggesting}
                seed={promptSeed()}
                onSeedDone={() => setPromptSeed("")}
                skills={props.skills}
                files={props.files}
              />
            </>
          </Show>
        </box>
        </box>
        <Show when={dimensions().width >= 100}>
          <Sidebar
            title={props.title}
            tokens={stats().tokens}
            maxContextTokens={stats().maxContextTokens}
            workspace={props.workspace}
            branch={props.branch}
            jobs={state.jobs}
          />
        </Show>
      </box>
      <StatusBar
        workspace={props.workspace}
        branch={props.branch}
        tokens={stats().tokens}
        maxContextTokens={stats().maxContextTokens}
        version={props.version}
        interruptArmed={interruptArmed()}
        mode={stats().mode}
        readonly={stats().readonly}
      />
    </box>
  )
}
