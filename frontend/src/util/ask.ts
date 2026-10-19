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
 * plan markdown and ``intent`` picks the card.  ``planReviewAnswer`` is that
 * card's answer codec; ``planReviewItems`` reads the rows R04 put on the wire.
 *
 * There is deliberately no "is this an approval?" helper here.  One existed
 * (C removed it): the card's buttons now hand their intent to the caller with
 * the answer, so the client never re-judges what the backend's
 * ``present_plan`` already decided — the two disagreed on approve+feedback,
 * and the client was the one that would have armed a run the backend refused.
 */

/** R04: one review row as the card renders it (dependsOn in display order). */
export interface PlanItemData {
  content: string
  verify: string
  dependsOn: number[]
}

export interface AskRequest {
  question: string
  options: string[]
  /** 19 §3.2: the plan-review markdown body; empty for a plain question. */
  detail: string
  /** 19 §3.2: which card renders this request ("" = the generic ask panel). */
  intent: string
  /**
   * R04: the structured payload beside detail/intent (the plan review's item
   * rows today; the generic panel ignores it).  Always an object — the wire
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

/**
 * R04: the review card's rows, shape-checked -- what the card renders is what
 * approval will write.  Shapeless entries are dropped, never guessed at.
 */
export function planReviewItems(request: AskRequest): PlanItemData[] {
  const raw = request.data["items"]
  if (!Array.isArray(raw)) return []
  const items: PlanItemData[] = []
  for (const entry of raw) {
    if (!isRecord(entry)) continue
    const content = typeof entry["content"] === "string" ? entry["content"] : ""
    if (!content) continue
    const verify = typeof entry["verify"] === "string" ? entry["verify"] : ""
    const rawDeps = entry["dependsOn"]
    const dependsOn = Array.isArray(rawDeps)
      ? rawDeps.filter((n): n is number => typeof n === "number" && Number.isInteger(n))
      : []
    items.push({ content, verify, dependsOn })
  }
  return items
}

/** The text a selection answers with; out of range (or no options) answers nothing. */
export function answerForSelection(options: string[], index: number): string {
  return options[index] ?? ASK_SKIP
}

/** 19 §3.3: the review card's answer, as the JSON ``present_plan`` parses. */
export function planReviewAnswer(choice: "approve" | "keep", feedback = ""): string {
  return feedback ? JSON.stringify({ choice, feedback }) : JSON.stringify({ choice })
}
