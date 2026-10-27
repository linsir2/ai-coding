/**
 * M3 T11: a submitted plan can be read back after the review is gone.
 *
 * The durable home of the plan is the tool call's arguments; live and replay
 * both hand the same `params` to the transcript.  These tests pin the pure
 * reader that turns those params into the rows a plan card renders -- the same
 * reader the review card uses, so the two cannot drift apart.
 */

import { describe, expect, test } from "bun:test"
import { submittedPlan } from "../src/util/ask"

describe("submittedPlan", () => {
  test("reads the plan a present_plan call carries", () => {
    const plan = submittedPlan({
      plan: "# 计划\n正文",
      items: [
        { content: "建目录", verify: "test -d hotel", depends_on: [0] },
        { content: "写文件", verify: "" },
      ],
    })

    expect(plan).toEqual({
      plan: "# 计划\n正文",
      items: [
        { content: "建目录", verify: "test -d hotel", dependsOn: [1] },
        { content: "写文件", verify: "", dependsOn: [] },
      ],
    })
  })

  test("a missing plan means there is no plan", () => {
    expect(submittedPlan({ items: [{ content: "x" }] })).toBeNull()
    expect(submittedPlan({ plan: "   ", items: [] })).toBeNull()
  })

  test("shapeless rows are dropped, never guessed at", () => {
    const plan = submittedPlan({
      plan: "# p",
      items: [
        null,
        "nope",
        { verify: "true" },
        { content: "  " },
        { content: "kept" },
      ],
    })
    expect(plan?.items).toEqual([{ content: "kept", verify: "", dependsOn: [] }])
  })

  test("depends_on may be camelCase or snake_case, as in the review card", () => {
    const plan = submittedPlan({
      plan: "# p",
      items: [
        { content: "a" },
        { content: "b", dependsOn: [1] },
        { content: "c", depends_on: [1, 2] },
      ],
    })
    expect(plan?.items.map((item) => item.dependsOn)).toEqual([[], [1], [2, 3]])
  })

  test("a numeric step is kept, not silently dropped", () => {
    // The backend accepts any truthy content and normalizes it with str();
    // dropping it here would renumber every later step.
    const plan = submittedPlan({
      plan: "# p",
      items: [{ content: 42 }, { content: "next", depends_on: [0] }],
    })
    expect(plan?.items.map((item) => item.content)).toEqual(["42", "next"])
    expect(plan?.items[1]?.dependsOn).toEqual([1])
  })

  test("both dependency spellings never merge", () => {
    const plan = submittedPlan({
      plan: "# p",
      items: [
        { content: "a" },
        { content: "b", dependsOn: [1], depends_on: [0] },
      ],
    })
    // The backend reads depends_on; the display must show what it executed
    // (0-based in the payload, 1-based on the card).
    expect(plan?.items[1]?.dependsOn).toEqual([1])
  })
})
