import { describe, expect, test } from "bun:test"

import { layerLine } from "../src/component/message"

/**
 * R22/A-8: the compaction line printed `L3 681→681 tok / L1 … / L2 …`, which
 * only means something to someone who has read the pipeline.  Each layer now
 * carries the one word for what it does.
 */
describe("layerLine names what each layer does", () => {
  const stat = (name: string, over: Record<string, number> = {}) => ({
    name,
    removed: 0,
    rewritten: 0,
    skipped: 0,
    beforeTokens: 681,
    afterTokens: 681,
    ...over,
  })

  test("a no-op compaction still reads as four named layers", () => {
    expect(layerLine(stat("L3", { skipped: 1 }) as never)).toContain("L3 落盘")
    expect(layerLine(stat("L1") as never)).toContain("L1 剪断")
    expect(layerLine(stat("L2") as never)).toContain("L2 占位")
    expect(layerLine(stat("L4", { afterTokens: 120 }) as never)).toContain("L4 摘要")
  })

  test("the counts and the token delta stay", () => {
    const line = layerLine(stat("L2", { rewritten: 6, skipped: 1, afterTokens: 300 }) as never)
    expect(line).toBe("L2 占位 改写 6 条 · 跳过 1 条 · 681→300 tok")
  })

  test("a removing layer says how many it removed", () => {
    expect(layerLine(stat("L1", { removed: 7, afterTokens: 200 }) as never)).toBe(
      "L1 剪断 −7 条 · 681→200 tok",
    )
  })
})
