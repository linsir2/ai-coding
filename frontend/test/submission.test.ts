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
import { buildSubmission, commandName } from "../src/util/submission"

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

  test("onSubmit accepts the images Prompt passes it", () => {
    expect(src).toMatch(/onSubmit:\s*\(text:\s*string,\s*images:\s*PromptImage\[\]\)/)
  })

  test("PromptImage is the type it imports", () => {
    expect(src).toMatch(/import[^\n]*PromptImage[^\n]*from "\.\.\/util\/attachment"/)
  })
})