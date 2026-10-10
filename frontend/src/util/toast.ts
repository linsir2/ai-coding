export interface ToastItem {
  title?: string
  message: string
  variant: "info" | "success" | "warning" | "error"
}

export function enqueueToast(queue: ToastItem[], item: ToastItem): ToastItem[] {
  return [...queue, item]
}

export function dequeueToast(queue: ToastItem[]): { current: ToastItem | null; rest: ToastItem[] } {
  if (queue.length === 0) return { current: null, rest: [] }
  return { current: queue[0]!, rest: queue.slice(1) }
}
