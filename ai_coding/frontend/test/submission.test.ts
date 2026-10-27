/**
 * r18/F14: one submission, one decision.
 *
 * `submit()` dispatched `{kind:"submitted", images}` unconditionally while
 * `dispatch()` separately decided that a slash line carries none -- so pasting
 * an image and typing `/help` drew the image into the transcript and sent
 * nothing.  Both readers now take the same `buildSubmission`, so what the
 * transcript shows and what the wire carries cannot disagree.
 *
 * r18/F13 is checked structurally below: no test in this repo renders the
 * Session route, and a clipboard cannot be driven from the terminal harness, so
 * the signature is what has to stay honest.
 */

import { describe, expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import type { PromptImage } from "../src/util/attachment"
import { buildSubmission, commandName, planPrelude } from "../src/util/submission"

/** The dispatcher's vocabulary, as slash.list would deliver it. */
const COMMANDS = ["help", "undo", "redo", "compact", "stats"]

const IMG: PromptImage = { name: "shot.png", dataUrl: "data:image/png;base64,AA==" }

describe("buildSubmission", () => {
  test("a plain line carries its images", () => {
    const s = buildSubmission(COMMANDS, "s1", "看这个", [IMG])
    expect(s.method).toBe("session.prompt")
    expect(s.payload.images).toEqual([{ name: "shot.png", data_url: IMG.dataUrl }])
    expect(s.kept).toEqual([])
  })

  test("a slash line sends none of them", () => {
    const s = buildSubmission(COMMANDS, "s1", "/help", [IMG])
    expect(s.method).toBe("session.command")
    expect(s.payload.images).toBeUndefined()
  })

  test("a slash line hands them back and says why", () => {
    const s = buildSubmission(COMMANDS, "s1", "/undo", [IMG])
    expect(s.kept).toEqual([IMG]), "the image must stay in the composer"
    expect(s.withheldReason).toContain("图片")
  })

  test("a plain line with no images withholds nothing", () => {
    const s = buildSubmission(COMMANDS, "s1", "hello", [])
    expect(s.kept).toEqual([])
    expect(s.withheldReason).toBe("")
  })

  test("what the transcript shows is exactly what the wire carries", () => {
    const cases: [string, PromptImage[]][] = [
      ["纯文本", []],
      ["带图", [IMG]],
      ["/help 带图", [IMG]],
      ["  /compact 带图  ", [IMG]],
    ]
    for (const [text, images] of cases) {
      const s = buildSubmission(COMMANDS, "s1", text, images)
      const sent = s.payload.images?.length ?? 0
      expect(sent, text).toBe(images.length - s.kept.length)
    }
  })

  test("the command rule mirrors the backend's, which strips both ends", () => {
    // app/commands.py:108 -- `text.strip().startswith("/")`.
    expect(buildSubmission(COMMANDS, "s1", " /help", [IMG]).method).toBe("session.command")
    expect(buildSubmission(COMMANDS, "s1", "hello /help", [IMG]).method).toBe("session.prompt")
  })
})

describe("the Home route's signature (r18/F13)", () => {
  const src = readFileSync(
    fileURLToPath(new URL("../src/routes/home.tsx", import.meta.url)),
    "utf8",
  )

  // T7 sanctioned behavior change (30-M4 §3.5/§7.5): the Home draft is an
  // agent id (null = default), not a mode boolean.  The mode only survives
  // inside PendingPrompt as the old-backend fallback for planPrelude.
  test("onSubmit carries the agent draft, null for default (T7)", () => {
    expect(src).toMatch(
      /onSubmit:\s*\(text:\s*string,\s*images:\s*PromptImage\[\],\s*agent:\s*string\s*\|\s*null\)/,
    )
  })

  test("PromptImage is the type it imports", () => {
    expect(src).toMatch(/import[^\n]*PromptImage[^\n]*from "\.\.\/util\/attachment"/)
  })
})

describe("planPrelude (20 §批次1)", () => {
  test("normal mode sends nothing ahead of the prompt", () => {
    expect(planPrelude({ text: "看这个", images: [] }, COMMANDS, "s1")).toBeNull()
    expect(planPrelude({ text: "看这个", images: [], mode: "normal" }, COMMANDS, "s1")).toBeNull()
  })

  test("plan mode enters the posture first, over the same command channel", () => {
    const prelude = planPrelude(
      { text: "探索一下你的计划层", images: [], mode: "plan" },
      COMMANDS,
      "s1",
    )
    expect(prelude?.method).toBe("session.command")
    expect(prelude?.payload).toEqual({ id: "s1", text: "/plan" })
  })

  test("a submission that is itself a command skips the prelude", () => {
    // `/help` is in COMMANDS above; with PLAN armed the command is the work.
    expect(planPrelude({ text: "/help", images: [], mode: "plan" }, COMMANDS, "s1")).toBeNull()
  })
})

describe("planPrelude follows the agent (T7)", () => {
  test("an explicit plan agent arms the prelude without the mode flag", () => {
    const prelude = planPrelude({ text: "探索一下", images: [], agent: "plan" }, COMMANDS, "s1")
    expect(prelude?.method).toBe("session.command")
    expect(prelude?.payload).toEqual({ id: "s1", text: "/plan" })
  })

  test("a non-plan agent sends no prelude", () => {
    expect(planPrelude({ text: "探索一下", images: [], agent: "reviewer" }, COMMANDS, "s1")).toBeNull()
    expect(planPrelude({ text: "探索一下", images: [], agent: null }, COMMANDS, "s1")).toBeNull()
  })

  test("a null draft on an already-plan session still arms the entry", () => {
    // The Session route fills agent from the hydrated live agent; the
    // prelude must fire for it the way the old mode flag did.
    const prelude = planPrelude({ text: "探索一下", images: [], agent: "plan" }, COMMANDS, "s1")
    expect(prelude?.payload).toEqual({ id: "s1", text: "/plan" })
  })

  test("an explicit agent wins over a stale mode flag", () => {
    expect(
      planPrelude({ text: "x", images: [], agent: "reviewer", mode: "plan" }, COMMANDS, "s1"),
    ).toBeNull()
  })
})