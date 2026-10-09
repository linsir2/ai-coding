/**
 * TDD (M5): slash-command autocomplete — the pure half.
 *
 * Port of OpenCode's trigger rule (prompt/display.ts::slashTriggerIndex): a
 * `/` starts a command only at a word boundary, with no space before the
 * cursor and no second slash.  The catalogue is only what this TUI can run.
 */

import { describe, expect, test } from "bun:test"
import { SLASH_COMMANDS, filterSlashCommands, slashQuery } from "../src/util/slash"

describe("slashQuery", () => {
  test("a slash at the start of the input triggers with the typed query", () => {
    expect(slashQuery("/hel", 4)).toBe("hel")
  })

  test("a slash after whitespace triggers", () => {
    expect(slashQuery("hi /he", 6)).toBe("he")
  })

  test("a slash inside a word (a path) does not trigger", () => {
    expect(slashQuery("src/main.ts", 11)).toBeNull()
  })

  test("a space ends the command token", () => {
    expect(slashQuery("/he lp", 7)).toBeNull()
  })

  test("a second slash means it is a path, not a command", () => {
    expect(slashQuery("/a/b", 4)).toBeNull()
  })

  test("plain text has no query", () => {
    expect(slashQuery("hello", 5)).toBeNull()
  })

  test("a bare slash triggers with an empty query", () => {
    expect(slashQuery("/", 1)).toBe("")
  })

  test("only the text before the cursor counts", () => {
    expect(slashQuery("/help me", 5)).toBe("help")
  })
})

describe("filterSlashCommands", () => {
  test("an empty query keeps the catalogue", () => {
    expect(filterSlashCommands("")).toBe(SLASH_COMMANDS)
  })

  test("a name match ranks first", () => {
    expect(filterSlashCommands("hel")[0]!.name).toBe("help")
    expect(filterSlashCommands("mod")[0]!.name).toBe("models")
  })

  test("no match yields an empty list", () => {
    expect(filterSlashCommands("zzzzz")).toEqual([])
  })

  test("every catalogue entry maps to a keymap command", () => {
    for (const command of SLASH_COMMANDS) {
      expect(command.command).toMatch(/\./)
      expect(command.name.startsWith("/")).toBe(false)
    }
  })
})
