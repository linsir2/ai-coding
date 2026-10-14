/**
 * One decision about a submission: what it sends, and therefore what it shows
 * (r18/F14).
 *
 * The transcript and the wire used to decide separately — `submit` dispatched
 * `{kind:"submitted", images}` unconditionally while `dispatch` dropped images
 * from a slash line — so pasting a picture and typing `/help` drew the picture
 * into the transcript and sent nothing.  Two decisions about one submission can
 * disagree; this is the one they both read.
 */

import type { PromptImage } from "./attachment"

/**
 * A line the user typed on a route that has no session yet.  Home is a second
 * submit entry (r18/F13), so the hand-off across the route switch has to carry
 * the images the Session entry always did.
 */
export interface PendingPrompt {
  text: string
  images: PromptImage[]
}

/** What the composer keeps: images the wire did not take, and why. */
export interface Withheld {
  kept: PromptImage[]
  withheldReason: string
}

export interface WireSubmission extends Withheld {
  method: "session.prompt" | "session.command"
  payload: {
    id: string
    text: string
    images?: { name: string; data_url: string }[]
  }
}

/**
 * A line is a command exactly when the backend would call it one: it strips
 * both ends before looking for the slash (``app/commands.py:108``).  Mirroring
 * that rule is the whole point -- if this guessed "prompt" for a line the
 * backend dispatches as a command, the two would disagree about what the
 * submission was.  Images ride the prompt path only: a command has no use for
 * them, and passing them along would put a screenshot into /compact's input.
 */
export function buildSubmission(
  sessionId: string,
  text: string,
  images: PromptImage[],
): WireSubmission {
  const isCommand = text.trim().startsWith("/")
  if (!images.length || isCommand) {
    return {
      method: isCommand ? "session.command" : "session.prompt",
      payload: { id: sessionId, text },
      kept: isCommand ? [...images] : [],
      withheldReason: isCommand ? "命令不发送图片，图片已留在输入框" : "",
    }
  }
  return {
    method: "session.prompt",
    payload: {
      id: sessionId,
      text,
      images: images.map((image) => ({ name: image.name, data_url: image.dataUrl })),
    },
    kept: [],
    withheldReason: "",
  }
}