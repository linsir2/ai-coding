/**
 * Jobs dialog (D-1b) — what is running, and how to stop it.
 *
 * dsh keeps a roster row for its background jobs and oc merges running shells
 * into a task list; before this the only thing our operator saw was a toast
 * after a job had already ended, which is the one moment they cannot act on it.
 * A dev server you started by accident had to be found with `ps`.
 *
 * The rows come from two places on purpose. `session.jobs` is read when the
 * dialog opens (the client that connected after a job started has no
 * `job.changed` history to catch up on), and the open dialog also refreshes
 * when a change arrives — subscribing to the same wire event the state layer
 * already folds, rather than polling, because the backend pushes.
 *
 * Stopping goes through `/tasks stop <id>` — the REPL command that already
 * stops sub-agent runs. One way to stop a thing means the operator, the model's
 * `job_kill`, and this panel all pass through the same guards and read back the
 * same words. Confirmation is the same two-press arming the memory panel uses,
 * for the same reason: a stray Enter in a list must not kill a build.
 */

import { createSignal, onCleanup, onMount } from "solid-js"
import { DialogSelect } from "./dialog-select"
import type { IpcClient } from "../ipc"
import { type JobRow, toJobRow } from "../state/session"
import type { SelectAction, SelectOption } from "../util/dialog-select"

const CONFIRM_NONE = ""

/**
 * What a press on a row means. Split out because the key path itself cannot be
 * driven in the render harness (the same note dialog-mcp's test carries) -- the
 * decision can, and it is the part that could be wrong about arming.
 */
export function armDecision(armed: string, value: string): "arm" | "fire" {
  return armed === value ? "fire" : "arm"
}

/** The title line: id and command, clipped so a long command stays readable. */
export function jobRowLine(job: JobRow): { title: string; description: string } {
  const clip = job.command.length <= 70 ? job.command : `${job.command.slice(0, 69)}…`
  const origin = job.adopted ? "上次运行遗留 · " : ""
  const detail = job.detail ? ` · ${job.detail}` : ""
  return {
    title: `${job.id}  ${clip}`,
    description: `${origin}${job.status}${detail} · ${job.logPath}`,
  }
}

export function DialogJobs(props: { client: IpcClient }) {
  const [rows, setRows] = createSignal<JobRow[]>([])
  const [error, setError] = createSignal("")
  const [armed, setArmed] = createSignal(CONFIRM_NONE)

  const refresh = async (): Promise<void> => {
    try {

      const data = (await props.client.request("session.jobs")) as {
        jobs?: unknown[]
        available?: boolean
      }
      setRows((data.jobs ?? []).map(toJobRow))
      setError(data.available === false ? "这个前端没有 job 注册表" : "")
    } catch (cause) {
      setError(String(cause))
    }
  }

  onMount(refresh)

  // A job that settles while the panel is open should leave the list without
  // anyone pressing a key.
  const unsubscribe = props.client.onEvent((event: { type: string }) => {
    if (event.type === "job.changed") void refresh()
  })
  onCleanup(unsubscribe)

  const options = (): SelectOption[] =>
    rows().map((job) => {
      const row = jobRowLine(job)
      const pending = armed() === job.id
      return {
        title: pending ? `再按一次回车停止 ${job.id}` : row.title,
        value: job.id,
        description: pending ? undefined : row.description,
        category: job.status === "running" ? "运行中" : "已结束",
        danger: pending,
      }
    })

  /** Stop, armed: the first Enter arms the row, the second one fires. */
  const stop = async (option: SelectOption) => {
    if (armDecision(armed(), option.value) === "arm") {
      setArmed(option.value)
      return
    }
    setArmed(CONFIRM_NONE)
    try {
      await props.client.request("session.command", { command: `/tasks stop ${option.value}` })
      setError("")
    } catch (cause) {
      setError(String(cause))
    }
    await refresh()
  }

  const actions = (): readonly SelectAction[] => [
    {
      key: "ctrl+d",
      title: "stop",
      onTrigger: (option) => option && void stop(option),
    },
  ]

  return (
    <DialogSelect
      title="后台任务"
      options={options()}
      onSelect={(option) => void stop(option)}
      actions={actions()}
      footer={
        [
          error(),
          rows().length === 0 ? "没有后台任务" : "",
          "enter 停止（再按一次确认） · ctrl+d stop · esc close",
        ]
          .filter(Boolean)
          .join(" · ") || undefined
      }
    />
  )
}
