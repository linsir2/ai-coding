/**
 * ctrl+c is selection-aware: a live selection is copied, and only an empty
 * one exits.  A failed clipboard write must never take the app down with it.
 */

import { describe, expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { copyOrExit, selectionText, type SelectionRenderer } from "../src/util/selection"

function renderer(
  selected: string | null,
  editor: string | null = null,
  osc52: (text: string) => boolean = () => true,
): SelectionRenderer & { osc52: string[] } {
  const osc52Text: string[] = []
  return {
    osc52: osc52Text,
    getSelection: () => (selected === null ? null : { getSelectedText: () => selected }),
    currentFocusedEditor: editor === null ? null : { getSelectedText: () => editor },
    copyToClipboardOSC52: (text) => {
      osc52Text.push(text)
      return osc52(text)
    },
  }
}

describe("selectionText", () => {
  test("reads the renderer's global selection", () => {
    expect(selectionText(renderer("hello"))).toBe("hello")
  })

  test("falls back to the focused editor when there is no global selection", () => {
    expect(selectionText(renderer(null, "prompt text"))).toBe("prompt text")
  })

  test("a renderer without selection support has nothing selected", () => {
    expect(selectionText({})).toBe("")
  })
})

describe("copyOrExit", () => {
  test("copies a selection and does not exit", async () => {
    const copied: string[] = []
    let exits = 0

    await copyOrExit(renderer("selected"), () => void (exits += 1), (text) => {
      copied.push(text)
    })

    expect(copied).toEqual(["selected"])
    expect(exits).toBe(0)
  })

  test("an empty selection still exits", async () => {
    let exits = 0
    let writes = 0

    await copyOrExit(renderer(null), () => void (exits += 1), () => {
      writes += 1
    })

    expect(exits).toBe(1)
    expect(writes).toBe(0)
  })

  test("a focused editor selection is copied", async () => {
    const copied: string[] = []
    let exits = 0

    await copyOrExit(renderer(null, "typed"), () => void (exits += 1), (text) => {
      copied.push(text)
    })

    expect(copied).toEqual(["typed"])
    expect(exits).toBe(0)
  })

  test("a failed write falls back to OSC52 and does not exit", async () => {
    const target = renderer("selected")
    let exits = 0

    await copyOrExit(target, () => void (exits += 1), () => {
      throw new Error("no host clipboard")
    })

    expect(target.osc52).toEqual(["selected"])
    expect(exits).toBe(0)
  })

  test("a write reporting failure also falls back to OSC52", async () => {
    const target = renderer("selected")
    let exits = 0

    await copyOrExit(target, () => void (exits += 1), () => false)

    expect(target.osc52).toEqual(["selected"])
    expect(exits).toBe(0)
  })

  test("OSC52 failure is swallowed and still does not exit", async () => {
    const target = renderer("selected", null, () => {
      throw new Error("no terminal")
    })
    let exits = 0

    await copyOrExit(target, () => void (exits += 1), () => false)

    expect(exits).toBe(0)
  })
})

describe("app key bindings", () => {
  const source = readFileSync(
    fileURLToPath(new URL("../src/app.tsx", import.meta.url)),
    "utf8",
  )

  test("ctrl+c runs the selection-aware command, not app.exit", () => {
    expect(source).toContain('{ key: "ctrl+c", cmd: "app.copy-or-exit" }')
    expect(source).not.toContain('{ key: "ctrl+c", cmd: "app.exit" }')
    expect(source).toContain('name: "app.copy-or-exit"')
  })

  test("the quit binding still runs app.exit", () => {
    expect(source).toContain('{ key: "<leader>q", cmd: "app.exit" }')
  })
})
