/**
 * Home route — OpenCode's home layout (M1, element-aligned at 140×42):
 *
 *   flexGrow spacer
 *   height 3 spacer
 *   Logo                     (hidden under 12 rows)
 *   height 1 spacer
 *   prompt wrapper           (width 100%, maxWidth 75, paddingTop 1)
 *   flexGrow spacer
 *   footer                   (hidden under 44 cols / 12 rows)
 *     left ~/dir:branch      right version
 *
 * The shortcut-hints row under the prompt is deliberately absent until the
 * command palette exists (M3) — every visible element maps to a real
 * capability (docs/opencode-gui-m1-plan.md §2).
 */

import { useTerminalDimensions } from "@opentui/solid"
import stringWidth from "string-width"
import { Logo } from "../component/logo"
import { Prompt } from "../component/prompt"
import { theme } from "../theme/tokens"
import { abbreviateHome, formatLocation, truncatePath } from "../util/location"

const PROMPT_EXAMPLES = [
  "Fix a TODO in the codebase",
  "What is the tech stack of this project?",
  "Fix broken tests",
]

export interface HomeProps {
  workspace: string
  version: string
  model: string
  provider: string
  branch: string
  onSubmit: (text: string) => void
}

export function Home(props: HomeProps) {
  const dimensions = useTerminalDimensions()
  const width = () => dimensions().width
  const height = () => dimensions().height
  const placeholder = () =>
    `Ask anything… "${PROMPT_EXAMPLES[Math.floor(Math.random() * PROMPT_EXAMPLES.length)]}"`
  const metadata = () => [props.provider, props.model].filter(Boolean).join(" · ")

  const location = () => {
    const home = process.env.HOME ?? ""
    const label = formatLocation(abbreviateHome(props.workspace, home), props.branch)
    // Reserve: 2 left pad + 2 right pad + version + 2 gap.
    const budget = Math.max(8, width() - stringWidth(props.version) - 6)
    return truncatePath(label, budget)
  }

  const showFooter = () => width() >= 44 && height() >= 12
  const footerPadding = () => (height() < 16 ? 0 : 1)

  return (
    <box flexDirection="column" width="100%" height="100%" alignItems="center">
      <box flexGrow={1} />
      <box height={3} />
      {height() >= 12 ? <Logo width={width()} /> : null}
      <box height={1} />
      <box width="100%" maxWidth={75} paddingTop={1} flexDirection="column">
        <Prompt
          placeholder={placeholder()}
          metadata={metadata()}
          onSubmit={props.onSubmit}
        />
      </box>
      <box flexGrow={1} />
      {showFooter() ? (
        <box
          width="100%"
          flexDirection="row"
          justifyContent="space-between"
          paddingLeft={2}
          paddingRight={2}
          paddingTop={footerPadding()}
          paddingBottom={footerPadding()}
          flexShrink={0}
        >
          <text fg={theme.textMuted}>{location()}</text>
          <text fg={theme.textMuted}>{props.version}</text>
        </box>
      ) : null}
    </box>
  )
}
