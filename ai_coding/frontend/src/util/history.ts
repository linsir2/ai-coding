/**
 * Title for the "history got shorter" toast (R22/A-3).
 *
 * A terminal event reports every same-session rewrite through one field
 * (`compacted: {before, after}`), so the client had one title for all of them:
 * running `/undo` showed "Compacted 7 → 0 messages" — a compaction that never
 * happened, which reads as "something compressed my session" when the operator
 * asked for the opposite.
 *
 * The discriminator is the *event*, not the text: the undo payload rides only
 * on the commands that rewind (``handlers.py::_notify_terminal_turn``), and it
 * is present even when the undone turn carried nothing but an image — a truthy
 * check on the text would have called that one a compaction.
 */
export function historyRewriteTitle(rewound: boolean): string {
  return rewound ? "Undone" : "Compacted"
}

/**
 * Which session a `turn.finished` re-projection must load (R22/A-11).
 *
 * `/resume <id>` and `/clear` end with a NEW_SESSION action *and* `reload: true`
 * in the same event.  The route switched the tab through the parent
 * (`onSelectSession` → an IPC round trip before `props.sessionId` changes), so
 * the reload branch read the id the app had *started* in — an empty session that
 * never reached disk — and the server answered `session not found`:
 *
 *     [ERROR] Error: ValueError: session not found: 0330a025…
 *
 * The event carries the authoritative id, so a switch projects *that*.  An
 * in-place rewrite (`/undo`, `/redo`) is the same session and keeps the prop;
 * a switch without an id (an older backend) projects nothing rather than asking
 * for the id that just went stale — the route's own load covers it.
 */
export function reloadTarget(
  action: unknown,
  eventSessionId: unknown,
  propSessionId: string,
): string | null {
  if (action !== "new_session") return propSessionId
  const next = typeof eventSessionId === "string" ? eventSessionId : ""
  return next || null
}
