/**
 * TDD (M1): pure location helpers — home abbreviation, dir:branch label,
 * CJK-aware path truncation.  Semantics mirror OpenCode's ui/file-path.tsx
 * (posix-only adaptation) and are exercised here as the contract.
 */

import { describe, expect, test } from "bun:test"
import { abbreviateHome, formatLocation, truncatePath } from "../src/util/location"

describe("abbreviateHome", () => {
  test("home itself becomes ~", () => {
    expect(abbreviateHome("/home/u", "/home/u")).toBe("~")
  })

  test("a path inside home becomes ~/...", () => {
    expect(abbreviateHome("/home/u/proj", "/home/u")).toBe("~/proj")
  })

  test("a sibling with the same prefix is not abbreviated", () => {
    expect(abbreviateHome("/home/u2", "/home/u")).toBe("/home/u2")
  })

  test("an outside path stays untouched", () => {
    expect(abbreviateHome("/other", "/home/u")).toBe("/other")
  })

  test("no home known means no abbreviation", () => {
    expect(abbreviateHome("/home/u/proj", "")).toBe("/home/u/proj")
  })
})

describe("formatLocation", () => {
  test("appends the branch after a colon", () => {
    expect(formatLocation("~/proj", "main")).toBe("~/proj:main")
  })

  test("without a branch only the directory is shown", () => {
    expect(formatLocation("~/proj", "")).toBe("~/proj")
  })
})

describe("truncatePath", () => {
  test("a fitting path is returned unchanged", () => {
    expect(truncatePath("/short/path", 30)).toBe("/short/path")
  })

  test("empty input stays empty", () => {
    expect(truncatePath("", 10)).toBe("")
  })

  test("zero width means nothing fits", () => {
    expect(truncatePath("/some/path", 0)).toBe("")
  })

  test("keeps the basename and drops middle segments", () => {
    // root "/" + ellipsis prefix + basename only
    expect(truncatePath("/home/user/projects/myapp", 10)).toBe("/…/myapp")
  })

  test("truncates a long basename keeping its extension", () => {
    expect(truncatePath("/a/really-long-filename.ts", 12)).toBe("/…/reall….ts")
  })

  test("relative paths keep trailing segments", () => {
    expect(truncatePath("foo/very/long/path/name", 12)).toBe("…/path/name")
  })

  test("CJK truncation respects display width (not char count)", () => {
    // 文件.txt is 8 columns wide; at width 10 the basename is cut to 文….txt
    expect(truncatePath("/项目/这是一个很长的目录名/文件.txt", 10)).toBe("/…/文….txt")
  })

  test("a wide CJK path that fits is untouched", () => {
    expect(truncatePath("/项目/文件.txt", 30)).toBe("/项目/文件.txt")
  })
})
