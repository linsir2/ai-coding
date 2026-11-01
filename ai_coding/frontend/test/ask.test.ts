/**
 * TDD (M6.5): the ask panel's pure half — wire params in, the answer out.
 *
 * The panel answers a server-initiated ``user_prompt.ask``
 * (handlers.py::_wire_user_prompt).  The answer is the plain text the tool
 * renders to the model, so an empty answer means "nobody answered" and the
 * model decides itself (security/user_prompt.py:80 — ``reason="empty"``).
 */

import { describe, expect, test } from "bun:test"
import {
  parseAskRequest,
  answerForSelection,
  ASK_SKIP,
} from "../src/util/ask"

describe("parseAskRequest", () => {
  test("reads the question and the options", () => {
    expect(parseAskRequest({ question: "哪个文件？", options: ["a.py", "b.py"] })).toEqual({
      question: "哪个文件？",
      options: ["a.py", "b.py"],
      // The wire's payload keys, always explicit.
      detail: "",
      intent: "",
      data: {},
    })
  })

  test("a detail and an intent survive parsing", () => {
    const request = parseAskRequest({
      question: "批准这个计划并开始执行？",
      options: ["开始执行", "继续规划"],
      detail: "# 计划\n正文",
      intent: "plan_review",
    })
    expect(request.detail).toBe("# 计划\n正文")
    expect(request.intent).toBe("plan_review")
  })

  test("shapeless detail and intent are dropped", () => {
    const request = parseAskRequest({ question: "q", detail: 3, intent: ["x"] })
    expect(request.detail).toBe("")
    expect(request.intent).toBe("")
  })

  test("a missing or shapeless options list means no options", () => {
    expect(parseAskRequest({ question: "q" }).options).toEqual([])
    expect(parseAskRequest({ question: "q", options: "nope" }).options).toEqual([])
  })

  test("options are strings; anything else is dropped", () => {
    expect(parseAskRequest({ question: "q", options: ["a", 3, null, "b"] }).options).toEqual([
      "a",
      "b",
    ])
  })

  test("a missing question still yields something the panel can show", () => {
    expect(parseAskRequest({}).question).toBe("…")
  })

  test("data is always an object", () => {
    expect(parseAskRequest({ question: "q" }).data).toEqual({})
    expect(parseAskRequest({ question: "q", data: 3 }).data).toEqual({})
    expect(parseAskRequest({ question: "q", data: ["x"] }).data).toEqual({})
  })
})

describe("answerForSelection", () => {
  const options = ["a", "b", "c"]

  test("an in-range selection answers that option's text", () => {
    expect(answerForSelection(options, 0)).toBe("a")
    expect(answerForSelection(options, 2)).toBe("c")
  })

  test("an out-of-range selection answers nothing (the model decides)", () => {
    expect(answerForSelection(options, 3)).toBe(ASK_SKIP)
    expect(answerForSelection(options, -1)).toBe(ASK_SKIP)
  })

  test("no options at all: only a typed answer can come back", () => {
    expect(answerForSelection([], 0)).toBe(ASK_SKIP)
  })
})
