/**
 * Session tabs (M4) — OpenCode's horizontal tab strip, reduced to this app's
 * single-active-session model: a clickable list of recent sessions with the
 * current one highlighted, plus a "+ New" affordance.  Switching loads and
 * replays (same path as the sessions dialog); the keyboard equivalents are
 * the session.next/prev/new commands in the palette.
 */

import { TextAttributes } from "@opentui/core"
import { For } from "solid-js"
import { theme } from "../theme/tokens"

export interface SessionTab {
  id: string
  title: string
  preview?: string
}

export function SessionTabs(props: {
  sessions: SessionTab[]
  currentId: string
  onSelect: (id: string) => void
  onNew: () => void
}) {
  const label = (tab: SessionTab) => {
    const text = tab.title || tab.preview || tab.id.slice(0, 8)
    return text.length > 12 ? `${text.slice(0, 12)}…` : text
  }

  return (
    <box flexDirection="row" flexShrink={0} gap={2} paddingLeft={2} paddingRight={2}>
      <For each={props.sessions}>
        {(tab) => (
          <text
            wrapMode="none"
            fg={tab.id === props.currentId ? theme.textBase : theme.textMuted}
            attributes={tab.id === props.currentId ? TextAttributes.BOLD : undefined}
            onMouseUp={() => props.onSelect(tab.id)}
          >
            {label(tab)}
          </text>
        )}
      </For>
      <text wrapMode="none" fg={theme.hueInteractive} onMouseUp={props.onNew}>
        + New
      </text>
    </box>
  )
}
