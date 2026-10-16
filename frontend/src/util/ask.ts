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
 * 19 §3.2: the same request also carries the plan review — ``detail`` is the
 * plan markdown and ``intent`` picks the card; ``planReviewAnswer`` /
 * ``isPlanApproval`` are that card's answer codec (the only thing allowed to
 * arm the post-approval auto-run).
 */

export interface AskRequest {
  question: string
  options: string[]
  /** 19 §3.2: the plan-review markdown body; empty for a plain question. */
  detail: string
  /** 19 §3.2: which card renders this request ("" = the generic ask panel). */
  intent: string
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
  const detail = typeof params["detail"] === "string" ? params["detail"] : ""
  const intent = typeof params["intent"] === "string" ? params["intent"] : ""
  return { question: question || "…", options, detail, intent }
}

/** The text a selection answers with; out of range (or no options) answers nothing. */
export function answerForSelection(options: string[], index: number): string {
  return options[index] ?? ASK_SKIP
}

/** 19 §3.3: the review card's answer, as the JSON ``present_plan`` parses. */
export function planReviewAnswer(choice: "approve" | "keep", feedback = ""): string {
  return feedback ? JSON.stringify({ choice, feedback }) : JSON.stringify({ choice })
}

/** 19 §3.3: only an explicit approve arms the auto-run at the turn's end. */
export function isPlanApproval(answer: string): boolean {
  try {
    const parsed = JSON.parse(answer) as { choice?: unknown }
    return parsed?.choice === "approve"
  } catch {
    return false
  }
}
