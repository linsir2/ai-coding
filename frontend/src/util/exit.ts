/**
 * The one exit path every quit goes through (I5).
 *
 * Order is the contract: close the socket, tear the renderer down so the
 * terminal leaves raw mode, tell the user anything worth telling, then exit.
 * ``process.exit`` on its own would strand the shell in the alternate screen
 * — OpenTUI's ``process.on("exit")`` only drops a console cache, so the tty
 * restore lives in ``renderer.destroy()``.
 */

export interface ExitDeps {
  close: () => void
  destroy: () => void
  exit: () => void
  error?: (message: string) => void
}

export function quitSequence(deps: ExitDeps, options?: { message?: string }): void {
  deps.close()
  deps.destroy()
  const message = options?.message
  if (message) deps.error?.(message)
  deps.exit()
}
