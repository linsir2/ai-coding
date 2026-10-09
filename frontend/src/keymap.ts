/**
 * Keymap setup — @opentui/keymap (M0.5/M5), the same engine OpenCode uses.
 *
 * Layers are declared with ``useBindings`` inside components (see app.tsx);
 * this module owns the renderer-bound instance.  The renderer-free addons
 * (the ``<leader>`` token) live in ``keymap-addons.ts`` and are shared with
 * the tests; ``createDefaultOpenTuiKeymap`` already installs the default key
 * parser and the enabled/metadata layer fields.
 */

import type { CliRenderer } from "@opentui/core"
import { createDefaultOpenTuiKeymap } from "@opentui/keymap/opentui"
import { registerAppKeymapAddons } from "./keymap-addons"

export type AppKeymap = ReturnType<typeof setupKeymap>

export function setupKeymap(renderer: CliRenderer) {
  const keymap = createDefaultOpenTuiKeymap(renderer)
  registerAppKeymapAddons(keymap)
  return keymap
}
