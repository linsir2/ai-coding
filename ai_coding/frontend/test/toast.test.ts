import { describe, expect, test } from "bun:test"
import type { ToastItem } from "../src/util/toast"
import { dequeueToast, enqueueToast } from "../src/util/toast"

const item = (message: string, variant: ToastItem["variant"] = "info"): ToastItem => ({
  message,
  variant,
})

describe("enqueueToast", () => {
  test("appends to the queue", () => {
    const queue = [item("a")]
    const next = enqueueToast(queue, item("b"))
    expect(next.map((t) => t.message)).toEqual(["a", "b"])
  })

  test("does not mutate the original queue", () => {
    const queue: ToastItem[] = []
    enqueueToast(queue, item("a"))
    expect(queue).toEqual([])
  })
})

describe("dequeueToast", () => {
  test("returns the first item and the rest", () => {
    const queue = [item("a"), item("b"), item("c")]
    const { current, rest } = dequeueToast(queue)
    expect(current?.message).toBe("a")
    expect(rest.map((t) => t.message)).toEqual(["b", "c"])
  })

  test("returns null current for an empty queue", () => {
    const { current, rest } = dequeueToast([])
    expect(current).toBeNull()
    expect(rest).toEqual([])
  })
})
