/**
 * r17/T5: the pure half of image input — clipboard bytes to a data URL.
 *
 * The clipboard and the terminal are not in this harness; what is pinned here
 * is the one encoding decision the backend depends on.
 */

import { describe, expect, test } from "bun:test"
import {
  dropToken,
  extractImages,
  imageDataUrl,
  interpretPaste,
  imageToken,
} from "../src/util/attachment"

/** The clipboard result shapes the host backend can hand back. */
function read(status: string, extra: Record<string, unknown> = {}) {
  return { status, ...extra } as never
}

describe("inline image placeholders", () => {
  // OpenCode's shape: the picture is a token in the text, so it sits where it
  // was pasted and backspace removes it like any other character.  Nothing has
  // to be learned and nothing can be forgotten.
  test("a token in the text becomes an image and leaves the message clean", () => {
    const token = imageToken(1)
    const { text, images } = extractImages(`看看这个 ${token} 对吗`, { [token]: "data:image/png;base64,AAA" })

    expect(images).toEqual([{ name: "image-1", dataUrl: "data:image/png;base64,AAA" }])
    expect(text).toBe("看看这个  对吗")
  })

  test("several tokens keep the order they were written in", () => {
    const first = imageToken(1)
    const second = imageToken(2)
    const map = { [first]: "data:image/png;base64,ONE", [second]: "data:image/png;base64,TWO" }

    const { images } = extractImages(`${first} 和 ${second}`, map)

    expect(images.map((image) => image.dataUrl)).toEqual([
      "data:image/png;base64,ONE",
      "data:image/png;base64,TWO",
    ])
  })

  // Deleting the token is how a picture is removed, so a token the map does not
  // know -- deleted, or typed by hand -- must not resurrect anything.
  test("a token with no image behind it is left as ordinary text", () => {
    const { text, images } = extractImages("这是 [Image 9] 手打的", {})

    expect(images).toEqual([])
    expect(text).toBe("这是 [Image 9] 手打的")
  })

  test("a message with no token has no images and its text is untouched", () => {
    const { text, images } = extractImages("普通的一句话", {})

    expect(images).toEqual([])
    expect(text).toBe("普通的一句话")
  })

  test("a deleted token drops its image even though the map still holds it", () => {
    const token = imageToken(1)
    // The map is the record of what was pasted; the text is the truth of what
    // the user still wants.  Removing the token removes the picture.
    const { text, images } = extractImages("删掉了", { [token]: "data:image/png;base64,AAA" })

    expect(images).toEqual([])
    expect(text).toBe("删掉了")
  })
})

describe("dropping a token with one keypress", () => {
  // The whole reason this exists: the placeholder is real text, so a plain
  // backspace ate it a character at a time and left "[Imag" behind.  These
  // cases are all CJK on purpose -- opentui's own extmarks mechanism handles
  // ASCII and corrupts Chinese, which is why this is done by hand.
  const token = imageToken(1)
  const tokens = new Set([token])

  test("backspace at the end of a token takes the whole thing", () => {
    const text = `看看 ${token} 对吗`
    const offset = text.indexOf(token) + token.length

    expect(dropToken(text, offset, tokens, "before")).toEqual({
      text: "看看  对吗",
      offset: 3,
      dropped: true,
    })
  })

  test("delete at the start of a token takes the whole thing", () => {
    const text = `看看 ${token} 对吗`
    const offset = text.indexOf(token)

    expect(dropToken(text, offset, tokens, "after")).toEqual({
      text: "看看  对吗",
      offset: 3,
      dropped: true,
    })
  })

  // A cursor inside the token means the user is editing it, not removing it.
  test("backspace in the middle of a token leaves it alone", () => {
    const text = `看看 ${token} 对吗`
    const offset = text.indexOf(token) + 3

    expect(dropToken(text, offset, tokens, "before").dropped).toBe(false)
  })

  test("delete in the middle of a token leaves it alone", () => {
    const text = `看看 ${token} 对吗`
    const offset = text.indexOf(token) + 3

    expect(dropToken(text, offset, tokens, "after").dropped).toBe(false)
  })

  test("ordinary text is untouched", () => {
    expect(dropToken("看看这个", 2, tokens, "before")).toEqual({
      text: "看看这个",
      offset: 2,
      dropped: false,
    })
  })

  // Only the token the cursor is against comes out.
  test("two tokens side by side: only the one at the cursor", () => {
    const second = imageToken(2)
    const text = `${token}${second}`
    const offset = text.length

    expect(dropToken(text, offset, new Set([token, second]), "before")).toEqual({
      text: token,
      offset: token.length,
      dropped: true,
    })
  })

  test("a token at the very start has nothing before it", () => {
    expect(dropToken(`${token} 后面`, 0, tokens, "before").dropped).toBe(false)
  })

  test("a token at the very end has nothing after it", () => {
    const text = `前面 ${token}`
    expect(dropToken(text, text.length, tokens, "after").dropped).toBe(false)
  })

  // The map is the record of what was pasted; a token it does not know is
  // ordinary text (typed by hand, or a token whose image was already dropped).
  test("a token the map does not know is not special", () => {
    const text = `看看 ${token} 对吗`
    const offset = text.indexOf(token) + token.length

    expect(dropToken(text, offset, new Set(), "before").dropped).toBe(false)
  })
})

describe("interpretPaste", () => {
  test("an image comes back as a data URL", () => {
    const outcome = interpretPaste(
      read("read", { representation: { mimeType: "image/png", bytes: new Uint8Array([1, 2]) } }),
    )

    expect(outcome).toEqual({ kind: "image", dataUrl: "data:image/png;base64,AQI=" })
  })

  test("text is decoded for the composer", () => {
    const outcome = interpretPaste(
      read("read", { representation: { mimeType: "text/plain", bytes: new Uint8Array([104, 105]) } }),
    )

    expect(outcome).toEqual({ kind: "text", text: "hi" })
  })

  // Every one of these used to return silently, which is why a user pressing
  // ctrl+v on a machine with no reachable clipboard was told nothing at all.
  test.each([
    ["empty", "空的"],
    ["unsupported", "不支持"],
    ["timed-out", "超时"],
    ["cancelled", "取消"],
    ["limit-exceeded", "上限"],
  ])("a %s clipboard says so", (status, fragment) => {
    const outcome = interpretPaste(read(status))

    expect(outcome.kind).toBe("failed")
    expect(outcome.kind === "failed" && outcome.message).toContain(fragment)
  })

  test("a read that failed names the error", () => {
    const outcome = interpretPaste(read("failed", { error: new Error("no display") }))

    expect(outcome.kind === "failed" && outcome.message).toContain("no display")
  })

  test("a throw becomes a message rather than silence", () => {
    const outcome = interpretPaste(null, new Error("backend missing"))

    expect(outcome.kind === "failed" && outcome.message).toContain("backend missing")
  })

  test("an empty image payload is not silently dropped", () => {
    const outcome = interpretPaste(
      read("read", { representation: { mimeType: "image/png", bytes: new Uint8Array([]) } }),
    )

    expect(outcome.kind).toBe("failed")
  })

  // The frontend deliberately does NOT carry an image whitelist: the backend's
  // allowed_mime() is the single source of truth (r19/F5 removed the copy that
  // had drifted).  An unfamiliar image is forwarded and the backend's refusal
  // comes back on the message (r20/F5).
  test("an unfamiliar image mime is forwarded, not judged here", () => {
    const outcome = interpretPaste(
      read("read", { representation: { mimeType: "image/bmp", bytes: new Uint8Array([1]) } }),
    )

    expect(outcome).toEqual({ kind: "image", dataUrl: "data:image/bmp;base64,AQ==" })
  })

  test("a non-text, non-image mime names itself", () => {
    const outcome = interpretPaste(
      read("read", { representation: { mimeType: "application/pdf", bytes: new Uint8Array([1]) } }),
    )

    expect(outcome.kind === "failed" && outcome.message).toContain("application/pdf")
  })
})

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
