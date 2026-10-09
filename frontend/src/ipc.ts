/**
 * JSON-RPC client over a Unix socket (newline-delimited JSON).
 *
 * Mirrors the wire contract in ai_coding/server/protocol.py:
 *   -> {"jsonrpc":"2.0","id":N,"method":"session.prompt","params":{...}}
 *   <- {"jsonrpc":"2.0","id":N,"result":{...}}        (answer)
 *   <- {"jsonrpc":"2.0","method":"event","params":{...}}   (push)
 *   <- {"jsonrpc":"2.0","id":N,"method":"approval.request"} (server asks)
 */

import { Socket } from "node:net";

export interface EventMessage {
  type: string;
  data: Record<string, unknown>;
}

type EventListener = (event: EventMessage) => void;
type RequestHandler = (params: Record<string, unknown>) => Promise<unknown>;

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

export class IpcClient {
  private socket: Socket | undefined;
  private buffer = "";
  private nextId = 1;
  private readonly pending = new Map<number, Pending>();
  private readonly listeners = new Set<EventListener>();
  private readonly requestHandlers = new Map<string, RequestHandler>();
  private closed = false;

  connect(socketPath: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const socket = new Socket();
      socket.once("connect", () => resolve());
      socket.once("error", (error) => reject(error));
      socket.on("data", (chunk) => this.onData(chunk));
      socket.on("close", () => this.onClose());
      socket.connect(socketPath);
      this.socket = socket;
    });
  }

  /** Send a request; resolves with the server's result. */
  request(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
    const id = this.nextId++;
    const message = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket?.write(message, (error) => {
        if (error) {
          this.pending.delete(id);
          reject(error);
        }
      });
    });
  }

  /** Subscribe to server event pushes; returns an unsubscribe function. */
  onEvent(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Register an answer to a server-initiated request (e.g. approvals). */
  onRequest(method: string, handler: RequestHandler): void {
    this.requestHandlers.set(method, handler);
  }

  close(): void {
    this.closed = true;
    this.socket?.end();
  }

  private onData(chunk: Buffer | string): void {
    this.buffer += chunk.toString("utf-8");
    let index: number;
    while ((index = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      this.handleLine(line);
    }
  }

  private handleLine(line: string): void {
    if (!line.trim()) return;
    let message: Record<string, unknown>;
    try {
      message = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return; // a malformed line is dropped, same as the server side
    }
    const id = message["id"];
    const method = message["method"];
    if (typeof method === "string") {
      if (typeof id === "number") {
        void this.answerServerRequest(id, method, (message["params"] ?? {}) as Record<string, unknown>);
      } else if (method === "event") {
        this.emitEvent((message["params"] ?? {}) as EventMessage);
      }
      return;
    }
    if (typeof id === "number") {
      this.resolvePending(id, message);
    }
  }

  private resolvePending(id: number, message: Record<string, unknown>): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    this.pending.delete(id);
    if ("error" in message) {
      const error = message["error"] as { message?: string } | undefined;
      pending.reject(new Error(error?.message ?? "ipc error"));
    } else {
      pending.resolve(message["result"]);
    }
  }

  private async answerServerRequest(
    id: number,
    method: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    const handler = this.requestHandlers.get(method);
    let payload: Record<string, unknown>;
    try {
      const result = handler ? await handler(params) : null;
      payload = { jsonrpc: "2.0", id, result };
    } catch (error) {
      payload = {
        jsonrpc: "2.0",
        id,
        error: { code: -32603, message: String(error) },
      };
    }
    this.socket?.write(JSON.stringify(payload) + "\n");
  }

  private emitEvent(event: EventMessage): void {
    for (const listener of this.listeners) {
      listener(event);
    }
  }

  private onClose(): void {
    for (const pending of this.pending.values()) {
      pending.reject(new Error("ipc connection closed"));
    }
    this.pending.clear();
    if (!this.closed) {
      for (const listener of this.listeners) {
        listener({ type: "connection.closed", data: {} });
      }
    }
  }
}
