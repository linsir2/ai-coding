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
  /** 20 §批次1: the posture the Home row showed when this was submitted. */
  mode?: "normal" | "plan"
}

/** What the composer keeps: images the wire did not take, and why. */
export interface Withheld {
  kept: PromptImage[]
  withheldReason: string
}

/** The name in "/name", or "" when the line is not a command at all. */
export function commandName(commands: readonly string[], text: string): string {
  const match = /^\/([A-Za-z][\w-]*)/.exec(text.trim())
  return match && commands.includes(match[1].toLowerCase()) ? match[1].toLowerCase() : ""
}

function isCommandLine(commands: readonly string[], text: string): boolean {
  return commandName(commands, text) !== ""
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
 * A line is a command exactly when the backend says it is one -- so the client
 * asks (`slash.list`, derived from `app/slash.py::_ALIASES`) instead of
 * guessing from the leading slash.
 *
 * Guessing was r18's version and it was wrong in the expensive direction:
 * `/path/to/file.py` starts with a slash, but the dispatcher does not
 * recognise it and hands the line to the model as ordinary text.  The client
 * declared it a command, kept the picture, and told the user the command would
 * not send it -- while the model received the line with no picture at all.
 *
 * Images ride the prompt path only: a command has no use for them, and passing
 * them along would put a screenshot into /compact's input.
 */
export function buildSubmission(
  commands: readonly string[],
  sessionId: string,
  text: string,
  images: PromptImage[],
): WireSubmission {
  const isCommand = isCommandLine(commands, text)
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

/**
 * The wire entry that must land before a Home submission (20 §批次1).
 *
 * The Session route mounts on the first submission, so its shift+tab binding
 * cannot pre-arm the posture at cold start; Home carries the toggle instead,
 * and the posture has to be in place before the prompt runs.  ``/plan`` is
 * built here rather than through ``buildSubmission``: it is an internal
 * command (19 §3.6), not a user line subject to the ``slash.list``
 * vocabulary, and a bare ``/plan`` is synchronous on the backend
 * (``commands.py::_handle_plan``, no model call).  A submission that is
 * itself a command is that command, not plan material, so it goes alone.
 */
export function planPrelude(
  prompt: PendingPrompt,
  commands: readonly string[],
  sessionId: string,
): WireSubmission | null {
  if (prompt.mode !== "plan") return null
  if (commandName(commands, prompt.text)) return null
  return {
    method: "session.command",
    payload: { id: sessionId, text: "/plan" },
    kept: [],
    withheldReason: "",
  }
}