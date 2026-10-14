/**
 * r21: how a terminal paste actually reaches the app -- pinned with opentui's
 * own test renderer rather than assumed.
 *
 * Three facts decide the whole implementation, and all three were guessed wrong
 * before this file existed:
 *
 *   1. A bracketed paste fires `onPaste` only when the renderable has focus.
 *      Without that, nothing arrives and every conclusion drawn from a test
 *      that forgot to focus is backwards.
 *   2. An EMPTY bracketed paste does fire `onPaste`.  That matters because a
 *      terminal asked to paste a clipboard holding a picture and no text has
 *      nothing to forward -- the empty paste is the only cue it can give, and
 *      it is the cue to go and read the host clipboard.
 *   3. ctrl+v is NOT a paste event; it stays an ordinary key.  So the two
 *      paths are independent and both are needed: onPaste for terminals that
 *      forward the picture, the keybinding for terminals that pass ctrl+v
 *      through untouched.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { testRender } from "@opentui/solid"
import { createSignal } from "solid-js"

interface Seen {
  events: { bytes: number; mime: string }[]
}

async function focusedTextarea() {
  const base = await createTestRenderer({ width: 60, height: 10 })
  const seen: Seen = { events: [] }
  const [value, setValue] = createSignal("")

  const setup = await testRender(
    () => (
      <box flexDirection="column">
        <textarea
          ref={(element) => queueMicrotask(() => element.focus?.())}
          value={value()}
          onInput={setValue}
          onSubmit={() => {}}
          onPaste={(event) => {
            seen.events.push({
              bytes: event.bytes.length,
              mime: event.metadata?.mimeType ?? "(none)",
            })
          }}
        />
      </box>
    ),
    { width: 60, height: 10, renderer: base.renderer },
  )
  await setup.flush()
  return { seen, setup, destroy: () => base.renderer.destroy() }
}

describe("a terminal paste reaches the app as", () => {
  test("an onPaste event carrying the bytes, when there are any", async () => {
    const harness = await focusedTextarea()
    try {
      await harness.setup.mockInput.pasteBracketedText("hello world")
      await harness.setup.flush()

      expect(harness.seen.events.length).toBeGreaterThan(0)
      expect(harness.seen.events[0]!.bytes).toBeGreaterThan(0)
    } finally {
      harness.destroy()
    }
  })

  test("an onPaste event with no bytes, when the clipboard had no text", async () => {
    const harness = await focusedTextarea()
    try {
      await harness.setup.mockInput.pasteBracketedText("")
      await harness.setup.flush()

      // The cue to consult the host clipboard.  Absent this, a terminal that
      // cannot carry an image gives the app no signal at all.
      expect(harness.seen.events).toHaveLength(1)
      expect(harness.seen.events[0]!.bytes).toBe(0)
    } finally {
      harness.destroy()
    }
  })

  test("a plain key, not a paste, when ctrl+v arrives as itself", async () => {
    const harness = await focusedTextarea()
    try {
      harness.setup.mockInput.pressKey("v", { ctrl: true })
      await harness.setup.flush()

      // Proves the keybinding is not dead code: ctrl+v does not turn into a
      // paste event, it goes to whatever handles keys.
      expect(harness.seen.events).toHaveLength(0)
    } finally {
      harness.destroy()
    }
  })
})
