/**
 * Toast (M5) — OpenCode's top-right notification: a coloured left border, a
 * raised surface, a 5-second lifetime and a queue so a burst of notices shows
 * one at a time.  Hovering pauses the timer; moving away resumes it.
 *
 * The wire events that reach it (``provider.retried``, ``provider.fallback``,
 * ``background.done``) are already mapped by the backend's EventBridge; this
 * component only owns the presentation.
 */

import { TextAttributes } from "@opentui/core"
import { Show, createContext, createSignal, useContext, type JSX } from "solid-js"
import { useTerminalDimensions } from "@opentui/solid"
import { theme } from "../theme/tokens"
import type { ToastItem } from "../util/toast"
import { dequeueToast, enqueueToast } from "../util/toast"

interface ToastApi {
  show: (item: ToastItem) => void
}

const ToastContext = createContext<ToastApi>()

export function useToast(): ToastApi {
  const api = useContext(ToastContext)
  if (!api) throw new Error("useToast must be used inside ToastProvider")
  return api
}

const DEFAULT_DURATION = 5000

export function ToastProvider(props: { children: JSX.Element }) {
  const [current, setCurrent] = createSignal<ToastItem | null>(null)
  const [queue, setQueue] = createSignal<ToastItem[]>([])
  const [paused, setPaused] = createSignal(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let remaining = DEFAULT_DURATION
  let startedAt = 0

  const clearTimer = () => {
    if (timer !== undefined) {
      clearTimeout(timer)
      timer = undefined
    }
  }

  const dismiss = () => {
    clearTimer()
    const { current: next, rest } = dequeueToast(queue())
    setQueue(rest)
    setCurrent(next)
    if (next) {
      remaining = DEFAULT_DURATION
      if (!paused()) timer = setTimeout(dismiss, DEFAULT_DURATION)
    }
  }

  const api: ToastApi = {
    show(item) {
      const wasIdle = current() === null
      if (wasIdle) {
        setCurrent(item)
        remaining = DEFAULT_DURATION
        if (!paused()) timer = setTimeout(dismiss, DEFAULT_DURATION)
      } else {
        setQueue((q) => enqueueToast(q, item))
      }
    },
  }

  return (
    <ToastContext.Provider value={api}>
      {props.children}
      <Show when={current()}>
        {(toast) => (
          <ToastSurface
            toast={toast()}
            paused={paused()}
            onHover={(value) => {
              setPaused(value)
              if (value) {
                clearTimer()
                remaining = Math.max(0, remaining - (Date.now() - startedAt))
              } else {
                startedAt = Date.now()
                if (remaining > 0) timer = setTimeout(dismiss, remaining)
              }
            }}
          />
        )}
      </Show>
    </ToastContext.Provider>
  )
}

function ToastSurface(props: {
  toast: ToastItem
  paused: boolean
  onHover: (hovered: boolean) => void
}) {
  const dimensions = useTerminalDimensions()
  const borderColor = () => {
    switch (props.toast.variant) {
      case "success":
        return theme.feedbackSuccess
      case "warning":
        return theme.feedbackWarning
      case "error":
        return theme.feedbackError
      default:
        return theme.feedbackInfo
    }
  }

  return (
    <box
      position="absolute"
      top={1}
      right={2}
      maxWidth={Math.min(60, dimensions().width - 6)}
      borderColor={borderColor()}
      border={["left", "right"]}
      backgroundColor={theme.backgroundRaisedHigh}
      onMouseOver={() => props.onHover(true)}
      onMouseOut={() => props.onHover(false)}
    >
      <Show
        when={props.toast.title}
        fallback={<text fg={theme.textBase}>{props.toast.message}</text>}
      >
        <box flexDirection="column" width="100%" marginBottom={1}>
          <text attributes={TextAttributes.BOLD} fg={theme.textBase}>
            {props.toast.title}
          </text>
          <text fg={theme.textBase}>{props.toast.message}</text>
        </box>
      </Show>
    </box>
  )
}
