/**
 * Renderer-free keymap addons (M5) — the leader token and its timed window.
 *
 * Split from ``keymap.ts`` so the registration can be exercised without a
 * terminal: ``setupKeymap`` binds these to a real renderer, the keymap test
 * binds them to the library's test host.  OpenCode runs the same engine with
 * the same trigger and timeout (v1.18.32 config/keybind.ts:41,
 * context/keymap.tsx:136-146).
 */

import type { Keymap, KeymapEvent } from "@opentui/keymap"
import { registerTimedLeader } from "@opentui/keymap/addons"

/** OpenCode's ``LeaderDefault`` (config/keybind.ts:41). */
export const LEADER_KEY = "ctrl+x"

/** OpenCode's leader timeout (context/keymap.tsx:143). */
export const LEADER_TIMEOUT_MS = 2000

/**
 * Define the ``<leader>`` token the default parser understands.  Returns the
 * dispose function; the app keeps it for the process lifetime.
 */
export function registerAppKeymapAddons<TTarget extends object, TEvent extends KeymapEvent>(
  keymap: Keymap<TTarget, TEvent>,
): () => void {
  return registerTimedLeader(keymap, {
    trigger: LEADER_KEY,
    name: "leader",
    timeoutMs: LEADER_TIMEOUT_MS,
  })
}
