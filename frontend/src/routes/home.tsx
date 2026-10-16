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

import { useBindings } from "@opentui/keymap/solid"
import { useTerminalDimensions } from "@opentui/solid"
import { createSignal } from "solid-js"
import stringWidth from "string-width"
import { DialogSkills } from "../component/dialog-skills"
import { Logo } from "../component/logo"
import { Prompt } from "../component/prompt"
import { useDialog } from "../dialog/context"
import type { IpcClient } from "../ipc"
import type { PromptImage } from "../util/attachment"
import type { MentionOption } from "../util/mention"
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
  onSubmit: (text: string, images: PromptImage[], mode: "normal" | "plan") => void
  client: IpcClient
  /** The registry's skills, for the composer's `@` suggestions (r16/E-2). */
  skills: MentionOption[]
  /** Workspace files/directories for the same list (r17/T4). */
  files: MentionOption[]
  /** The dispatcher's vocabulary (r19/F5); decides what a submission is. */
  commands: string[]
}

export function Home(props: HomeProps) {
  const dimensions = useTerminalDimensions()
  const width = () => dimensions().width
  const height = () => dimensions().height
  const dialog = useDialog()
  const [promptSeed, setPromptSeed] = createSignal("")
  // 20 §批次1: the same two gears the session row shows, pre-armable before
  // the Session route (and its keymap layer) exists at all.
  const [mode, setMode] = createSignal<"normal" | "plan">("normal")

  // r16/E-1: Home has a composer too — the old app-layer registration called
  // this panel read-only on the belief that it did not. Selecting a skill
  // seeds `@name`, so a session can start on a skill, not only during one.
  useBindings(() => ({
    enabled: !dialog.isOpen(),
    commands: [
      {
        name: "dialog.skills.show",
        title: "Skills",
        group: "Skill",
        desc: "List available skills",
        palette: true,
        run: () =>
          dialog.open(
            () => (
              <DialogSkills
                client={props.client}
                onSelect={(name: string) => setPromptSeed(`@${name} `)}
              />
            ),
            "large",
          ),
      },
      {
        // 20 §批次1: the Toggle lives at both layers because the two routes
        // never mount together; the palette shows exactly one. The carry into
        // the session is `planPrelude` (util/submission), not this command.
        name: "home.mode.cycle",
        title: "Toggle plan mode",
        group: "Session",
        desc: "Switch between Build and read-only Plan mode",
        palette: true,
        run: () => setMode((value) => (value === "plan" ? "normal" : "plan")),
      },
    ],
    bindings: [{ key: "shift+tab", cmd: "home.mode.cycle" }],
  }))

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
          mode={mode()}
          onSubmit={(text, images) => props.onSubmit(text, images, mode())}
          seed={promptSeed()}
          onSeedDone={() => setPromptSeed("")}
          skills={props.skills}
          files={props.files}
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
