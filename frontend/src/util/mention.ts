/**
 * `@skill` suggestions, the pure half (r16/E-2).
 *
 * Mirrors `slashQuery`'s boundary rule (start or whitespace before the
 * trigger, no whitespace after) and the registry's mention pattern
 * (`skills/registry.py:428`) closely enough that anything this panel
 * suggests resolves server-side after submit.
 */

import { go } from "fuzzysort"

export interface SkillOption {
  name: string
  description?: string | null
  phase?: string
}

/** The `@…` query before the cursor, or null when no token is active. */
export function mentionQuery(text: string, cursor: number): string | null {
  const upto = text.slice(0, Math.max(0, Math.min(cursor, text.length)))
  for (let index = upto.lastIndexOf("@"); index >= 0; index = upto.lastIndexOf("@", index - 1)) {
    const before = index === 0 ? undefined : upto[index - 1]
    if (before !== undefined && !/\s/.test(before)) continue
    const query = upto.slice(index + 1)
    if (/\s/.test(query) || query.includes("@")) return null
    return query
  }
  return null
}

/** Fuzzy-match the skill list; an empty query keeps it (top `limit`). */
export function filterSkillMentions(
  query: string,
  skills: readonly SkillOption[],
  limit = 10,
): SkillOption[] {
  const needle = query.trim()
  if (!needle) return skills.slice(0, limit)
  return go(needle, skills, {
    keys: ["name", "description"],
    scoreFn: (result) => (result[0]?.score ?? 0) * 2 + (result[1]?.score ?? 0),
    threshold: 0,
  })
    .slice(0, limit)
    .map((result) => result.obj)
}

/** Replace the active `@token` with `@name ` and report the new cursor. */
export function replaceMentionToken(
  text: string,
  cursor: number,
  name: string,
): { text: string; cursor: number } {
  const upto = text.slice(0, Math.max(0, Math.min(cursor, text.length)))
  const at = upto.lastIndexOf("@")
  if (at < 0) return { text, cursor }
  return {
    text: `${text.slice(0, at)}@${name} ${text.slice(upto.length)}`,
    cursor: at + name.length + 2,
  }
}
