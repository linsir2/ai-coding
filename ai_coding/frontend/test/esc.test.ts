import { describe, expect, test } from "bun:test"
import { escStep } from "../src/util/esc"

describe("escStep", () => {
  test("first esc arms without interrupting", () => {
    expect(escStep(false)).toEqual({ armed: true, interrupt: false })
  })

  test("second esc while armed interrupts and disarms", () => {
    expect(escStep(true)).toEqual({ armed: false, interrupt: true })
  })
})
