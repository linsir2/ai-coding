/**
 * Selection-aware ctrl+c: copy what is selected, otherwise quit.
 *
 * The renderer owns the selection (mouse-drag or an editor's own range), so
 * this is the one place that reads it.  The terminal's OSC52 path runs first
 * because the native clipboard can hang (measured: a 2s timeout under a VS
 * Code terminal); the host clipboard is the fallback.  Copy failures are never
 * fatal: even both paths failing leaves the app running.
 */

import {
  createClipboard,
  createHostClipboard,
  createRendererClipboardAdapter,
  type RendererClipboardBoundary,
} from "@opentui/core"

interface TextSelection {
  getSelectedText?: () => string
}

export interface SelectionRenderer {
  getSelection?: () => TextSelection | null
  currentFocusedEditor?: TextSelection | null
  copyToClipboardOSC52?: (text: string) => boolean
}

export type ClipboardWrite = (text: string) => boolean | void | Promise<boolean | void>

/** The selected text, preferring the renderer's global selection. */
export function selectionText(renderer: SelectionRenderer): string {
  try {
    const text = renderer.getSelection?.()?.getSelectedText?.() ?? ""
    if (text.length > 0) return text
  } catch {
    /* a renderer without selection support simply has nothing selected */
  }
  try {
    return renderer.currentFocusedEditor?.getSelectedText?.() ?? ""
  } catch {
    return ""
  }
}

async function writeBestAvailable(renderer: SelectionRenderer, text: string): Promise<boolean> {
  try {
    const clipboard = createClipboard({
      host: createHostClipboard(),
      terminal: createRendererClipboardAdapter(renderer as RendererClipboardBoundary),
    })
    try {
      const result = await clipboard.writeText(text, {
        destination: "best-available",
        signal: AbortSignal.timeout(2000),
      })
      return result.host.status === "written" || result.terminal.status === "attempted"
    } finally {
      await clipboard.dispose()
    }
  } catch {
    return false
  }
}

/**
 * Copy when there is a selection; otherwise exit.
 *
 * ``write`` is injectable for tests.  The real path tries OSC52 first: the
 * host clipboard backend can hang (measured: 2s timeout under a VS Code
 * terminal), and a user who pastes immediately would get the old clipboard.
 * OSC52 is a single terminal escape sequence and is the fast path in a TUI;
 * the host backend is the fallback for terminals that do not support it.
 *
 * A ``false`` result or a rejected write falls back to the other clipboard
 * path and then gives up — it never calls ``exit``.
 */
export async function copyOrExit(
  renderer: SelectionRenderer,
  exit: () => void,
  write?: ClipboardWrite,
): Promise<void> {
  const text = selectionText(renderer)
  if (text.length === 0) {
    exit()
    return
  }

  // A test-injected writer keeps the old ordering (so tests can exercise the
  // fallback deterministically).  Production goes terminal-first.
  if (write === undefined) {
    try {
      if (renderer.copyToClipboardOSC52?.(text)) return
    } catch {
      /* fall through to the host clipboard */
    }
  }

  let copied = false
  try {
    copied = write ? (await write(text)) !== false : await writeBestAvailable(renderer, text)
  } catch {
    copied = false
  }

  if (!copied) {
    try {
      renderer.copyToClipboardOSC52?.(text)
    } catch {
      /* the OS clipboard is unavailable; the app must keep running */
    }
  }
}
