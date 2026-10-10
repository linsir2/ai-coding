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
  todo_write: "更新计划",
  subAgent: "委派子任务",
  skill: "读取技能",
  memory_search: "搜索记忆",
  memory_write: "记录记忆",
  checkpoint_write: "记录检查点",
  ask_user: "询问用户",
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
    default:
      return TOOL_VERBS[toolName] ? "" : toolName
  }
}

/** `正在运行命令 · ls` while running, `已运行命令 · ls` once settled. */
export function toolTitle(verb: string, target: string, running: boolean): string {
  const action = `${running ? "正在" : "已"}${verb}`
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
 * The failure code → human label (A5 §4).  Transcribed from the documented
 * `ToolResult.error` contract (docs/data-contracts.md:159,167,347) so the
 * screen never shows a machine code; an unknown code keeps itself visible
 * rather than being swallowed, so a new backend code is visible when it lands.
 */
const FAILURES: Record<string, { label: string; tone: FailureTone }> = {
  denied: { label: "被拒绝", tone: "denied" },
  not_approved: { label: "未获批准", tone: "denied" },
  guard_denied: { label: "门卫拦截", tone: "denied" },
  hook_blocked: { label: "钩子拦截", tone: "denied" },
  approval_timeout: { label: "审批超时", tone: "denied" },
  stopped: { label: "已停止", tone: "denied" },
  skipped: { label: "已跳过", tone: "denied" },
  nonzero_exit: { label: "命令返回非零", tone: "failed" },
  timeout: { label: "命令超时", tone: "failed" },
  spawn_failed: { label: "命令无法启动", tone: "failed" },
  tool_timeout: { label: "工具调用超时", tone: "failed" },
  tool_error: { label: "工具异常", tone: "failed" },
  unknown_tool: { label: "未知工具", tone: "failed" },
  mcp_error: { label: "MCP 调用失败", tone: "failed" },
  subagent_error: { label: "子代理失败", tone: "failed" },
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

/** The head of a long tool tail is dropped; say so rather than pretend. */
export function truncationNote(dropped: boolean): string {
  return dropped ? "…（前面的输出已截断）" : ""
}
