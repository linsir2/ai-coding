/**
 * TDD (M6.5): the ask panel's pure half — wire params in, the answer out.
 *
 * The panel answers a server-initiated ``user_prompt.ask``
 * (handlers.py::_wire_user_prompt).  The answer is the plain text the tool
 * renders to the model, so an empty answer means "nobody answered" and the
 * model decides itself (security/user_prompt.py:80 — ``reason="empty"``).
 */

import { describe, expect, test } from "bun:test"
import stringWidth from "string-width"
import {
  parseAskRequest,
  answerForSelection,
  ASK_SKIP,
  planProse,
  planReviewAnswer,
  planReviewItems,
  planText,
} from "../src/util/ask"

describe("parseAskRequest", () => {
  test("reads the question and the options", () => {
    expect(parseAskRequest({ question: "哪个文件？", options: ["a.py", "b.py"] })).toEqual({
      question: "哪个文件？",
      options: ["a.py", "b.py"],
      // 19 §3.2: the plan review's payload keys, always explicit; R04 added
      // `data` to the same rule.
      detail: "",
      intent: "",
      data: {},
    })
  })

  test("the plan review's detail and intent survive parsing", () => {
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

// ---- R04: the plan review's structured rows ---------------------------------


describe("planReviewItems", () => {
  test("keeps the card rows and sheds shapeless entries", () => {
    const request = parseAskRequest({
      question: "批准这个计划并开始执行？",
      intent: "plan_review",
      data: {
        items: [
          { content: "建目录", verify: "test -d hotel", dependsOn: [1, 2] },
          { content: "写 db.py" },
          { content: 3 },
          "nope",
        ],
      },
    })
    expect(planReviewItems(request)).toEqual([
      { content: "建目录", verify: "test -d hotel", dependsOn: [1, 2] },
      { content: "写 db.py", verify: "", dependsOn: [] },
    ])
  })

  test("a plain question has no rows", () => {
    expect(planReviewItems(parseAskRequest({ question: "q" }))).toEqual([])
  })
})

// ---- R21 §1: the plan card's text transform --------------------------------


describe("planText", () => {
  test("a pipe table lines up, and the --- row is gone", () => {
    const plan = [
      "| 步骤 | 验收 |",
      "|---|---|",
      "| 建目录 | test -d hotel |",
      "| 写 db.py | pytest |",
    ].join("\n")
    const rows = planText(plan).split("\n")
    expect(rows.length).toBe(3) // the separator row is not a row
    expect(rows.join("\n")).not.toContain("---")
    // Every row occupies the same number of terminal columns -- measured,
    // not hand-counted, so the CJK cells are included in the check.
    expect(new Set(rows.map((row) => stringWidth(row))).size).toBe(1)
    expect(rows[1]).toContain("建目录")
    expect(rows[2]).toContain("写 db.py")
  })

  test("CJK cells are padded by display width, not by character count", () => {
    // "建" is two columns wide: one 建 == two ASCII chars, so the ASCII rows
    // pad by fewer spaces than a naive .length would.
    const rows = planText("| a | b |\n| 建 | x |").split("\n")
    expect(rows[0]).toBe("| a  | b |")
    expect(rows[1]).toBe("| 建 | x |")
  })

  test("a --- rule between sections is dropped", () => {
    expect(planText("why\n\n---\n\nrisk")).toBe("why\n\nrisk")
  })

  test("a table inside a fence is left alone", () => {
    const plan = "```\n| a | b |\n```"
    expect(planText(plan)).toBe(plan)
  })

  test("the old markers still go", () => {
    expect(planText("# 标题\n- 项\n`code` **bold**")).toBe("标题\n· 项\ncode bold")
  })

  test("an empty plan is an empty string", () => {
    expect(planText("")).toBe("")
  })
})

describe("planProse", () => {
  const items = [{ content: "建目录" }, { content: "写 db.py" }]

  test("a table that only lists the reviewed items is dropped", () => {
    const plan = [
      "做法是先脚手架后数据层。",
      "",
      "| 步骤 | 验收 |",
      "|---|---|",
      "| 建目录 | test -d hotel |",
      "| 写 db.py | pytest |",
      "",
      "风险：写库需要迁移脚本。",
    ].join("\n")
    expect(planProse(plan, items)).toBe(
      "做法是先脚手架后数据层。\n\n\n风险：写库需要迁移脚本。",
    )
  })

  test("a table of trade-offs stays (the items are not what it lists)", () => {
    const plan = "| 取舍 | 理由 |\n|---|---|\n| 用 sqlite | 无外部依赖 |"
    expect(planProse(plan, items)).toBe(plan)
  })

  test("no items to compare against: the plan is untouched", () => {
    const plan = "| 步骤 | 验收 |\n|---|---|\n| 建目录 | test |"
    expect(planProse(plan, [])).toBe(plan)
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

// ---- 19 §3.3: the plan review's answer codec --------------------------------


describe("the plan review's answer codec", () => {
  test("approve and keep encode as the JSON the tool parses", () => {
    expect(planReviewAnswer("approve")).toBe('{"choice":"approve"}')
    expect(planReviewAnswer("keep", "第 3 步拆细")).toBe(
      '{"choice":"keep","feedback":"第 3 步拆细"}',
    )
    expect(planReviewAnswer("keep")).toBe('{"choice":"keep"}')
  })

  // C（审核修复）：isPlanApproval 被删除 —— approve 的判定收敛到按钮本身
  // （PlanReviewPanel 随答案传出 intent），前端不再解析答案字符串反推意图。
  // 后端 present_plan._parse_answer 是唯一权威（approve+feedback=keep）。
  test("the card's intent travels with the answer, not re-derived from it", () => {
    // 编解码契约不变（后端仍按这个 JSON 解析）
    expect(planReviewAnswer("approve")).toBe('{"choice":"approve"}')
    expect(planReviewAnswer("keep", "第 3 步拆细")).toBe(
      '{"choice":"keep","feedback":"第 3 步拆细"}',
    )
    expect(planReviewAnswer("keep")).toBe('{"choice":"keep"}')
  })
})
