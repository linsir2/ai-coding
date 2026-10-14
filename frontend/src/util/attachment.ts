/**
 * Prompt attachments (r17/T5): the small pure half of image input.
 *
 * The bytes come off the clipboard as a Uint8Array; what travels to the
 * backend is one `data:` URL, the same shape it accepts from any other client.
 * Pure so the encoding is testable without a clipboard or a terminal.
 */

import { createHostClipboard } from "@opentui/core"

export interface PromptImage {
  name: string
  dataUrl: string
}

/** Encode clipboard bytes as a data URL, or null for something we cannot send. */
export function imageDataUrl(mimeType: string, bytes: Uint8Array): string | null {
  if (!mimeType.toLowerCase().startsWith("image/")) return null
  if (bytes.length === 0) return null
  return `data:${mimeType};base64,${Buffer.from(bytes).toString("base64")}`
}

/** The token an attached image is written as, in the composer's own text.
 *
 * OpenCode's shape: the picture sits where it was pasted, so backspace removes
 * it like any other character and there is no second place it can hide.  The
 * number only has to be unique within one message.
 */
export function imageToken(sequence: number): string {
  return `[Image ${sequence}]`
}

const TOKEN = /\[Image (\d+)\]/g

/**
 * Split the composer's text into the message and the images it still refers to.
 *
 * The text is the truth: a token the map knows is an image, and a token the map
 * does not know -- deleted, or typed by hand -- is ordinary text.  That is what
 * makes deletion work without a keybinding of its own, and it is why the map can
 * keep an entry for a picture the user has already removed.
 */
export function extractImages(
  raw: string,
  imagesByToken: Record<string, string>,
): { text: string; images: PromptImage[] } {
  const images: PromptImage[] = []
  const text = raw.replace(TOKEN, (match) => {
    const dataUrl = imagesByToken[match]
    if (dataUrl === undefined) return match
    images.push({ name: `image-${images.length + 1}`, dataUrl })
    return ""
  })
  return { text, images }
}

export type PasteOutcome =
  | { kind: "image"; dataUrl: string }
  | { kind: "text"; text: string }
  | { kind: "failed"; message: string }

/**
 * One host clipboard for the process, not one per composer.
 *
 * @opentui/core documents the limit: "WAYLAND_SOCKET-only launches can create
 * one host service per process because the inherited fd is one-shot."  Home and
 * Session each mount a Prompt, so building the service inside the component
 * made a second one the moment a conversation started -- and under a sandboxed
 * Wayland launch that second service has no socket left to read through.  The
 * clipboard belongs to the host, not to a text box.
 */
let hostService: ReturnType<typeof createHostClipboard> | undefined

export function hostClipboard(): ReturnType<typeof createHostClipboard> {
  hostService ??= createHostClipboard()
  return hostService
}

/** What each refusal from the host clipboard means to the person pressing ctrl+v.
 *
 * The backend answers with a status rather than data for six distinct reasons,
 * and every one of them used to return silently -- so a user whose terminal
 * could not reach the clipboard was told nothing, and had no way to tell that
 * from the key not being bound.  Nothing here touches a clipboard, so it is
 * testable without one.
 */
function refusalFor(result: { status: string; error?: Error }): string {
  switch (result.status) {
    case "empty":
      return "剪贴板是空的"
    case "unsupported":
      return "当前环境不支持读取剪贴板（终端/SSH 会话通常读不到本机剪贴板）"
    case "timed-out":
      return "读取剪贴板超时"
    case "cancelled":
      return "读取剪贴板被取消"
    case "limit-exceeded":
      return "剪贴板内容超出读取上限"
    case "failed":
      return `读取剪贴板失败：${result.error?.message ?? "未知错误"}`
    default:
      return `剪贴板读取失败（${result.status}）`
  }
}

/**
 * Turn whatever the clipboard gave back into an outcome the composer can act on.
 *
 * `thrown` is the other half: the host backend is a native call that can fail
 * before it ever returns a status, and that was swallowed outright.
 */
export function interpretPaste(
  result: { status: string; representation?: { mimeType: string; bytes: Uint8Array }; error?: Error } | null,
  thrown?: Error,
): PasteOutcome {
  if (thrown) return { kind: "failed", message: `读取剪贴板失败：${thrown.message}` }
  if (!result || result.status !== "read") {
    return { kind: "failed", message: refusalFor(result ?? { status: "failed" }) }
  }
  const representation = result.representation
  if (!representation) return { kind: "failed", message: "剪贴板没有返回任何内容" }
  const { mimeType, bytes } = representation
  if (mimeType.toLowerCase().startsWith("image/")) {
    const dataUrl = imageDataUrl(mimeType, bytes)
    if (dataUrl) return { kind: "image", dataUrl }
    return { kind: "failed", message: `剪贴板里的 ${mimeType} 图片没有内容` }
  }
  if (mimeType.toLowerCase().startsWith("text/")) {
    return { kind: "text", text: new TextDecoder().decode(bytes) }
  }
  return { kind: "failed", message: `剪贴板内容类型 ${mimeType} 无法粘贴` }
}
