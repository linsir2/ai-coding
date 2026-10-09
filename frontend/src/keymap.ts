/**
 * Keymap setup — @opentui/keymap (M0.5), the same engine OpenCode uses.
 *
 * Layers are declared with ``useBindings`` inside components (see app.tsx);
 * this module owns the renderer-bound instance and the universal addons.
 */

import type { CliRenderer } from "@opentui/core"
import { registerDefaultKeys, registerMetadataFields } from "@opentui/keymap/addons"
import { createDefaultOpenTuiKeymap } from "@opentui/keymap/opentui"

export type AppKeymap = ReturnType<typeof setupKeymap>

export function setupKeymap(renderer: CliRenderer) {
  const keymap = createDefaultOpenTuiKeymap(renderer)
  registerDefaultKeys(keymap)
  // desc/group/title/category on commands (used by the command palette later).
  registerMetadataFields(keymap)
  return keymap
}
