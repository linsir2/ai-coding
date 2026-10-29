/**
 * TDD (M5): slash-command autocomplete — the pure half.
 *
 * Port of OpenCode's trigger rule (prompt/display.ts::slashTriggerIndex): a
 * `/` starts a command only at a word boundary, with no space before the
 * cursor and no second slash.  The catalogue is only what this TUI can run.
 */

import { describe, expect, test } from "bun:test"
import { readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"
import {
  SLASH_COMMANDS,
  filterSlashCommands,
  ownCommand,
  slashQuery,
  slashSelectionText,
} from "../src/util/slash"

/**
 * M5b: `/mcp` is the entry the operator asked for — the MCP dialog had been
 * reachable only from the palette, and a slash command that listed it was cut
 * back in MCP-MCP §6.  Reversed on request: the panel answers "whose server
 * did I connect", which is exactly what a slash catalogue is for.
 */
describe("mcp slash entry", () => {
  test("/mcp runs the MCP dialog command", () => {
    const entry = SLASH_COMMANDS.find((command) => command.name === "mcp")
    expect(entry?.command).toBe("mcp.list")
    // A keymap-backed entry submits nothing: the command runs instead.
    expect(slashSelectionText(entry!)).toBeUndefined()
  })
})

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

  // R22/A-2: the suggestion list used to search descriptions too, so a command
  // the *backend* knows but the catalogue does not was replaced at Enter by a
  // fuzzy neighbour: `/readonly` ran `/export`, `/plan` opened the permissions
  // dialog, `/agents` ran `/tasks`, `/clear` opened permissions, `/resume` ran
  // `/export`.  A name-only match leaves the line alone, so Enter submits it to
  // the backend dispatcher instead of running somebody else's command.
  test("a command the catalogue does not list matches nothing", () => {
    for (const query of ["readonly", "plan", "agents", "clear", "resume", "goal"]) {
      expect(filterSlashCommands(query)).toEqual([])
    }
  })

  test("descriptions are displayed, not searched", () => {
    // "Review the current git diff" is the /review description; "diff" must not
    // surface it, because the same rule would let any description word hijack a
    // typed command.
    expect(filterSlashCommands("diff").map((entry) => entry.name)).toEqual([])
  })

  test("a keymap entry names a dotted command; names carry no slash", () => {
    // A2.3 widened the contract: a text entry has no keymap command (the line
    // is submitted to the backend dispatcher).  What must still hold for a
    // keymap entry is the dotted command name, and every name stays bare.
    for (const command of SLASH_COMMANDS) {
      if (command.command !== undefined) expect(command.command).toMatch(/\./)
      expect(command.name.startsWith("/")).toBe(false)
    }
  })
})

describe("the command catalogue (A2.3)", () => {
  test("every entry is either a keymap command or a plain text submission (I8)", () => {
    // The Home-screen bug this pins: an entry pointing at a command that only
    // the session route registers silently did nothing.  A text entry is
    // submitted verbatim to session.command instead.
    for (const entry of SLASH_COMMANDS) {
      const hasCommand = typeof entry.command === "string" && entry.command.length > 0
      const isText = entry.command === undefined || entry.command === ""
      expect(hasCommand || isText).toBe(true)
    }
  })

  test("the backend's text-report commands are all reachable from the list", () => {
    const names = SLASH_COMMANDS.map((entry) => entry.name)
    for (const expected of ["budget", "trace", "todo", "config", "tasks", "compact", "permissions"]) {
      expect(names).toContain(expected)
    }
  })

  test("plan and readonly are not advertised as slash entries (19 Q1=A)", () => {
    // The composer's mode row owns plan entry now; the backend commands stay
    // as internal interfaces (typing them still works — categorization reads
    // the backend's list, not this catalogue).  This pins the removal so a
    // future edit cannot quietly re-advertise them.
    const names = SLASH_COMMANDS.map((entry) => entry.name)
    expect(names).not.toContain("plan")
    expect(names).not.toContain("readonly")
  })

  test("/memory opens the panel instead of printing a line of counts", () => {
    // M7. The name stays reachable — but as a keymap command, because the text
    // form answered "43 notes, 33 scenes" and nothing you could read, pin or
    // remove. The backend still handles the text form for the non-TUI entry.
    const memory = SLASH_COMMANDS.find((entry) => entry.name === "memory")
    expect(memory?.command).toBe("dialog.memory.show")
  })

  test("a text entry carries the exact line the backend dispatches", () => {
    const budget = SLASH_COMMANDS.find((entry) => entry.name === "budget")
    expect(budget?.command ?? "/budget").toBe("/budget")
  })

  /**
   * R20b: /permissions pointed at "session.permissions", a name no keymap ever
   * registered — selecting it cleared the composer and did nothing.  The
   * catalogue's contract ("points at a command the TUI can actually run") was
   * only ever checked by eye, so this reads the source for the registrations.
   */
  test("every keymap entry names a command the app actually registers", () => {
    const registered = new Set<string>()
    const walk = (dir: string) => {
      for (const item of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, item.name)
        if (item.isDirectory()) walk(full)
        else if (/\.tsx?$/.test(item.name)) {
          const source = readFileSync(full, "utf8")
          for (const match of source.matchAll(/name:\s*"([a-z][\w.]*)"/g)) {
            registered.add(match[1]!)
          }
        }
      }
    }
    walk(join(import.meta.dir, "..", "src"))
    const dead = SLASH_COMMANDS.filter(
      (entry) => entry.command && !registered.has(entry.command),
    )
    expect(dead.map((entry) => `${entry.name} -> ${entry.command}`)).toEqual([])
  })

  test("/permissions opens the dialog rather than a dead pointer", () => {
    const entry = SLASH_COMMANDS.find((command) => command.name === "permissions")
    expect(entry?.command).toBe("dialog.permissions.show")
  })
})

/**
 * R20b: the names the backend has never known.  Typed by hand they used to be
 * submitted as prose — `/models` reached the model as a question.
 */
describe("a typed line that names a TUI command (R20b)", () => {
  test("the four backend-unknown names resolve to their keymap command", () => {
    expect(ownCommand("/models")?.command).toBe("dialog.models.show")
    expect(ownCommand("/sessions")?.command).toBe("dialog.sessions.show")
    expect(ownCommand("/new")?.command).toBe("session.new")
    expect(ownCommand("/mcp")?.command).toBe("mcp.list")
  })

  test("surrounding whitespace does not stop the match", () => {
    expect(ownCommand("  /models  ")?.name).toBe("models")
  })

  test("arguments leave the line to the backend", () => {
    // `/permissions auto` is a text command; the keymap form takes no argument.
    expect(ownCommand("/permissions auto")).toBeUndefined()
  })

  test("a text-only entry is never run directly", () => {
    expect(ownCommand("/budget")).toBeUndefined()
    expect(ownCommand("/export html")).toBeUndefined()
  })

  test("ordinary prose and paths stay prose", () => {
    expect(ownCommand("please read /src/main.ts")).toBeUndefined()
    expect(ownCommand("/src/main.ts")).toBeUndefined()
  })
})

describe("connect slash entry (H3)", () => {
  test("/connect opens the guided provider wizard", () => {
    const matches = filterSlashCommands("conn")
    expect(matches[0]!.name).toBe("connect")
    expect(matches[0]!.command).toBe("dialog.connect.show")
  })
})
