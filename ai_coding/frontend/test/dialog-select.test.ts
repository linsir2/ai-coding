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
import {
  emptyLabel,
  filterOptions,
  flattenGroups,
  groupOptions,
  moveSelection,
  rowLayout,
  scrollTopFor,
  singleLineText,
} from "../src/util/dialog-select"

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

describe("rowLayout", () => {
  test("a first group header takes one line, later ones add the gap line", () => {
    expect(rowLayout([true, false, true, false])).toEqual({ tops: [1, 2, 5, 6], height: 7 })
  })

  test("an empty list has no rows", () => {
    expect(rowLayout([])).toEqual({ tops: [], height: 0 })
  })

  test("options without categories start at line 0", () => {
    expect(rowLayout([false, false, false])).toEqual({ tops: [0, 1, 2], height: 3 })
  })

  test("a first option without a category does not reserve a header line", () => {
    expect(rowLayout([false, true])).toEqual({ tops: [0, 3], height: 4 })
  })
})

describe("scrollTopFor", () => {
  test("a row fully below the viewport bottom-aligns (PageDown)", () => {
    // 10 rows down, a 6-line viewport starting at 0: the row is out of sight.
    expect(scrollTopFor(0, 10, 6)).toBe(5)
  })

  test("a row fully above the viewport top-aligns (PageUp)", () => {
    expect(scrollTopFor(8, 2, 6)).toBe(2)
  })

  test("a row already inside the viewport leaves the scroll alone", () => {
    expect(scrollTopFor(4, 6, 6)).toBe(4)
  })

  test("the result never goes negative", () => {
    expect(scrollTopFor(0, 0, 6)).toBe(0)
  })

  test("scrolling up reveals the group header above the row", () => {
    // The row's block starts one line earlier (its header); a Home from below
    // must land on the header, not slice it off.
    expect(scrollTopFor(8, 5, 6, 4)).toBe(4)
  })

  test("a one-line viewport still follows the selection", () => {
    expect(scrollTopFor(3, 7, 1)).toBe(7)
  })

  test("a current offset beyond the content settles on the row", () => {
    expect(scrollTopFor(99, 2, 6)).toBe(2)
  })
})

describe("singleLineText", () => {
  test("folds line breaks so a one-line row keeps every word", () => {
    expect(singleLineText("first line\nsecond line")).toBe("first line second line")
    expect(singleLineText("first\n\nsecond")).toBe("first second")
    expect(singleLineText("no breaks")).toBe("no breaks")
  })
})

describe("emptyLabel", () => {
  test("no options at all says there is nothing to pick", () => {
    expect(emptyLabel(false)).toBe("没有可选项")
  })

  test("options that a query did not match say so instead", () => {
    expect(emptyLabel(true)).toBe("没有匹配项")
  })

  test("a panel's own wording wins over the defaults", () => {
    expect(emptyLabel(false, "自定义空态", "自定义无命中")).toBe("自定义空态")
    expect(emptyLabel(true, "自定义空态", "自定义无命中")).toBe("自定义无命中")
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
