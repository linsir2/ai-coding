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
  // M3 T11: a submitted plan is not an anonymous tool call -- the transcript
  // says what happened, and the card can be opened for the plan itself.
  present_plan: "提交计划",
  finish_research: "收尾研究",
  // B0: one call fans out several read-only questions; without a verb it
  // would draw as the generic 运行工具.
  parallel_research: "并行调研",
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
  // A-4: the process sandbox cannot run here (no bwrap / namespaces).  It is
  // an environment failure, so it gets ✗ rather than the ⊘ a refusal wears --
  // "被拒绝" said a human had said no, which is not what happened.
  sandbox_unavailable: { label: "沙箱不可用（命令未执行）", tone: "failed" },
  // The file tools' boundary: a path outside the session's workspace (trace
  // 2026-10-31: a sub-agent in its worktree was handed absolute main-repo
  // paths and four calls were refused).  It is an environment refusal, not
  // breakage, and "未知原因（sandbox_violation）" said nothing.
  sandbox_violation: { label: "路径越界（沙箱拒绝）", tone: "denied" },
  // R0-3's machine code for a delegated run that used up its clock.
  child_timeout: { label: "子代理超时", tone: "failed" },
  tool_timeout: { label: "工具调用超时", tone: "failed" },
  // The stale-read protection's two refusals.  They reached the wire with
  // ``error = None`` before, so the card showed a bare "write: ?" and the
  // transcript never said why (measured 2026-10-31: one item looped 75
  // iterations on this).
  stale_read: { label: "文件已被外部修改，需重读", tone: "denied" },
  must_read_first: { label: "需先读取文件", tone: "denied" },
  // The plan is frozen after approval: a step cannot rewrite verify/content or
  // add items, and shell_read refuses anything outside its read-only subset.
  frozen_plan: { label: "计划执行中，不能改计划", tone: "denied" },
  frozen_verify: { label: "计划执行中，不能改验收命令", tone: "denied" },
  frozen_item: { label: "计划执行中，不能改条目内容", tone: "denied" },
  read_only_shell_required: { label: "只允许只读命令", tone: "denied" },
  tool_error: { label: "工具异常", tone: "failed" },
  unknown_tool: { label: "未知工具", tone: "failed" },
  mcp_error: { label: "MCP 调用失败", tone: "failed" },
  subagent_error: { label: "子代理失败", tone: "failed" },
  // 20 §批次2: present_plan's verdicts are refusals (the plan is not ready),
  // not breakage; the old fallback drew them as 未知原因 in the middle of the
  // planning flow.
  no_plan_items: { label: "计划尚未写成条目", tone: "denied" },
  missing_plan: { label: "计划内容为空", tone: "denied" },
  no_user: { label: "无人评审", tone: "denied" },
  user_prompt_error: { label: "评审通道异常", tone: "failed" },
  // 内化批次: the readonly gate's own code was the last live 未知原因
  // (7ad86804's transcript).
  readonly_denied: { label: "只读拒绝", tone: "denied" },
  // R03: present_plan's posture gates -- a step or plain session calling it
  // gets a verdict, not 未知原因.
  not_in_plan: { label: "非计划模式", tone: "denied" },
  task_busy: { label: "任务运行中", tone: "denied" },
  // R08: keep planning is the user's revision request, not breakage.
  plan_keep: { label: "评审要求继续规划", tone: "denied" },
  // N1: the research exit -- one turn, one exit, and a conclusion that was
  // never written down is a model mistake, not breakage.
  already_concluded: { label: "本回合已收尾", tone: "denied" },
  missing_summary: { label: "结论为空", tone: "denied" },
  plan_already_approved: { label: "计划已批准", tone: "denied" },
  // N1: the request asks for work, so the conclusion exit is not the way out.
  implementation_request: { label: "该请求要实现", tone: "denied" },
  // R21 §3: a plan-step turn has no running goal (``armed_profile == "plan"``),
  // so ``update_goal`` there is a misplaced call, not breakage.  The fallback
  // drew it as 未知原因（no_goal） in the middle of execution.
  no_goal: { label: "无运行中的目标", tone: "denied" },
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
