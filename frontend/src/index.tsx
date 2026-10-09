/**
 * Frontend entry: connect to the Python backend, say hello, render.
 *
 * Started by `ai_coding/server/launcher.py` with AI_CODING_SOCKET set; the
 * tty is ours, the IPC rides the socket.
 */

import { render } from "@opentui/solid"
import { App, type Hello } from "./app"
import { IpcClient } from "./ipc"

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

// render() resolves once the tree is mounted; the renderer (stdin listener)
// keeps the process alive after that.  The client must NOT be closed here —
// it is the app's only channel back to the backend.
await render(() => <App client={client} hello={hello} />)
