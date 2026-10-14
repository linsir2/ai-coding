/**
 * TDD (M5): the leader key — registration is renderer-free so the library's
 * own test host can drive real key presses (no terminal needed).
 *
 * The contract: OpenCode's trigger and window (ctrl+x, 2000ms), a
 * ``<leader>m`` binding dispatches only after the leader key, and the palette
 * formatter renders the token as ``ctrl+x m``.
 */

import { describe, expect, test } from "bun:test"
import { formatCommandBindings } from "@opentui/keymap/extras"
import { createTestKeymap } from "@opentui/keymap/testing"
import { LEADER_KEY, LEADER_TIMEOUT_MS, registerAppKeymapAddons } from "../src/keymap-addons"

function harnessWithLeader() {
  const harness = createTestKeymap({ defaultKeys: true })
  registerAppKeymapAddons(harness.keymap)
  return harness
}

describe("composer paste key", () => {
  // r21: ctrl+v was the only ctrl binding in the app with no test at all, so
  // "paste does nothing" could have meant "the key never fires" and nothing in
  // the suite could tell the two apart.
  test("ctrl+v dispatches its command", () => {
    const harness = createTestKeymap({ defaultKeys: true })
    let runs = 0
    harness.keymap.registerLayer({
      commands: [{ name: "prompt.paste", run: () => void (runs += 1) }],
      bindings: [{ key: "ctrl+v", cmd: "prompt.paste" }],
    })

    harness.host.press("v", { ctrl: true })

    expect(runs).toBe(1)
    harness.cleanup()
  })
})

describe("leader key", () => {
  test("uses OpenCode's trigger and timeout", () => {
    expect(LEADER_KEY).toBe("ctrl+x")
    expect(LEADER_TIMEOUT_MS).toBe(2000)
  })

  test("ctrl+x then m dispatches a <leader>m binding", () => {
    const harness = harnessWithLeader()
    let runs = 0
    harness.keymap.registerLayer({
      commands: [
        {
          name: "test.model",
          title: "Model",
          run: () => {
            runs += 1
          },
        },
      ],
      bindings: [{ key: "<leader>m", cmd: "test.model" }],
    })
    harness.host.press("x", { ctrl: true })
    expect(runs).toBe(0)
    harness.host.press("m")
    expect(runs).toBe(1)
    harness.cleanup()
  })

  test("m alone does not dispatch the leader binding", () => {
    const harness = harnessWithLeader()
    let runs = 0
    harness.keymap.registerLayer({
      commands: [
        {
          name: "test.model",
          title: "Model",
          run: () => {
            runs += 1
          },
        },
      ],
      bindings: [{ key: "<leader>m", cmd: "test.model" }],
    })
    harness.host.press("m")
    expect(runs).toBe(0)
    harness.cleanup()
  })

  test("the shortcut formatter renders the token as ctrl+x", () => {
    const harness = harnessWithLeader()
    harness.keymap.registerLayer({
      commands: [{ name: "test.model", title: "Model", run: () => {} }],
      bindings: [{ key: "<leader>m", cmd: "test.model" }],
    })
    const bindings = harness.keymap.getCommandBindings({
      visibility: "registered",
      commands: ["test.model"],
    })
    const formatted = formatCommandBindings(bindings.get("test.model"), {
      tokenDisplay: { leader: LEADER_KEY },
    })
    expect(formatted).toBe("ctrl+x m")
    harness.cleanup()
  })
})
