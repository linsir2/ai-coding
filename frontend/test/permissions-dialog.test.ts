/**
 * TDD (A3.1): the permissions dialog's pure half.
 *
 * The dialog only *reads* through ``permissions.state``; every switch it
 * offers is the backend's own command (``/permissions auto|confirm|clear``)
 * so the provider re-arm (I1) keeps a single home.
 */

import { describe, expect, test } from "bun:test"
import {
  policyLabel,
  grantLine,
  toggledPolicy,
  toggleCommand,
  ruleLine,
  RESET_SESSION_COMMAND,
} from "../src/util/permissions-dialog"

describe("policyLabel", () => {
  test("names the two policies the backend has (J1)", () => {
    expect(policyLabel("auto")).toBe("Auto-approve")
    expect(policyLabel("confirm")).toBe("Confirm each call")
  })
})

describe("grantLine", () => {
  test("renders a grant key the way the key reads", () => {
    expect(grantLine("write:/tmp/a.py")).toBe("write · /tmp/a.py")
  })

  test("an empty grant list says so instead of rendering nothing", () => {
    expect(grantLine("")).toBe("(none)")
  })
})

describe("the session layer", () => {
  test("a rule reads as the decision it makes", () => {
    expect(ruleLine({ action: "edit", resource: "*.env", effect: "deny" })).toBe(
      "deny edit *.env",
    )
    expect(
      ruleLine({
        action: "bash",
        resource: "rm -rf *",
        effect: "ask",
        reason: "recursive",
      }),
    ).toBe("ask bash rm -rf * · recursive")
  })

  test("reset is the backend command, not a second write path", () => {
    expect(RESET_SESSION_COMMAND).toBe("/permissions reset")
  })
})

describe("the policy toggle (OpenCode's prompt/autoaccept pair)", () => {
  test("toggling moves to the other policy and back", () => {
    expect(toggledPolicy("confirm")).toBe("auto")
    expect(toggledPolicy("auto")).toBe("confirm")
  })

  test("the toggle dispatches the command for the policy it moves to", () => {
    expect(toggleCommand("confirm")).toBe("/permissions auto")
    expect(toggleCommand("auto")).toBe("/permissions confirm")
  })
})
