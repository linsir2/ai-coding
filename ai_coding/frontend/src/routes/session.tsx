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
import { normalizeAgents, nextAgentId, type AgentSummary } from "../state/agents"
import { isPlanAgentId } from "../util/plan-agent"
import { AgentPicker } from "../component/agent-picker"
import { SessionHeader } from "../component/session-header"
import { DialogInbox } from "../component/dialog-inbox"
import { Sidebar } from "../component/sidebar"
import { StatusBar } from "../component/status-bar"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { PromptImage } from "../util/attachment"
import type { MentionOption } from "../util/mention"
import type { Action, SessionState } from "../state/session"
import { type BudgetFigures, initialState, jobsFromEvent, reduce } from "../state/session"
import { useToast } from "../component/toast"
import { theme } from "../theme/tokens"
import { escStep } from "../util/esc"
import { shouldDispatchPlanRun } from "../util/plan-run"
import { loadSession, resumeSession } from "../state/replay"
import type { AskRequest } from "../util/ask"
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
  /** T7: the live agent id; absent key = old backend -> composer fail-safe. */
  agent?: string | null
  /** M4b: the session card's facts; absent keys = old backend -> lines hide. */
  turnCount?: number
  createdTime?: string
  budget?: BudgetFigures | null
}

export function Session(props: SessionProps) {
  const dialog = useDialog()
  const toast = useToast()
  const dimensions = useTerminalDimensions()
  const [state, setState] = createStore<SessionState>(initialState())
  const [alwaysThinking, setAlwaysThinking] = createSignal(false)
  const [stats, setStats] = createSignal<Stats>({ tokens: 0, maxContextTokens: 0 })
  // T7: the visible primaries (picker + shift+tab share this order).  The
  // current agent itself lives in SessionState.agent, never here.
  const [agents, setAgents] = createSignal<AgentSummary[]>([])
  /** Display name for an agent id (falls back to the id). */
  const agentName = (id: string | null | undefined): string =>
    (id && agents().find((row) => row.id === id)?.name) || id || "Build"
  // T7: the posture axes derive from the agent id at render time — the one
  // truth is SessionState.agent (stats hydrates it, agent.switched moves it).
  const currentAgent = () => state.agent ?? stats().agent ?? null
  const currentMode = (): "normal" | "plan" => (isPlanAgentId(currentAgent()) ? "plan" : "normal")
  const currentReadonly = () =>
    isPlanAgentId(currentAgent()) ? true : (stats().readonly ?? false)
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

  const refreshPosture = async () => {
    // T7: stats and the agent list land together (one mount, one retry).
    // stats.agent hydrates SessionState.agent; the list feeds picker/cycle.
    try {
      const [fetchedStats, fetchedAgents] = await Promise.all([
        props.client.request("session.stats"),
        props.client.request("session.agents"),
      ])
      setStats(fetchedStats as Stats)
      setAgents(normalizeAgents((fetchedAgents as { agents?: unknown } | null)?.agents))
      const agent = (fetchedStats as { agent?: unknown } | null)?.agent
      apply({ kind: "agent-synced", agent: typeof agent === "string" && agent ? agent : null })
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
    // D: a command runs on the server, so the composer must not read as
    // free while it does.  Its inner turns ride "turn.step" (which never
    // touches busy) and its terminal "turn.finished" is what frees the
    // composer -- so the busy has to go up here, on the way in.  Typed
    // commands already had it (the "submitted" action); the ones the route
    // dispatches itself (the auto-run) did not, and the operator could
    // type into a session that was executing a plan.
    if (method === "session.command") apply({ kind: "command-started" })
    void props.client
      .request(method, payload)
      .then((ack: unknown) => {
        // D7: the ack names the turn this request started; its terminal event
        // will name it too.  Registering the id here is what lets the reducer
        // tell this ending from an older turn's.
        const turnId = Number((ack as { turnId?: unknown } | null)?.turnId ?? 0)
        if (Number.isInteger(turnId) && turnId > 0) apply({ kind: "turn-started", turnId })
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
        if (method === "session.command") return refreshPosture()
      })
      .catch((error) => {
        apply({ kind: "submit-failed", reason: String(error) })
      })
  }

  /** Local ids for rows the socket was too dead to persist (D-2). */
  let localSeq = 0
  const localId = (): string => `local-${(localSeq += 1)}-${Date.now() % 100000}`

  /**
   * Queue one line through the backend table (D-2).  The echo lands with the
   * id the server returns; when the socket is dead the row lives only here
   * (same fallback the steer path has always had) and dispatch deletes it
   * best-effort.  Either way the text is never dropped.
   */
  const queueText = (text: string): void => {
    void props.client
      .request("session.inbox", { op: "add", text })
      .then((ack: unknown) => {
        const data = ack as { ok?: boolean; id?: string };
        if (data?.ok === true && typeof data.id === "string" && data.id) {
          apply({ kind: "queued", id: data.id, text });
        } else {
          apply({ kind: "queued", id: localId(), text });
        }
      })
      .catch(() => apply({ kind: "queued", id: localId(), text }));
  }

  /** Read the backend's rows into state (mount, reload, panel refresh). */
  const loadInbox = (): void => {
    void props.client
      .request("session.inbox", { op: "list" })
      .then((data: unknown) => {
        const rows = (data as { rows?: unknown })?.rows;
        if (!Array.isArray(rows)) return;
        apply({
          kind: "queued-loaded",
          rows: rows
            .map((row) => {
              const record = row as Record<string, unknown>;
              if (typeof record["id"] !== "string" || typeof record["text"] !== "string") return null;
              return { id: record["id"] as string, text: record["text"] as string };
            })
            .filter((row): row is { id: string; text: string } => row !== null),
        });
      })
      .catch(() => {
        /* the panel stays as it was; a later turn's refresh will fill it */
      });
  }

  /**
   * The inbox row being edited, if the composer currently holds one (D-2).
   * Submitting while set updates the row instead of starting anything -- the
   * row was queued for later, and editing it must not spend a turn.
   */
  const [editingInboxId, setEditingInboxId] = createSignal<string | null>(null)

  const submit = (text: string, images: PromptImage[] = []): boolean | string => {
    const editing = editingInboxId()
    if (editing !== null) {
      setEditingInboxId(null)
      void props.client
        .request("session.inbox", { op: "edit", id: editing, text })
        .then((ack: unknown) => {
          const data = ack as { ok?: boolean };
          if (data?.ok === true) apply({ kind: "queued-edited", id: editing, text });
        })
        .catch(() => {
          /* the row keeps its old text locally; the panel still shows it */
        })
      return true
    }    // The retired Textual UI queued a submit that arrived mid-turn and ran it
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
        queueText(text)
        return true
      }
      void props.client
        .request("session.steer", { text })
        .then((ack: unknown) => {
          const accepted = (ack as { accepted?: boolean }).accepted !== false
          if (accepted) apply({ kind: "submitted", text, images: [] })
          else queueText(text)
        })
        .catch(() => queueText(text))
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

  /** A card owns the keyboard while it is up (D4). */
  const cardOpen = (): boolean => props.permission !== null || props.ask !== null

  /**
   * T7: run one agent switch through session.agent.select.  The backend owns
   * the mid-turn queue (it answers queued and lands the flip at the turn's
   * close), so the route never queues text for this — idle or busy, the call
   * is the same.  A refusal surfaces as a toast: the composer (and its busy
   * flag) is not ours to rewrite for a switch that never happened.
   */
  const selectAgent = async (id: string): Promise<{ ok: boolean; status: string; reason: string }> => {
    try {
      const res = (await props.client.request("session.agent.select", { id })) as {
        ok?: unknown
        status?: unknown
        reason?: unknown
      }
      if (res?.ok === true) return { ok: true, status: String(res.status ?? ""), reason: "" }
      return { ok: false, status: "", reason: typeof res?.reason === "string" ? res.reason : "unknown" }
    } catch (error) {
      return { ok: false, status: "", reason: String(error) }
    }
  }

  /**
   * T7: the shift+tab action, shared by the session layer and the card layer
   * (D4).  Cycles the visible-primary order from state/agents — no longer
   * plan ↔ off.  An idle flip lands with the backend's agent.switched
   * notice; a mid-turn one answers queued and lands at the turn's close.
   */
  const cycleMode = (): void => {
    const next = nextAgentId(agents(), state.agent ?? stats().agent ?? "build")
    if (!next) return
    void selectAgent(next).then((res) => {
      if (!res.ok) {
        toast.show({ title: "Agent", message: `切换失败：${res.reason}`, variant: "error" })
      }
    })
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
          void resumeSession(props.client, props.sessionId)
            .then((ack) => {
              // D7: the resumed turn's ending will name this id.
              if (ack.turnId > 0) apply({ kind: "turn-started", turnId: ack.turnId })
            })
            .catch((error) => apply({ kind: "submit-failed", reason: String(error) }))
        },
      },
      {
        // D-2: the queued-input panel. Route-integrated: rows come from state,
        // edits land in the composer, mutations report back through actions.
        // An app-level twin exists for palette discovery (the two routes never
        // mount together); it runs self-contained instead.
        name: "dialog.inbox.show",
        title: "Queued input",
        group: "Session",
        desc: "View, edit, or delete queued input",
        palette: true,
        run: () =>
          dialog.open(
            () => (
              <DialogInbox
                client={props.client}
                sessionId={props.sessionId}
                rows={() => state.queued}
                onEdited={(id, text) => apply({ kind: "queued-edited", id, text })}
                onDeleted={(id) => apply({ kind: "queued-deleted", id })}
                onSelectRow={(row) => {
                  dialog.close()
                  setPromptSeed(row.text)
                  setEditingInboxId(row.id)
                }}
              />
            ),
            "large",
          ),
      },
      {
        name: "session.mode.cycle",
        title: "Cycle agent",
        group: "Session",
        desc: "Switch to the next primary agent",
        palette: true,
        run: cycleMode,
      },
      {
        // T7: the TUI `/agents` listing — rows from state/agents, the switch
        // through session.agent.select.  shift+tab cycles without asking;
        // this names the target first.
        name: "dialog.agents.show",
        title: "Agents",
        group: "Session",
        desc: "List agents and switch the current one",
        palette: true,
        run: () =>
          dialog.open(
            () => (
              <AgentPicker
                agents={agents()}
                current={state.agent ?? stats().agent ?? null}
                onSelect={(id) => {
                  dialog.close()
                  void selectAgent(id).then((res) => {
                    if (!res.ok) {
                      toast.show({ title: "Agent", message: `切换失败：${res.reason}`, variant: "error" })
                    }
                  })
                }}
              />
            ),
            "large",
          ),
      },
      {
        name: "session.review",
        title: "Review",
        group: "Session",
        desc: "Review the current git diff",
        palette: true,
        run: () => {
          if (state.busy) {
            queueText("/review")
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
      { key: "<leader>i", cmd: "dialog.inbox.show" },
    ],
  }))

  // D4: the card owns the keyboard — its digits, esc and return must win —
  // but the posture toggle is not part of the card's question.  A separate
  // layer at a lower priority keeps shift+tab alive without re-enabling the
  // session layer (whose overlapping keys would re-sort against the app's).
  useBindings(() => ({
    priority: -1,
    enabled: cardOpen(),
    commands: [
        {
          name: "session.mode.cycle.card",
          desc: "Switch agent while a card is up",
          run: cycleMode,
        },
    ],
    bindings: [{ key: "shift+tab", cmd: "session.mode.cycle.card" }],
  }))

  // The suggestion lists seed the composer (e.g. ``@skill``); the seed lands
  // the text in the textarea and focuses it.
  const [promptSeed, setPromptSeed] = createSignal("")

    onMount(() => {
    // The posture lands first: stats.agent hydrates SessionState.agent and
    // the list feeds picker/cycle.  The initial prompt below waits for it.
    const postureReady = refreshPosture()
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
        void refreshPosture()
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
            // Carry the queue across the switch the way the old in-memory list
            // did: rows that only ever lived here are persisted into the fresh
            // session first, then the server's list -- which now includes them
            // -- becomes the list.  A reload implies a live socket (the load
            // just succeeded over it), so the flush cannot strand them.
            for (const row of state.queued) {
              if (!row.id.startsWith("local-")) continue
              try {
                await props.client.request("session.inbox", { op: "add", text: row.text })
              } catch {
                break
              }
            }
            setState({ ...loaded, queued: [] })
            loadInbox()
            void refreshPosture()
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
            void refreshPosture()
          }
        }
        // 19 §3.3: an approved plan starts executing here — the mode disarm
        // already landed with the turn's close, so the internal run command
        // finds an idle loop.  The echo line mirrors the /review dispatch.
        //
        // W2/R11: the approval is a yes to *this* plan, and the turn that
        // carried it is what the plan was written against.  A turn that did
        // not end cleanly (aborted / error / timeout / budget /
        // max_iterations / stopped) left the session somewhere else, so the
        // run is not dispatched from it.  The approval itself is still
        // consumed -- the pending flag is the approval's record, and the
        // plan stays armed on the task for the boot resume to decide.
        let ranPlanRun = false
        const approved = pendingPlanRun
        pendingPlanRun = false
        if (shouldDispatchPlanRun(approved, event.data["status"])) {
          ranPlanRun = true
          // 内化批次: silent — the run's own receipts and the posture notice
          // are the story; no "┃ /plan run" line dresses it as user input.
          dispatch("/plan run")
        }
        // One queued line per finished turn (deferred when the plan run won
        // this end-of-turn slot, or when the ending was not this turn's --
        // the prelude's terminal lands while the prompt is still running).
        // The server row goes with it, best-effort: a local-only row has
        // nothing to delete and the server ignores ids it never saw, so both
        // kinds dispatch exactly the same way.
        const next = ranPlanRun || state.busy ? undefined : state.queued[0]
        if (next !== undefined) {
          apply({ kind: "queued-dispatched" })
          void props.client
            .request("session.inbox", { op: "delete", id: next.id })
            .catch(() => {
              /* the row is already out of local state; nothing to reconcile */
            });
          dispatch(next.text)
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
    if (props.initialPrompt) {
      const prompt = props.initialPrompt
      // T7: the Home draft selects first, then the old prelude, then the
      // prompt — agent.select → [plan prelude] → prompt.  A queued or refused
      // select sends NOTHING (displaying A while B executes is the failure
      // mode this ordering exists to close); the reason lands in the
      // composer area instead.
      void (async () => {
        // Wait for the hydration above: the prelude derives from the live
        // agent, and the select must not race the sync that names it.
        await postureReady
        if (prompt.agent) {
          const switched = await selectAgent(prompt.agent)
          if (!switched.ok || switched.status === "queued") {
            const why = switched.ok ? "已排队" : switched.reason
            apply({ kind: "submit-failed", reason: `agent 切换未完成（${why}），未发送` })
            return
          }
        }
        // The prelude follows the effective agent now: an explicit draft, or
        // the live one the stats just hydrated (a null draft on an already
        // plan session still arms the plan entry).
        const live = state.agent ?? stats().agent
        const effectivePrompt: PendingPrompt = {
          ...prompt,
          agent: prompt.agent ?? (typeof live === "string" ? live : undefined),
        }
        const prelude = planPrelude(effectivePrompt, props.commands ?? [], props.sessionId)
        if (prelude) {
          // 内化批次: the posture lands silently; the notice is the
          // transcript's account of it (a "┃ /plan" echo dressed the
          // internal command as the user's own line).
          void props.client
            .request(prelude.method, prelude.payload)
            .finally(() => submit(prompt.text, prompt.images))
        } else {
          submit(prompt.text, prompt.images)
        }
      })()
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
          // The inbox table survived on disk, but this process never saw it:
          // read it once the session is actually here, not on mount (the
          // server has no session yet then, and the call fails silently).
          loadInbox()
          void refreshPosture()
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
                  onAnswer={(answer, intent) => {
                    // C: the panel says what it meant; we no longer re-parse the
                    // answer to find out.  A dismiss also CLEARS a pending run:
                    // the turn may have been approved once already, and esc on a
                    // second card must not leave the earlier approval to fire.
                    pendingPlanRun = intent === "approve"
                    props.onAskAnswer(answer)
                  }}
                />
              </Show>
            )}
          </Show>
          <Show when={state.queued.length > 0}>
            <text fg={theme.textMuted}>{`${state.queued.length} queued`}</text>
          </Show>
          <Prompt
            // D4: a card no longer unmounts the composer -- the draft stays,
            // the card owns the keyboard, and shift+tab still cycles.
            disabled={cardOpen()}
            placeholder={
              editingInboxId() !== null
                ? "编辑排队输入…（回车保存）"
                : state.busy
                  ? "Working…"
                  : "Ask anything…"
            }
            metadata={`${[props.provider, props.model].filter(Boolean).join(" · ") || "no model"}${state.busy ? " · working" : ""}`}
            agent={currentAgent()}
            agentName={agentName(currentAgent())}
            mode={currentMode()}
            readonly={currentReadonly()}
            onSubmit={submit}
            onOverlay={setSuggesting}
            seed={promptSeed()}
            onSeedDone={() => setPromptSeed("")}
            skills={props.skills}
            files={props.files}
          />
        </box>
        </box>
        <Show when={dimensions().width >= 100}>
          <Sidebar
            title={props.title}
            tokens={stats().tokens}
            maxContextTokens={stats().maxContextTokens}
            turns={stats().turnCount ?? 0}
            messages={state.messages.length}
            createdAt={stats().createdTime ?? ""}
            budget={stats().budget ?? null}
            jobs={state.jobs}
            goal={state.goal}
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
        agent={currentAgent()}
        agentName={agentName(currentAgent())}
        mode={currentMode()}
        readonly={currentReadonly()}
      />
    </box>
  )
}
