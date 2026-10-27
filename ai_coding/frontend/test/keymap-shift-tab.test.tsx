import { describe, expect, test } from "bun:test"
import { parseKeypress } from "@opentui/core"
import { Keymap } from "@opentui/keymap"
import { registerDefaultKeys, registerEnabledFields, registerMetadataFields } from "@opentui/keymap/addons"

/**
 * 19 §3.1: the mode toggle is bound to ``shift+tab``.  The renderer-bound
 * test harness cannot deliver key events (no focused-target wiring), so this
 * pins the contract the binding relies on, headlessly: the opentui parser
 * turns the terminal's ``ESC[Z`` into ``{name:"tab",shift:true}``, and a
 * keymap built exactly like ``createDefaultOpenTuiKeymap`` (stub host with the
 * same field registrations) fires a ``shift+tab`` binding for it — while a
 * plain tab does not.  The live pipeline (tmux, raw ESC[Z bytes) was verified
 * on top of this.
 */
function headlessKeymap() {
  const root = {}
  const listeners: ((event: object) => void)[] = []
  const host = {
    metadata: { platform: "linux", primaryModifier: "ctrl", modifiers: {} },
    rootTarget: root,
    isDestroyed: false,
    getFocusedTarget: () => root,
    getParentTarget: () => null,
    isTargetDestroyed: () => false,
    onKeyPress: (listener: (event: object) => void) => {
      listeners.push(listener)
      return () => {
        const index = listeners.indexOf(listener)
        if (index >= 0) listeners.splice(index, 1)
      }
    },
    onKeyRelease: () => () => {},
    onFocusChange: () => () => {},
    onTargetDestroy: () => () => {},
    createCommandEvent: () => ({ preventDefault: () => {}, stopPropagation: () => {} }),
  }
  const keymap = new Keymap(host as never)
  registerDefaultKeys(keymap)
  registerEnabledFields(keymap)
  registerMetadataFields(keymap)
  const feed = (sequence: string) => {
    const parsed = parseKeypress(sequence) as object
    for (const listener of listeners) {
      listener({ ...parsed, preventDefault: () => {}, stopPropagation: () => {} })
    }
  }
  return { keymap, feed }
}

describe("the mode toggle stroke (19 §3.1)", () => {
  test("ESC[Z parses to shift+tab and fires the cycle; plain tab does not", () => {
    const { keymap, feed } = headlessKeymap()
    const fired: string[] = []
    keymap.registerLayer({
      enabled: true,
      commands: [{ name: "probe.cycle", run: () => fired.push("cycle") }],
      bindings: [{ key: "shift+tab", cmd: "probe.cycle" }],
    } as never)

    expect(parseKeypress("\x1b[Z")).toMatchObject({ name: "tab", shift: true })

    feed("\x1b[Z")
    expect(fired).toEqual(["cycle"])

    feed("\t")
    expect(fired).toEqual(["cycle"])

    feed("\x1b[Z")
    expect(fired).toEqual(["cycle", "cycle"])
  })
})
