/**
 * TDD (A2.2, I5): the exit sequence every quit path shares.
 *
 * ``process.exit`` alone leaves the terminal in raw mode — OpenTUI's
 * ``process.on("exit")`` only drops a console cache, so the tty restore lives
 * in ``renderer.destroy()`` (``lib.destroyRenderer``).  Order matters: the
 * socket closes, the renderer is torn down so the shell is usable again, only
 * then does the process leave — and any message meant for the user lands after
 * the teardown, on a restored terminal.
 */

import { describe, expect, test } from "bun:test"
import { quitSequence } from "../src/util/exit"

function recorder() {
  const calls: string[] = []
  return {
    calls,
    deps: {
      close: () => calls.push("close"),
      destroy: () => calls.push("destroy"),
      exit: () => calls.push("exit"),
      error: (message: string) => calls.push(`error:${message}`),
    },
  }
}

describe("quitSequence", () => {
  test("closes the socket, restores the terminal, then exits", () => {
    const { calls, deps } = recorder()
    quitSequence(deps)
    expect(calls).toEqual(["close", "destroy", "exit"])
  })

  test("a message for the user lands after the teardown, not into the alt screen", () => {
    const { calls, deps } = recorder()
    quitSequence(deps, { message: "backend connection closed" })
    expect(calls).toEqual([
      "close",
      "destroy",
      "error:backend connection closed",
      "exit",
    ])
  })
})