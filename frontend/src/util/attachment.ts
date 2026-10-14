/**
 * Prompt attachments (r17/T5): the small pure half of image input.
 *
 * The bytes come off the clipboard as a Uint8Array; what travels to the
 * backend is one `data:` URL, the same shape it accepts from any other client.
 * Pure so the encoding is testable without a clipboard or a terminal.
 */

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
