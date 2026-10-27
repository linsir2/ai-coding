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
 * One reader for plan rows, used by the review card and the history card.
 *
 * A review payload carries ``dependsOn``, already display-shaped (1-based).
 * A tool call carries ``depends_on``, 0-based into that same submission, so a
 * replayed card shows step numbers and must shift it.  Rows without content
 * are dropped, never guessed at.
 */
export function planItems(
  raw: unknown,
  { coerce = false }: { coerce?: boolean } = {},
): PlanItemData[] {
  if (!Array.isArray(raw)) return []
  const items: PlanItemData[] = []
  for (const entry of raw) {
    if (!isRecord(entry)) continue
    // The backend accepts any truthy `content` and normalizes it with
    // ``str(...)`` (present_plan_tool._parse_items).  Dropping a numeric step
    // here would renumber every later row and shift its dependencies.
    const rawContent = entry["content"]
    const content =
      typeof rawContent === "string"
        ? rawContent
        : coerce && (typeof rawContent === "number" || typeof rawContent === "boolean")
          ? String(rawContent)
          : ""
    if (!content.trim()) continue
    const rawVerify = entry["verify"]
    const verify =
      typeof rawVerify === "string"
        ? rawVerify
        : typeof rawVerify === "number"
          ? String(rawVerify)
          : ""
    // The two payloads mean different things: a review card's ``dependsOn`` is
    // already 1-based display data, a tool call's ``depends_on`` is 0-based
    // into the submission.  Read whichever the payload has; never merge them.
    const fromTool = entry["depends_on"] !== undefined
    const rawDeps = fromTool ? entry["depends_on"] : entry["dependsOn"]
    const dependsOn = Array.isArray(rawDeps)
      ? rawDeps.filter((n): n is number => typeof n === "number" && Number.isInteger(n))
      : []
    // One key wins, never a merge: the backend reads only ``depends_on``, so a
    // payload carrying both must not display a union it did not execute.
    const display = fromTool ? dependsOn.map((n) => n + 1) : dependsOn
    items.push({ content, verify, dependsOn: display })
  }
  return items
}

/**
 * R04: the review card's rows, shape-checked -- what the card renders is what
 * approval will write.  Shapeless entries are dropped, never guessed at.
 */
export function planReviewItems(request: AskRequest): PlanItemData[] {
  return planItems(request.data["items"])
}

/** The text a selection answers with; out of range (or no options) answers nothing. */
export function answerForSelection(options: string[], index: number): string {
  return options[index] ?? ASK_SKIP
}

/** 19 §3.3: the review card's answer, as the JSON ``present_plan`` parses. */
export function planReviewAnswer(choice: "approve" | "keep", feedback = ""): string {
  return feedback ? JSON.stringify({ choice, feedback }) : JSON.stringify({ choice })
}

/**
 * M3 T11: the plan a ``present_plan`` call submitted, read back from its
 * arguments.
 *
 * The durable home of a plan is the tool call, not `task.items` (which the
 * run rewrites).  Live and replayed tool parts both carry the same `params`,
 * so this one reader serves the transcript's historical card and keeps it in
 * step with the review card.
 *
 * The rows go through the same reader as the review card (`planItems`), so
 * a key rename in one payload cannot make the two cards disagree.
 */
export function submittedPlan(
  params: Record<string, unknown>,
): { plan: string; items: PlanItemData[] } | null {
  const plan = typeof params["plan"] === "string" ? params["plan"] : ""
  if (!plan.trim()) return null
  // A tool call is the model's own JSON: normalize it the way the backend's
  // ``_parse_items`` does (``str(content)``), so a numeric step is not dropped.
  return { plan, items: planItems(params["items"], { coerce: true }) }
}

/**
 * The plan as the card shows it when a nested markdown renderable has no
 * layout to grow in: drop the block-level markers the operator does not need
 * to read (``#``, ``-``, backticks), keep every word.
 *
 * This is deliberately a display transform, not a second parser: the raw
 * plan stays in `submittedPlan`, and nothing here decides what ran.
 */
export function planText(plan: string): string {
  return plan
    .split("\n")
    .map((line) =>
      line
        .replace(/^\s{0,3}#{1,6}\s+/, "")
        .replace(/^\s{0,3}[-*+]\s+/, "· ")
        .replace(/`/g, "")
        .replace(/\*\*(.+?)\*\*/g, "$1")
        .replace(/__(.+?)__/g, "$1"),
    )
    .join("\n")
}
