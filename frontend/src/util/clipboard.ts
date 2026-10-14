/**
 * r21: read the clipboard the way OpenCode does.
 *
 * The host clipboard library @opentui/core ships is the wrong mechanism here.
 * It talks to the display server natively, so it cannot reach a clipboard over
 * SSH and has no fallback when the platform has no library for the current
 * session -- and every one of its refusals used to be swallowed, which is why
 * ctrl+v did nothing and said nothing.
 *
 * OpenCode's TUI reads the clipboard by running the platform's own tool and
 * falling through a list until one answers:
 *
 *   linux   wl-paste -t image/png
 *           xclip -selection clipboard -t image/png -o
 *   darwin  osascript, which writes the clipboard as PNG to a file
 *   win32   powershell.exe, System.Windows.Forms.Clipboard::GetImage
 *
 * Those tools resolve DISPLAY / WAYLAND_DISPLAY themselves and are the ones
 * already installed wherever a desktop exists.  Which command to try is decided
 * by `imageCommands`, which is pure so the order can be pinned without a
 * clipboard, a terminal, or the platform's tools installed.
 */

import { readFileSync, rmSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

export interface ClipboardCommand {
  /** argv for the read. */
  argv: string[]
  /** What the tool emits, which decides how the bytes become a data URL. */
  encoding: "png-bytes" | "base64-png" | "png-file"
  /** For `png-file`: where the command wrote it. */
  file?: string
}

export interface ClipboardProbe {
  platform: string
  /** WSL reports linux but keeps the clipboard on the Windows side. */
  isWSL: boolean
  /** Whether the tool exists on PATH. */
  has: (tool: string) => boolean
}

const WSL_MARKERS = ["microsoft", "WSL"]

/**
 * The image commands to try, in order, for this host.
 *
 * Order is OpenCode's and the reason for it is fallback: a Wayland session
 * usually has `wl-paste` and no `xclip`, an X11 session the reverse.  Stopping
 * at the first tool that merely exists is not enough -- the first one can exist
 * and still fail to connect, so the caller tries every one until bytes arrive.
 */
export function imageCommands(probe: ClipboardProbe): ClipboardCommand[] {
  const { platform, isWSL } = probe
  if (platform === "darwin") {
    // osascript cannot print binary to stdout; OpenCode has the clipboard written
    // to a file by the script and reads that back.  The path is a placeholder
    // the caller fills in, because only it knows a writable directory.
    return [
      {
        argv: ["osascript", "-e", 'set imageData to the clipboard as "PNGf"'],
        encoding: "png-file",
        file: "",
      },
    ]
  }
  if (platform === "win32" || isWSL) {
    return [
      {
        argv: [
          "powershell.exe",
          "-NonInteractive",
          "-NoProfile",
          "-command",
          "Add-Type -AssemblyName System.Windows.Forms; " +
            "$img = [System.Windows.Forms.Clipboard]::GetImage(); " +
            "if ($img) { $ms = New-Object System.IO.MemoryStream; " +
            "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); " +
            "[System.Convert]::ToBase64String($ms.ToArray()) }",
        ],
        encoding: "base64-png",
      },
    ]
  }
  if (platform === "linux") {
    const commands: ClipboardCommand[] = []
    if (probe.has("wl-paste")) {
      commands.push({ argv: ["wl-paste", "-t", "image/png"], encoding: "png-bytes" })
    }
    if (probe.has("xclip")) {
      commands.push({
        argv: ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
        encoding: "png-bytes",
      })
    }
    return commands
  }
  return []
}

/** WSL keeps the clipboard on the Windows host, so ask Windows for it. */
export function looksLikeWSL(release: string | undefined, env: Record<string, string | undefined>): boolean {
  const text = (release ?? "").toLowerCase()
  if (WSL_MARKERS.some((marker) => text.includes(marker.toLowerCase()))) return true
  return Object.keys(env).some((key) => key.toUpperCase().startsWith("WSL"))
}

/** The PNG signature, so a shell error message cannot pass as an image. */
export function isPng(bytes: Uint8Array): boolean {
  const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
  return bytes.length > signature.length && signature.every((byte, index) => bytes[index] === byte)
}

/** The metadata the terminal attaches to a paste, if any. */
export interface PasteMetadataLike {
  mimeType?: string
  kind?: "text" | "binary" | "unknown"
}

/**
 * Decide what a terminal-delivered paste is.
 *
 * A terminal with bracketed paste enabled consumes ctrl+v itself and sends the
 * clipboard wrapped in paste markers, so the keybinding never runs -- this is
 * the path that actually fires, and nothing was listening on it.  Text is
 * reported as text so the Textarea's own insert still runs; only a genuine
 * image mime is taken over here.
 */
export function pasteEventOutcome(
  metadata: PasteMetadataLike | undefined,
  bytes: Uint8Array,
): { kind: "image"; dataUrl: string } | { kind: "text" } | { kind: "clipboard" } {
  const mime = (metadata?.mimeType ?? "").split(";")[0]!.trim().toLowerCase()
  if (mime.startsWith("image/") && bytes.length > 0) {
    return { kind: "image", dataUrl: `data:${mime};base64,${Buffer.from(bytes).toString("base64")}` }
  }
  // An empty paste is the only signal a terminal can give for a clipboard that
  // holds a picture and no text: it has nothing to forward, so it forwards
  // nothing.  Real text always has bytes, so this cannot swallow an ordinary
  // paste; it is the cue to consult the host clipboard instead.
  if (bytes.length === 0) return { kind: "clipboard" }
  return { kind: "text" }
}

/** Run one command and collect stdout.  Never throws: a tool that is missing,
 *  hangs, or exits non-zero is a tool that does not answer. */
async function capture(argv: string[], timeoutMs: number): Promise<Uint8Array | null> {
  const child = Bun.spawn(argv, { stdout: "pipe", stderr: "ignore", stdin: "ignore" })
  const timer = setTimeout(() => child.kill(), timeoutMs)
  try {
    await child.exited
    const chunks: Uint8Array[] = []
    for await (const chunk of child.stdout) chunks.push(chunk)
    return new Uint8Array(Buffer.concat(chunks))
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

/** Does this tool exist?  Probed once per process, not once per keypress. */
const toolCache = new Map<string, boolean>()
function hasTool(tool: string): boolean {
  const cached = toolCache.get(tool)
  if (cached !== undefined) return cached
  let found = false
  try {
    found =
      Bun.spawnSync(["sh", "-c", `command -v ${tool}`], {
        stdout: "ignore",
        stderr: "ignore",
      }).exitCode === 0
  } catch {
    found = false
  }
  toolCache.set(tool, found)
  return found
}

/**
 * Read an image off the host clipboard, or say why there is none.
 *
 * Each command in the table is tried in order, not just the ones present: a
 * `wl-paste` that is installed while the session is X11 exits non-zero rather
 * than returning nothing, and the next entry is what actually answers.  Only a
 * real PNG signature ends the walk, because `xclip` writes its error to stdout
 * on some builds.
 *
 * The default timeout is generous on purpose.  Spawning powershell.exe and
 * loading System.Windows.Forms costs about 1.9 s on a cold WSL host -- a 2 s
 * deadline sat right on top of it, so the same paste worked or silently did
 * nothing depending on the day.  A paste is something the user asked for and is
 * waiting on; failing it to save eight seconds helps nobody.
 */
export async function readClipboardImage(
  timeoutMs = 10_000,
): Promise<{ dataUrl: string } | { error: string }> {
  const commands = imageCommands({
    platform: process.platform,
    isWSL: looksLikeWSL(process.release?.name ?? undefined, process.env),
    has: hasTool,
  })
  if (commands.length === 0) {
    return {
      error:
        "本机没有可用的剪贴板读取工具（Linux 需要 wl-paste 或 xclip；" +
        "macOS 需要 osascript；Windows 需要 powershell）",
    }
  }
  const attempted: string[] = []
  for (const command of commands) {
    attempted.push(command.argv[0]!)
    if (command.encoding === "base64-png") {
      const raw = await capture(command.argv, timeoutMs)
      const text = new TextDecoder().decode(raw ?? new Uint8Array()).trim()
      if (!text || !/^[A-Za-z0-9+/=\s]+$/.test(text)) continue
      return { dataUrl: `data:image/png;base64,${text.replace(/\s+/g, "")}` }
    }
    if (command.encoding === "png-file") {
      const path = join(tmpdir(), `ai-coding-clipboard-${process.pid}.png`)
      const script = [
        ...command.argv,
        "-e",
        `set fileRef to open for access POSIX file "${path}" with write permission`,
        "-e",
        "set eof fileRef to 0",
        "-e",
        "write imageData to fileRef",
        "-e",
        "close access fileRef",
      ]
      await capture(script, timeoutMs)
      let bytes: Uint8Array | null = null
      try {
        bytes = new Uint8Array(readFileSync(path))
      } catch {
        bytes = null
      } finally {
        rmSync(path, { force: true })
      }
      if (bytes && isPng(bytes)) {
        return { dataUrl: `data:image/png;base64,${Buffer.from(bytes).toString("base64")}` }
      }
      continue
    }
    const bytes = await capture(command.argv, timeoutMs)
    if (bytes && isPng(bytes)) {
      return { dataUrl: `data:image/png;base64,${Buffer.from(bytes).toString("base64")}` }
    }
  }
  return { error: `剪贴板里没有图片（试过：${attempted.join("、")}）` }
}