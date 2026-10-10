/**
 * Ask panel, the pure half (M6.5).
 *
 * The panel answers a server-initiated ``user_prompt.ask``
 * (handlers.py::_wire_user_prompt); the answer is the plain text the tool
 * renders to the model (``用户回答：…``).  An empty answer — esc, a dead
 * round-trip — is ``reason="empty"`` inside the shared callback
 * (security/user_prompt.py:80), so the model decides for itself: failing
 * open to the model, never hanging the turn.
 */

export interface AskRequest {
  question: string
  options: string[]
}

/** The answer that says "nobody answered" (esc, a closed panel). */
export const ASK_SKIP = ""

/** Build the panel's request from the wire params, dropping anything unshaped. */
export function parseAskRequest(params: Record<string, unknown>): AskRequest {
  const rawOptions = params["options"]
  const options = Array.isArray(rawOptions)
    ? rawOptions.filter((option): option is string => typeof option === "string")
    : []
  const question = String(params["question"] ?? "").trim()
  return { question: question || "…", options }
}

/** The text a selection answers with; out of range (or no options) answers nothing. */
export function answerForSelection(options: string[], index: number): string {
  return options[index] ?? ASK_SKIP
}
