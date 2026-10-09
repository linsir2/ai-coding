/**
 * Tool card metadata (M2) — a faithful port of ai_coding/ui/blocks.py:
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
