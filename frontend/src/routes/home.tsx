/**
 * Home route — OpenCode's home layout:
 *
 *   (flex spacers)  Logo  (spacer)
 *   centered prompt (max 75 cols) + footer hints
 *   (flex spacer)
 *   footer: left  ~/workspace:branch    right  vX.Y.Z
 */

import { useTerminalDimensions } from "@opentui/solid"
import { Logo } from "../component/logo"
import { Prompt } from "../component/prompt"
import { theme } from "../theme/tokens"

const PROMPT_EXAMPLES = [
  "Fix a TODO in the codebase",
  "What is the tech stack of this project?",
  "Fix broken tests",
]

export interface HomeProps {
  workspace: string
  version: string
  model: string
  onSubmit: (text: string) => void
}

export function Home(props: HomeProps) {
  const dimensions = useTerminalDimensions()
  const width = () => dimensions().width
  const placeholder = () =>
    `Ask anything… "${PROMPT_EXAMPLES[Math.floor(Math.random() * PROMPT_EXAMPLES.length)]}"`
  const location = () => {
    const path = props.workspace.replace(process.env.HOME ?? "", "~")
    return path
  }

  return (
    <box flexDirection="column" width="100%" height="100%" alignItems="center">
      <box flexGrow={1} />
      {width() >= 22 ? <Logo width={width()} /> : null}
      <box height={1} />
      <box width="100%" maxWidth={75} flexDirection="column">
        <Prompt
          placeholder={placeholder()}
          metadata={`Build · ${props.model || "no model"}`}
          onSubmit={props.onSubmit}
        />
        <box height={1} />
        <box width="100%" flexDirection="row" justifyContent="flex-end">
          <text fg={theme.textMuted}>ctrl+p commands</text>
        </box>
      </box>
      <box flexGrow={1} />
      <box
        width="100%"
        flexDirection="row"
        justifyContent="space-between"
        paddingLeft={2}
        paddingRight={2}
      >
        <text fg={theme.textMuted}>{location()}</text>
        <text fg={theme.textMuted}>{`v${props.version}`}</text>
      </box>
    </box>
  )
}
