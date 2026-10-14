/**
 * r17/T5: the pure half of image input — clipboard bytes to a data URL.
 *
 * The clipboard and the terminal are not in this harness; what is pinned here
 * is the one encoding decision the backend depends on.
 */

import { describe, expect, test } from "bun:test"
import { imageDataUrl } from "../src/util/attachment"

describe("imageDataUrl", () => {
  test("wraps png bytes as a data URL", () => {
    const url = imageDataUrl("image/png", new Uint8Array([1, 2, 3]))
    expect(url).toBe("data:image/png;base64,AQID")
  })

  test("refuses non-images and empty payloads", () => {
    expect(imageDataUrl("text/plain", new Uint8Array([1]))).toBeNull()
    expect(imageDataUrl("image/png", new Uint8Array())).toBeNull()
  })
})
