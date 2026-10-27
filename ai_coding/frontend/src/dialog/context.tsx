/**
 * Dialog host (M3) — the port of OpenCode's ui/dialog.tsx, reduced to what
 * this app needs: one dialog at a time (no nested/drill-down dialogs yet).
 *
 *   open(el, size?) -> replaces whatever is showing, remembering focus
 *   close()         -> hides it and hands focus back to the previous element
 *   isOpen()        -> drives the base-command gate (see app.tsx)
 *
 * The overlay is an absolute full-screen box with a translucent backdrop; the
 * frame picks a width preset and the raised surface.  Esc/ctrl+c are bound to
 * the host layer, enabled only while a dialog is open, so the base layer's
 * esc (interrupt) and ctrl+c (exit) stay quiet under a dialog.
 */

import { RGBA, type CliRenderer, type Renderable } from "@opentui/core"
import { useBindings } from "@opentui/keymap/solid"
import { useTerminalDimensions } from "@opentui/solid"
import { Show, createContext, createSignal, useContext, type JSX } from "solid-js"
import { theme } from "../theme/tokens"

export type DialogSize = "medium" | "large" | "xlarge"

/** OpenCode's width presets (ui/dialog.tsx::dialogWidth). */
export function dialogWidth(size: DialogSize): number {
  if (size === "xlarge") return 116
  if (size === "large") return 88
  return 60
}

interface DialogApi {
  /** The element is a thunk so the dialog is created inside the provider's
   * reactive owner (contexts like useDialog/useKeymap must resolve). */
  open(element: () => JSX.Element, size?: DialogSize): void
  close(): void
  isOpen(): boolean
}

const DialogContext = createContext<DialogApi>()

export function DialogProvider(props: { renderer: CliRenderer; children: JSX.Element }) {
  const [element, setElement] = createSignal<(() => JSX.Element) | null>(null)
  const [size, setSize] = createSignal<DialogSize>("medium")
  const dimensions = useTerminalDimensions()
  let previous: Renderable | null = null

  const api: DialogApi = {
    open(next, nextSize = "medium") {
      // Remember the background focus once per dialog chain: a palette that
      // opens another dialog must not remember its own search input.
      if (previous === null) previous = props.renderer.currentFocusedRenderable
      setSize(nextSize)
      setElement(() => next) // function form: the value IS the thunk
    },
    close() {
      setElement(null)
      if (previous === null) return
      // Restore focus after teardown, unless a follow-up command opened
      // another dialog in the same tick (the palette's close + run).  The
      // target may have unmounted (a route switch); its screen focuses itself.
      setTimeout(() => {
        if (element()) return
        const target = previous
        previous = null
        try {
          ;(target as { focus?: () => void }).focus?.()
        } catch {
          /* the previous focus owner is gone; nothing to restore */
        }
      }, 1)
    },
    isOpen: () => element() !== null,
  }

  useBindings(() => ({
    enabled: api.isOpen(),
    commands: [{ name: "dialog.close", desc: "Close dialog", run: () => api.close() }],
    bindings: [
      { key: "escape", cmd: "dialog.close" },
      { key: "ctrl+c", cmd: "dialog.close" },
    ],
  }))

  return (
    <DialogContext.Provider value={api}>
      {props.children}
      <Show when={element()}>
        <box
          position="absolute"
          left={0}
          top={0}
          width="100%"
          height="100%"
          zIndex={3000}
          alignItems="center"
          paddingTop={Math.floor(dimensions().height / 4)}
          backgroundColor={RGBA.fromInts(0, 0, 0, 150)}
        >
          <box
            width={dialogWidth(size())}
            maxWidth={dimensions().width - 2}
            backgroundColor={theme.backgroundRaised}
            paddingTop={1}
          >
            {element()?.()}
          </box>
        </box>
      </Show>
    </DialogContext.Provider>
  )
}

export function useDialog(): DialogApi {
  const api = useContext(DialogContext)
  if (!api) throw new Error("useDialog must be used inside DialogProvider")
  return api
}
