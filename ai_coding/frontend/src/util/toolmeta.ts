/**
 * Tool card metadata (M2) — a faithful port of the retired ui/blocks.py:
 *
 *   _TOOL_CARD_META  -> TOOL_VERBS     (the Chinese verbs the card renders)
 *   _tool_target()   -> toolTarget()   (which param names the call)
 *   tool_title()     -> toolTitle()    (正在/已 + verb + · target)
 *
 * The old table's English kind labels are deliberately not ported: they only
 * grouped folds in the Textual UI, a concept the parts model does not have.
 * Wire events carry raw params; this module is the only place that turns them
 * into human text.
 */

const TARGET_COMMAND_LIMIT = 60

const TOOL_VERBS: Record<string, string> = {
  bash: "运行命令",
  read: "读取文件",
  edit: "编辑文件",
  write: "写入文件",
  glob: "查找文件",
  grep: "搜索内容",
  todo_write: "更新计划",
  subAgent: "委派子任务",
  skill: "读取技能",
  memory_search: "搜索记忆",
  history_search: "回忆历史",
  memory_write: "记录记忆",
  checkpoint_write: "记录检查点",
  ask_user: "询问用户",
  finish_research: "收尾研究",
  // B0: one call fans out several read-only questions; without a verb it
  // would draw as the generic 运行工具.
  parallel_research: "并行调研",
  // P2-C3: the explicit merge call had no verb, so a merge drew as 运行工具.
  merge_agent: "合并分支",
}

/** The past-tense verb for a tool card; unknown tools (MCP) run under 运行工具. */
export function toolVerb(toolName: string): string {
  return TOOL_VERBS[toolName] ?? "运行工具"
}

/** Flatten whitespace and cut with an ellipsis (blocks.py `_short`). */
export function shorten(text: string, limit: number): string {
  const flat = text.split(/\s+/).filter(Boolean).join(" ")
  return flat.length <= limit ? flat : flat.slice(0, limit) + "…"
}

/**
 * The card's target text: the param that names the call, or the tool's own
 * name when it is not one of ours (so an MCP card still says who it is).
 */
export function toolTarget(toolName: string, params: Record<string, unknown>): string {
  switch (toolName) {
    case "glob":
      return String(params["pattern"] ?? "")
    case "read":
    case "write":
    case "edit":
      return String(params["path"] ?? "")
    case "bash":
      return shorten(String(params["command"] ?? ""), TARGET_COMMAND_LIMIT)
    case "merge_agent":
      return String(params["target"] ?? "")
    case "mcp_call":
      // The dispatcher reports the vessel; the call it carried is in params.
      // Naming the vessel alone made every remote tool read as "运行工具 ·
      // mcp_call", which says nothing about which server ran. A pair that is
      // not both present is left as the vessel rather than rendered half-formed.
      return mcpCallTarget(params)
    default:
      return TOOL_VERBS[toolName] ? "" : toolName
  }
}

/** ``fs__echo`` for the call an ``mcp_call`` actually made. */
function mcpCallTarget(params: Record<string, unknown>): string {
  const server = typeof params["server"] === "string" ? params["server"] : ""
  const tool = typeof params["tool"] === "string" ? params["tool"] : ""
  return server && tool ? `${server}__${tool}` : "mcp_call"
}

/**
 * `正在运行命令 · ls` / `已运行命令 · ls`；失败时是 `已尝试运行命令 · ls`.
 *
 * R22 A-17: the title used to ignore the result, so a failed call read as
 * "已运行命令" over a subtitle saying it never ran.  The subtitle already names
 * the cause, so the title only has to stop claiming the call happened.
 */
export function toolTitle(
  verb: string,
  target: string,
  running: boolean,
  ok = true,
): string {
  const action = running ? `正在${verb}` : ok ? `已${verb}` : `已尝试${verb}`
  return target ? `${action} · ${target}` : action
}

/**
 * How a settled tool call reads.  `denied` means something stopped the call on
 * purpose (the operator, the guard, a hook); `failed` means it broke or ran out
 * of time.  The card colours and strikes through by tone, which is why the
 * distinction is a value and not a boolean.
 */
export type FailureTone = "denied" | "failed"

/**
 * The failure family → human label (A5 §4, re-cut 2026-11).
 *
 * `ToolResult.error` is the gRPC status family now, so this table is the
 * closed set of 16 values: a new family cannot appear without a reader noticing
 * the `未知原因` fallback.  The specific reason travels beside it as
 * `detailCode` (``stale_read`` …) and is for the operator/trace, not for the
 * card's one-line label.
 */
const FAILURES: Record<string, { label: string; tone: FailureTone }> = {
  CANCELLED: { label: "已取消 / 已停止", tone: "denied" },
  UNKNOWN: { label: "未分类错误", tone: "failed" },
  INVALID_ARGUMENT: { label: "参数错误", tone: "failed" },
  DEADLINE_EXCEEDED: { label: "超时", tone: "failed" },
  NOT_FOUND: { label: "目标不存在", tone: "failed" },
  ALREADY_EXISTS: { label: "目标已存在", tone: "denied" },
  PERMISSION_DENIED: { label: "被拒绝", tone: "denied" },
  RESOURCE_EXHAUSTED: { label: "超出配额 / 尺寸上限", tone: "failed" },
  FAILED_PRECONDITION: { label: "前置条件不满足", tone: "denied" },
  ABORTED: { label: "并发冲突，已中止", tone: "denied" },
  OUT_OF_RANGE: { label: "超出范围", tone: "failed" },
  UNIMPLEMENTED: { label: "工具不存在或未实现", tone: "failed" },
  INTERNAL: { label: "工具内部错误", tone: "failed" },
  UNAVAILABLE: { label: "依赖不可用", tone: "failed" },
  DATA_LOSS: { label: "数据损坏或读写失败", tone: "failed" },
  UNAUTHENTICATED: { label: "未认证", tone: "denied" },
}

export function toolFailure(error: string): { label: string; tone: FailureTone } {
  if (!error) return { label: "", tone: "failed" }
  return FAILURES[error] ?? { label: `未知原因（${error}）`, tone: "failed" }
}

/** Milliseconds the way a reader wants it: 820ms / 1.5s / 2m 5s. */
export function formatDuration(ms: number): string {
  if (ms <= 0) return "0ms"
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  const minutes = Math.floor(ms / 60_000)
  return `${minutes}m ${Math.round((ms % 60_000) / 1000)}s`
}

/** A wait below this is noise (an auto-approval costs milliseconds). */
export const APPROVAL_NOTE_MS = 1000

/**
 * A card's duration, naming the wait when a human dominated the call (A-10).
 *
 * `approvalMs` is part of `durationMs`, never an addition.  The parenthetical
 * only appears when the wait is at least a second *and* at least a tenth of
 * the call -- otherwise the row is about the tool, not the operator.
 */
export function formatToolDuration(durationMs: number, approvalMs: number): string {
  const total = formatDuration(durationMs)
  const wait = Math.min(Math.max(approvalMs, 0), Math.max(durationMs, 0))
  if (wait < APPROVAL_NOTE_MS || wait * 10 < durationMs) return total
  return `${total}（审批 ${formatDuration(wait)}）`
}

/** The head of a long tool tail is dropped; say so rather than pretend. */
export function truncationNote(dropped: boolean): string {
  return dropped ? "…（前面的输出已截断）" : ""
}

/** Which pipe a streamed line came off (the wire's ``stream`` field). */
export type ToolStream = "stdout" | "stderr" | "progress"

/** One line of a running tool's output, tagged with the pipe it came from. */
export interface TailLine {
  text: string
  stream: ToolStream
}

/**: Same budget the old flat ``tail`` string had (session.ts TOOL_TAIL_CHARS). */
export const TAIL_CHAR_LIMIT = 4000

export interface FoldedChunk {
  lines: TailLine[]
  /** Text that has not been terminated by a newline yet. */
  pending: string
  /** True once the budget forced the oldest lines out. */
  dropped: boolean
}

/**
 * Fold one streamed chunk into the tail (A7 §2).
 *
 * Chunks arrive wherever the reader thread got to, so a line can arrive in
 * pieces.  Splitting on every chunk would cut a line apart and tag the second
 * half with whichever pipe delivered it, so an unterminated tail is held in
 * ``pending`` and joined with what follows.
 *
 * The budget is on characters, and it drops from the head — the newest output
 * is the part worth reading — reporting that it did, because a tail that
 * silently lost its top reads as the whole story.
 */
export function foldToolChunk(
  lines: TailLine[],
  pending: string,
  text: string,
  stream: ToolStream,
): FoldedChunk {
  if (!text) return { lines, pending, dropped: false }
  const joined = pending + text
  const parts = joined.split("\n")
  // A chunk that ends a line leaves an empty last part: nothing is pending.
  const rest = parts.pop() ?? ""
  const added: TailLine[] = parts.map((line) => ({ text: line, stream }))
  const next = [...lines, ...added]
  let dropped = false
  let chars = next.reduce((sum, line) => sum + line.text.length + 1, 0)
  while (chars > TAIL_CHAR_LIMIT && next.length > 0) {
    const gone = next.shift()!
    chars -= gone.text.length + 1
    dropped = true
  }
  // A command that prints one long line with no newline would otherwise park it
  // all in `pending`, where the card can never show it — and what it holds
  // counts toward the same budget, so a 10k single line has to be cut or the
  // tail stops being a tail.  Promote the open line to a settled one once the
  // total passes the limit, keeping as much of its end as fits, which is what
  // the flat tail used to show.
  if (chars + rest.length > TAIL_CHAR_LIMIT) {
    let room = TAIL_CHAR_LIMIT - chars
    while (room < 0 && next.length > 1) {
      room += next.shift()!.text.length + 1
      dropped = true
    }
    const tail = room > 0 ? rest.slice(-room) : ""
    if (tail.length < rest.length) {
      // The middle of this line is gone.  Same honesty as a dropped line: the
      // card says the output was cut, so a 4000-char line cannot pass itself
      // off as the whole story.
      dropped = true
    }
    chars += tail.length
    next.push({ text: tail, stream })
    return { lines: next, pending: "", dropped }
  }
  return { lines: next, pending: rest, dropped }
}
