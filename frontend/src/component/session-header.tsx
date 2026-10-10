/**
 * Session header (A4) — what used to be a tab rail, reduced to the truth.
 *
 * The backend holds ONE live session (`handlers.py:initialize` creates it,
 * `session.load` replaces it), so a strip of "tabs" was a fiction: it listed
 * recent sessions, every one of them looked open, and none of them could be
 * closed.  OpenCode's tabs are really open sessions with `<leader>w` to close
 * and `ctrl+shift+t` to reopen; we have no such state, so we do not pretend.
 *
 * This header shows the current session and nothing else; the history lives in
 * the sessions panel (`<leader>l`), where each row can be opened or deleted.
 */

import { TextAttributes } from "@opentui/core"
import { theme } from "../theme/tokens"

export function SessionHeader(props: { title: string; id: string; onOpen: () => void }) {
  const label = () => {
    const text = props.title.trim()
    return text.length > 0 ? text : "New session"
  }

  return (
    <box flexDirection="row" flexShrink={0} gap={2} paddingLeft={2} paddingRight={2} onMouseUp={props.onOpen}>
      <text
        wrapMode="none"
        fg={theme.textBase}
        attributes={TextAttributes.BOLD}
      >
        {label()}
      </text>
      <text wrapMode="none" fg={theme.textMuted}>
        {props.id.slice(0, 8)}
      </text>
    </box>
  )
}