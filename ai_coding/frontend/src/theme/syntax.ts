/**
 * Markdown syntax style (M2) — required by the library's `<markdown>`
 * renderable (`MarkdownOptions.syntaxStyle`).  `SyntaxStyle.create()` is the
 * renderable's own default fallback; one shared instance keeps the native
 * handle count at one instead of one per text part.
 */

import { SyntaxStyle } from "@opentui/core"

export const markdownSyntax = SyntaxStyle.create()
