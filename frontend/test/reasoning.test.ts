import { describe, expect, test } from "bun:test"
import { reasoningHeadline } from "../src/util/reasoning"

/**
 * A4 T6: the collapsed "+ Thought" header has to say what the model was
 * thinking, or folding it away throws the only signal that it thought at all.
 * The rule is deliberately one line of prose, not a summary model.
 */
describe("reasoningHeadline", () => {
  test("an English sentence stops at its full stop", () => {
    expect(reasoningHeadline("The user wants me to say hi. Then answer.")).toBe(
      "The user wants me to say hi.",
    )
  })

  test("a Chinese sentence stops at its full-width stop", () => {
    expect(reasoningHeadline("用户问的是分支。\n先跑命令看看。")).toBe("用户问的是分支。")
  })

  test("a decimal point inside a sentence is not a sentence end", () => {
    expect(reasoningHeadline("The model is qwen3.7 so I should keep it short.")).toBe(
      "The model is qwen3.7 so I should keep it short.",
    )
  })

  test("blank leading lines are skipped", () => {
    expect(reasoningHeadline("\n\n   \nfirst real line")).toBe("first real line")
  })

  test("a long first sentence is cut with an ellipsis", () => {
    const headline = reasoningHeadline("word ".repeat(40), 20)
    expect(headline.length).toBeLessThanOrEqual(21) // 20 chars + "…"
    expect(headline.endsWith("…")).toBe(true)
  })

  test("empty and whitespace-only reasoning have no headline", () => {
    expect(reasoningHeadline("")).toBe("")
    expect(reasoningHeadline("   \n\t ")).toBe("")
  })
})