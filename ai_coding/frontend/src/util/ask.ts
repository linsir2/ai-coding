/**
 * Ask panel, the pure half (M6.5).
 *
 * The panel answers a server-initiated ``user_prompt.ask``
 * (handlers.py::_wire_user_prompt); the answer is the plain text the tool
 * renders to the model (``用户回答：…``).  An empty answer — esc, a dead
 * round-trip — is ``reason="empty"`` inside the shared callback
 * (security/user_prompt.py:80), so the model decides for itself: failing
 * open to the model, never hanging the turn.
 *
 * ``detail`` / ``intent`` / ``data`` are the generic wire fields the request
 * always carried; the panel renders ``detail`` as its body when a request
 * has one and leaves the rest to the caller.
 */

export interface AskRequest {
  question: string
  options: string[]
  /** The markdown body a request may carry; empty for a plain question. */
  detail: string
  /** Which kind of request this is ("" = the plain question). */
  intent: string
  /**
   * The structured payload beside detail/intent.  Always an object — the wire
   * contract keeps it explicit, {} included.
   */
  data: Record<string, unknown>
}

/** The answer that says "nobody answered" (esc, a closed panel). */
export const ASK_SKIP = ""

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
}

/** Build the panel's request from the wire params, dropping anything unshaped. */
export function parseAskRequest(params: Record<string, unknown>): AskRequest {
  const rawOptions = params["options"]
  const options = Array.isArray(rawOptions)
    ? rawOptions.filter((option): option is string => typeof option === "string")
    : []
  const question = String(params["question"] ?? "").trim()
  const detail = typeof params["detail"] === "string" ? params["detail"] : ""
  const intent = typeof params["intent"] === "string" ? params["intent"] : ""
  const data = isRecord(params["data"]) ? params["data"] : {}
  return { question: question || "…", options, detail, intent, data }
}

/** The text a selection answers with; out of range (or no options) answers nothing. */
export function answerForSelection(options: string[], index: number): string {
  return options[index] ?? ASK_SKIP
}
