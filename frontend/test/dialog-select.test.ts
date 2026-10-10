/**
 * TDD (M3): DialogSelect pure logic — filtering, grouping, navigation.
 *
 * The component renders; these functions decide.  They are the port of
 * OpenCode's ui/dialog-select.tsx behaviour (fuzzysort keys/weights, category
 * grouping, wrap-around navigation) and are pinned here so the render layer
 * only has to lay things out.
 */

import { describe, expect, test } from "bun:test"
import type { SelectOption } from "../src/util/dialog-select"
import { filterOptions, flattenGroups, groupOptions, moveSelection } from "../src/util/dialog-select"

const opt = (title: string, extra: Partial<SelectOption> = {}): SelectOption => ({
  title,
  value: title,
  ...extra,
})

describe("filterOptions", () => {
  test("an empty query keeps every option untouched", () => {
    const options = [opt("model"), opt("session")]
    expect(filterOptions(options, "  ")).toBe(options)
  })

  test("a query keeps only matches", () => {
    const options = [opt("Switch model"), opt("Switch session"), opt("Exit")]
    expect(filterOptions(options, "model").map((o) => o.title)).toEqual(["Switch model"])
  })

  test("a title match outranks a description match (title weighted 2x)", () => {
    const options = [opt("x", { description: "model" }), opt("model")]
    expect(filterOptions(options, "model").map((o) => o.title)).toEqual(["model", "x"])
  })

  test("no match yields an empty list", () => {
    expect(filterOptions([opt("model"), opt("session")], "zzzzz")).toEqual([])
  })
})

describe("groupOptions", () => {
  test("options with categories group in first-seen order", () => {
    const groups = groupOptions([
      opt("a", { category: "Model" }),
      opt("b", { category: "Session" }),
      opt("c", { category: "Model" }),
    ])
    expect(groups.map((g) => g.category)).toEqual(["Model", "Session"])
    expect(groups[0]!.options.map((o) => o.title)).toEqual(["a", "c"])
    expect(groups[1]!.options.map((o) => o.title)).toEqual(["b"])
  })

  test("options without categories form one unlabelled group", () => {
    const groups = groupOptions([opt("a"), opt("b")])
    expect(groups).toEqual([{ category: "", options: [opt("a"), opt("b")] }])
  })
})

describe("flattenGroups", () => {
  test("flattens in group order then option order", () => {
    const groups = groupOptions([
      opt("a", { category: "One" }),
      opt("b", { category: "Two" }),
      opt("c", { category: "One" }),
    ])
    expect(flattenGroups(groups).map((o) => o.title)).toEqual(["a", "c", "b"])
  })
})

describe("moveSelection", () => {
  test("moves by delta", () => {
    expect(moveSelection(0, 1, 5)).toBe(1)
    expect(moveSelection(3, -1, 5)).toBe(2)
  })

  test("wraps around both ends", () => {
    expect(moveSelection(0, -1, 5)).toBe(4)
    expect(moveSelection(4, 1, 5)).toBe(0)
  })

  test("an empty list is safe", () => {
    expect(moveSelection(0, 1, 0)).toBe(0)
  })
})
