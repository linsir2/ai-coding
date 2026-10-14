/**
 * r21: the clipboard command table, pinned against OpenCode's.
 *
 * Nothing here touches a clipboard, a terminal, or a display server -- only
 * which command would be run, and in what order.
 */

import { describe, expect, test } from "bun:test"
import { imageCommands, isPng, looksLikeWSL } from "../src/util/clipboard"

function probe(platform: string, installed: string[], isWSL = false) {
  return { platform, isWSL, has: (tool: string) => installed.includes(tool) }
}

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