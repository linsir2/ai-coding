/**
 * IPC client — vscode-jsonrpc over a Unix socket (M0.5).
 *
 * Pairs with the Python backend's ``python-lsp-jsonrpc``: both speak
 * Content-Length framed JSON-RPC 2.0 (verified; plan v2 §3.0).
 *
 * The public surface is what the app uses:
 *   - ``request(method, params)``    -> sendRequest
 *   - ``onEvent(listener)``          -> onNotification("event", ...)
 *   - ``onRequest(method, handler)`` -> onRequest (approvals)
 */

import * as net from "node:net"
import * as rpc from "vscode-jsonrpc/node"

export interface EventMessage {
  type: string
  data: Record<string, unknown>
}

type EventListener = (event: EventMessage) => void
type RequestHandler = (params: Record<string, unknown>) => Promise<unknown> | unknown

export class IpcClient {
  private socket: net.Socket | undefined
  private connection: rpc.MessageConnection | undefined
  private closed = false
  private readonly eventListeners = new Set<EventListener>()

  async connect(socketPath: string): Promise<void> {
    const socket = net.connect(socketPath)
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", () => resolve())
      socket.once("error", reject)
    })
    this.socket = socket
    const connection = rpc.createMessageConnection(
      new rpc.StreamMessageReader(socket),
      new rpc.StreamMessageWriter(socket),
    )
    this.connection = connection
    connection.onClose(() => {
      if (this.closed) return
      // Surface the drop as an app-level event; the UI decides what to show.
      for (const listener of this.eventListeners) {
        listener({ type: "connection.closed", data: {} })
      }
    })
    connection.onError(() => {
      /* socket errors also land in onClose; keep the app alive */
    })
    connection.listen()
  }

  request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    if (!this.connection) return Promise.reject(new Error("ipc client not connected"))
    return this.connection.sendRequest(method, params)
  }

  /** Subscribe to server event pushes; returns an unsubscribe function. */
  onEvent(listener: EventListener): () => void {
    this.eventListeners.add(listener)
    const disposable = this.connection?.onNotification("event", (params) =>
      listener(params as EventMessage),
    )
    return () => {
      this.eventListeners.delete(listener)
      disposable?.dispose()
    }
  }

  /** Answer a server-initiated request (the approval round-trip). */
  onRequest(method: string, handler: RequestHandler): void {
    this.connection?.onRequest(method, async (params) =>
      handler((params ?? {}) as Record<string, unknown>),
    )
  }

  close(): void {
    this.closed = true
    this.connection?.dispose()
    this.socket?.destroy()
  }
}
