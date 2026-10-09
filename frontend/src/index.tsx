/**
 * Frontend entry: connect to the Python backend, say hello, render.
 *
 * Started by `ai_coding/server/launcher.py` with AI_CODING_SOCKET set; the
 * tty is ours, the IPC rides the socket (vscode-jsonrpc, M0.5).
 */

import { createCliRenderer } from "@opentui/core"
import { KeymapProvider } from "@opentui/keymap/solid"
import { render } from "@opentui/solid"
import { App, type Hello } from "./app"
import { IpcClient } from "./ipc"
import { setupKeymap } from "./keymap"

const socketPath = process.env.AI_CODING_SOCKET
if (!socketPath) {
  console.error("AI_CODING_SOCKET is not set — start the TUI via `ai-coding`")
  process.exit(1)
}

const client = new IpcClient()
try {
  await client.connect(socketPath)
} catch (error) {
  console.error(`cannot connect to backend at ${socketPath}: ${error}`)
  process.exit(1)
}

let hello: Hello
try {
  hello = (await client.request("initialize")) as unknown as Hello
} catch (error) {
  console.error(`initialize failed: ${error}`)
  process.exit(1)
}

// The renderer is created up-front so the keymap can bind to it (the keymap
// engine intercepts input at the host level, before focused renderables).
// exitOnCtrlC is off: ctrl+c is an app command that shuts the backend down
// cleanly (see app.exit in app.tsx).
const renderer = await createCliRenderer({ exitOnCtrlC: false })
const keymap = setupKeymap(renderer)

// render() resolves once the tree is mounted; the renderer (stdin listener)
// keeps the process alive after that.  The client must NOT be closed here —
// it is the app's only channel back to the backend.
await render(
  () => (
    <KeymapProvider keymap={keymap}>
      <App client={client} hello={hello} />
    </KeymapProvider>
  ),
  renderer,
)
