/**
 * Theme mode (M1): detect the terminal's dark/light preference and apply
 * the matching token table before the first render.
 *
 * The detection is a library feature (`renderer.waitForThemeMode` queries
 * the terminal with OSC 11 and times out); the built-in `theme` object from
 * tokens.ts is the single source components read from.
 */

import type { CliRenderer } from "@opentui/core"
import { darkTheme, lightTheme, theme } from "./tokens"

export type ThemeMode = "dark" | "light"

/** Terminal preference with a 1s budget; unknown or failed reads as dark. */
export async function detectThemeMode(renderer: CliRenderer): Promise<ThemeMode> {
  try {
    const mode = await renderer.waitForThemeMode(1000)
    return mode === "light" ? "light" : "dark"
  } catch {
    return "dark"
  }
}

/** Swap the active token values in place (see tokens.ts on why not a context). */
export function applyTheme(mode: ThemeMode): void {
  Object.assign(theme, mode === "light" ? lightTheme : darkTheme)
}
