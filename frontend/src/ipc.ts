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
    // ONE connection-level handler, fanning out to the subscriber set.
    // vscode-jsonrpc keys notification handlers by method in a Map -- last
    // registration wins (connection.js onNotification) -- so registering one
    // connection handler per onEvent() would let a dialog's subscription
    // silently replace the session route's: measured live 2026-10-18 on the
    // jobs panel (the stop refreshed the dialog's own rows while the sidebar
    // stayed on the start snapshot, and after the dialog closed nobody
    // received events at all).  The dispatcher is registered once, here.
    connection.onNotification("event", (params) => {
      for (const listener of [...this.eventListeners]) {
        listener(params as EventMessage)
      }
    })
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

  /** Subscribe to server event pushes; returns an unsubscribe function.
   *
   * Subscribers form a set this client fans out to from its one connection
   * handler (see ``connect``): registration order never matters and no
   * subscriber can displace another. */
  onEvent(listener: EventListener): () => void {
    this.eventListeners.add(listener)
    return () => {
      this.eventListeners.delete(listener)
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
