/**
 * r21: the clipboard command table, pinned against OpenCode's.
 *
 * Nothing here touches a clipboard, a terminal, or a display server -- only
 * which command would be run, and in what order.
 */

import { describe, expect, test } from "bun:test"
import { capture, imageCommands, isPng, looksLikeWSL, pasteEventOutcome } from "../src/util/clipboard"

function probe(platform: string, installed: string[], isWSL = false) {
  return { platform, isWSL, has: (tool: string) => installed.includes(tool) }
}

describe("pasteEventOutcome", () => {
  // When the terminal has bracketed paste on, it eats the ctrl+v and hands the
  // app a paste event instead of a keypress -- so a keybinding for ctrl+v never
  // runs.  This is the path that actually fires, and it was not being listened
  // for at all.
  test("an image paste event carries its bytes through", () => {
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1])
    const outcome = pasteEventOutcome({ mimeType: "image/png", kind: "binary" }, png)

    expect(outcome).toEqual({ kind: "image", dataUrl: `data:image/png;base64,${Buffer.from(png).toString("base64")}` })
  })

  test("a mime with parameters is still an image", () => {
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1])
    const outcome = pasteEventOutcome({ mimeType: "image/png; charset=binary" }, png)

    expect(outcome.kind).toBe("image")
  })

  test("text stays text so the default insert still happens", () => {
    const outcome = pasteEventOutcome({ mimeType: "text/plain" }, new Uint8Array([104, 105]))

    expect(outcome).toEqual({ kind: "text" })
  })

  // A terminal with no image transport sends an EMPTY paste when the clipboard
  // holds a picture and no text -- it has nothing to forward.  That empty paste
  // is the cue to go and read the host clipboard, and it is the only signal
  // this environment ever gives.
  test("an empty paste is the cue to read the host clipboard", () => {
    expect(pasteEventOutcome(undefined, new Uint8Array([]))).toEqual({ kind: "clipboard" })
  })

  test("an image mime with no bytes also means the clipboard", () => {
    expect(pasteEventOutcome({ mimeType: "image/png" }, new Uint8Array([]))).toEqual({
      kind: "clipboard",
    })
  })

  // A terminal that sends text while the clipboard holds an image must not make
  // the app paste a stray line -- that is the clipboard reader's job, not this.
  test("an event with no metadata and no bytes still means the clipboard", () => {
    expect(pasteEventOutcome(undefined, new Uint8Array([]))).toEqual({ kind: "clipboard" })
  })
})

describe("imageCommands", () => {
  // The order is the fallback.  A Wayland box has wl-paste and no xclip; an X11
  // box the reverse.  Only listing what is installed would leave a tool that
  // exists but cannot connect as the only candidate.
  test("linux tries wl-paste before xclip", () => {
    const commands = imageCommands(probe("linux", ["xclip", "wl-paste"]))

    expect(commands.map((command) => command.argv[0])).toEqual(["wl-paste", "xclip"])
  })

  test("linux with only xclip still offers xclip", () => {
    const commands = imageCommands(probe("linux", ["xclip"]))

    expect(commands).toHaveLength(1)
    expect(commands[0]!.argv).toEqual(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
  })

  test("linux with neither tool offers neither", () => {
    expect(imageCommands(probe("linux", []))).toEqual([])
  })

  test("WSL asks the Windows clipboard, which is where it lives", () => {
    const commands = imageCommands(probe("linux", ["wl-paste"], true))

    expect(commands).toHaveLength(1)
    expect(commands[0]!.argv[0]).toBe("powershell.exe")
    expect(commands[0]!.encoding).toBe("base64-png")
  })

  test("darwin goes through osascript", () => {
    const commands = imageCommands(probe("darwin", []))

    expect(commands[0]!.argv[0]).toBe("osascript")
    expect(commands[0]!.argv.join(" ")).toContain('the clipboard as "PNGf"')
  })

  test("win32 goes through powershell", () => {
    const commands = imageCommands(probe("win32", []))

    expect(commands[0]!.argv[0]).toBe("powershell.exe")
  })

  test("an unknown platform gets nothing rather than a guess", () => {
    expect(imageCommands(probe("freebsd", ["xclip"]))).toEqual([])
  })
})

describe("looksLikeWSL", () => {
  test("the kernel release names it", () => {
    expect(looksLikeWSL("5.15.0-microsoft-standard-WSL2", {})).toBe(true)
  })

  test("an env var names it when the release does not", () => {
    expect(looksLikeWSL("5.15.0-generic", { WSL_DISTRO_NAME: "Ubuntu" })).toBe(true)
  })

  test("a normal Linux box is not WSL", () => {
    expect(looksLikeWSL("6.1.0-generic", { HOME: "/home/linsir365" })).toBe(false)
  })
})

describe("isPng", () => {
  test("accepts a PNG header", () => {
    expect(isPng(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2]))).toBe(true)
  })

  // A tool that fails writes its error to stdout often enough to matter, and
  // "Error: Can't open display" would otherwise be pasted as a broken image.
  test("rejects a shell error message", () => {
    expect(isPng(new TextEncoder().encode("Error: Can't open display"))).toBe(false)
  })

  test("rejects an empty payload", () => {
    expect(isPng(new Uint8Array([]))).toBe(false)
  })
})
/**
 * D6: `capture` must have a hard upper bound.
 *
 * Measured before this: a clipboard command that ignores SIGTERM left the read
 * awaiting forever, so the TUI's hint stayed on "正在读取剪贴板…" and every
 * retry leaked another process.  Two waits were unbounded, not one --
 * `child.exited` *and* `for await (child.stdout)`, the second because a
 * surviving grandchild still holds the pipe's write end.
 *
 * The command is a real `sh` (which clipboard.ts already assumes exists) with a
 * real short budget, so the assertions are about the bound, never about exact
 * milliseconds: that is the only thing here that could be flaky.
 */
describe("capture has a hard upper bound", () => {
  test("a command that ignores SIGTERM still returns", async () => {
    const started = Date.now()
    const bytes = await capture(["sh", "-c", "trap '' TERM; sleep 30"], 300)
    expect(Date.now() - started).toBeLessThan(5000)
    // Nothing to paste, not an error: killing the group closes the pipe, so
    // the read finishes with zero bytes.  `isPng` rejects that, which is the
    // contract the caller actually relies on.
    expect(bytes === null || bytes.length === 0).toBe(true)
  })

  test("a surviving grandchild does not hold the read open", async () => {
    // The parent's death is not the end of the pipe: this grandchild keeps the
    // write end, so a fix that only bounded `child.exited` still hangs here.
    const started = Date.now()
    const bytes = await capture(["sh", "-c", "sleep 30 & trap '' TERM; sleep 30"], 300)
    expect(Date.now() - started).toBeLessThan(5000)
    expect(bytes === null || bytes.length === 0).toBe(true)
  })

  test("a command that finishes is not cut short", async () => {
    const bytes = await capture(["sh", "-c", "printf hello"], 5000)
    expect(new TextDecoder().decode(bytes ?? new Uint8Array())).toBe("hello")
  })
})
