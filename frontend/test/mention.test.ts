/**
 * r16/E-2: the `@skill` half of the composer's suggestions.
 *
 * The trigger boundary must agree with the registry's `_MENTION`
 * (skills/registry.py:428): a mention the panel suggests but the backend
 * ignores would be a dead reference.
 */

import { describe, expect, test } from "bun:test"
import { filterSkillMentions, mentionQuery, replaceMentionToken } from "../src/util/mention"

describe("mentionQuery", () => {
  test("opens on a word boundary and takes the text after @", () => {
    expect(mentionQuery("fix @ti", 7)).toBe("ti")
    expect(mentionQuery("@", 1)).toBe("")
  })

  test("an email is not a mention", () => {
    expect(mentionQuery("mail me a@b.com", 15)).toBeNull()
  })

  test("a space closes the token", () => {
    expect(mentionQuery("@ti then", 8)).toBeNull()
  })

  test("only text before the cursor counts", () => {
    expect(mentionQuery("@tidy 后缀", 5)).toBe("tidy")
  })
})

describe("replaceMentionToken", () => {
  test("the token is replaced in place and the cursor lands after the space", () => {
    expect(replaceMentionToken("@ti", 3, "tidy")).toEqual({ text: "@tidy ", cursor: 6 })
    expect(replaceMentionToken("用 @ti", 5, "tidy")).toEqual({ text: "用 @tidy ", cursor: 8 })
  })

  test("text after the cursor is preserved", () => {
    expect(replaceMentionToken("@ti 之后", 3, "tidy").text).toBe("@tidy  之后")
  })
})

describe("filterSkillMentions", () => {
  const skills = [
    { name: "demo-skill", description: "A demo" },
    { name: "review-helper", description: "Reviews diffs" },
  ]

  test("an empty query keeps the list, capped", () => {
    expect(filterSkillMentions("", skills).map((s) => s.name)).toEqual([
      "demo-skill",
      "review-helper",
    ])
  })

  test("matches on name and description", () => {
    expect(filterSkillMentions("review", skills).map((s) => s.name)).toEqual(["review-helper"])
    expect(filterSkillMentions("demo", skills).map((s) => s.name)).toEqual(["demo-skill"])
  })

  test("a skill without a description still matches on its name", () => {
    const bare = [{ name: "demo-skill" }]
    expect(filterSkillMentions("de", bare).map((s) => s.name)).toEqual(["demo-skill"])
  })
})
