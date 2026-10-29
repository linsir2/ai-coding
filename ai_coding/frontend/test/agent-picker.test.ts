/**
 * T7 (M4c): the agent is visible, switchable, replayable.
 *
 * RED first: util/agent, state/agents and SessionState.agent do not exist
 * yet, so these imports fail until the GREEN lands.
 *
 * - agent.switched updates the current agent AND inserts the notice (the tag
 *   on a message is history attribution, never the current posture);
 * - shift+tab cycles the visible-primary order (state/agents), wrapping;
 * - the same agent id gets the same accent in message and status bar.
 */

import { describe, expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { initialState, reduce } from "../src/state/session"
import type { SessionState } from "../src/state/session"
import { agentAccent } from "../src/util/agent"
import {
  nextAgentId,
  normalizeAgents,
  type AgentSummary,
} from "../src/state/agents"
import { theme } from "../src/theme/tokens"

const LIST: AgentSummary[] = [
  { id: "build", name: "Build", kind: "primary", source: "builtin", description: "" },
  { id: "plan", name: "Plan", kind: "primary", source: "builtin", description: "" },
  { id: "reviewer", name: "Reviewer", kind: "primary", source: "project", description: "" },
]

const switched = (from: string, to: string, reason = "agent.select") =>
  reduce(initialState(), {
    kind: "wire",
    event: { type: "agent.switched", data: { from, to, reason } },
  })

const noticesOf = (state: SessionState): string[] =>
  state.messages.flatMap((m) =>
    m.parts.filter((p) => p.kind === "notice").map((p) => (p as { text: string }).text),
  )

describe("agent.switched updates the current agent (T7)", () => {
  test("the event writes SessionState.agent and inserts the notice", () => {
    const state = switched("build", "plan")
    expect(state.agent).toBe("plan")
    expect(noticesOf(state)).toEqual(["Switched agent: build → plan（agent.select）"])
  })

  test("the current agent starts empty (stats hydrates it on mount)", () => {
    expect(initialState().agent).toBeNull()
  })

  test("a second switch moves the current agent again", () => {
    const first = switched("build", "plan")
    const second = reduce(first, {
      kind: "wire",
      event: { type: "agent.switched", data: { from: "plan", to: "reviewer", reason: "agent.select" } },
    })
    expect(second.agent).toBe("reviewer")
  })
})

describe("agent.switched carries the read-only axis (R21 §2)", () => {
  const posture = (data: Record<string, unknown>) =>
    reduce(initialState(), { kind: "wire", event: { type: "agent.switched", data } })

  test("a readonly-only flip lands without a bogus agent notice", () => {
    // ``/readonly`` moves this axis alone; the agent id is the same on both
    // ends, so "Switched agent: build → build" must not appear.
    const state = posture({ from: "build", to: "build", reason: "/readonly", readonly: true })

    expect(state.readonly).toBe(true)
    expect(state.agent).toBe("build")
    expect(noticesOf(state)).toEqual([])
  })

  test("a run that leaves the plan posture clears it in the same event", () => {
    const plan = posture({ from: "build", to: "plan", reason: "/plan", readonly: true })
    const running = reduce(plan, {
      kind: "wire",
      event: {
        type: "agent.switched",
        data: { from: "plan", to: "build", reason: "/plan run", readonly: false },
      },
    })

    expect(running.readonly).toBe(false)
    expect(running.agent).toBe("build")
  })

  test("an event that names no readonly axis leaves the known one alone", () => {
    const known = posture({ from: "build", to: "build", readonly: true })
    const next = reduce(known, {
      kind: "wire",
      event: { type: "agent.switched", data: { from: "build", to: "plan", reason: "agent.select" } },
    })

    expect(next.readonly).toBe(true)
    expect(noticesOf(next)).toEqual(["Switched agent: build → plan（agent.select）"])
  })

  test("stats hydration writes both axes", () => {
    const state = reduce(initialState(), {
      kind: "agent-synced",
      agent: "build",
      readonly: false,
    })

    expect(state.agent).toBe("build")
    expect(state.readonly).toBe(false)
  })
})

describe("shift+tab cycles the visible primaries (T7)", () => {
  test("order follows the list, wrapping at the end", () => {
    expect(nextAgentId(LIST, "build")).toBe("plan")
    expect(nextAgentId(LIST, "plan")).toBe("reviewer")
    expect(nextAgentId(LIST, "reviewer")).toBe("build")
  })

  test("an unknown current starts at the head", () => {
    expect(nextAgentId(LIST, null)).toBe("build")
    expect(nextAgentId(LIST, "typo")).toBe("build")
  })

  test("normalizeAgents keeps visible primaries only, sorted by id", () => {
    const rows = normalizeAgents([
      { id: "reviewer", name: "R", kind: "primary", source: "project", description: "" },
      { id: "secret", name: "S", kind: "primary", source: "project", description: "", hidden: true },
      { id: "helper", name: "H", kind: "subagent", source: "builtin", description: "" },
      { id: "build", name: "B", kind: "primary", source: "builtin", description: "" },
    ])
    expect(rows.map((r) => r.id)).toEqual(["build", "reviewer"])
  })
})

describe("message tag, composer and status bar share the one mapping (T7)", () => {
  const src = (rel: string) =>
    readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8")

  test("all three read agentAccent, no second mapping", () => {
    for (const rel of [
      "../src/component/message.tsx",
      "../src/component/composer-meta.tsx",
      "../src/component/prompt.tsx",
      "../src/component/status-bar.tsx",
    ]) {
      expect(src(rel)).toContain("agentAccent")
    }
  })
})

describe("agentAccent is the one color mapping (T7)", () => {
  test("the plan agent reads warning, everything else the agent color", () => {
    expect(agentAccent("plan")).toBe(theme.feedbackWarning)
    expect(agentAccent("build")).toBe(theme.agentColor)
    expect(agentAccent("reviewer")).toBe(theme.agentColor)
  })

  test("the same id always maps to the same accent", () => {
    expect(agentAccent("reviewer")).toBe(agentAccent("reviewer"))
  })
})
