# F3: Cross-Domain Immune Learning — Cross-Presentation + Session Mycelium Graph

> **Status**: Design complete, implementation pending
> **Priority**: Highest long-term value (solves fundamental knowledge silo problem)
> **Depends on**: F1 (price signals can guide dendritic index)

## Problem

Current system has **knowledge silos**:
- Sessions are isolated — Session A's discoveries don't help Session B
- Skills are siloed — a refactoring skill's patterns can't help a debugging skill
- Memory and skills don't communicate

## Design

### 1. Cross-Presentation Layer (Dendritic Index)

When any skill succeeds, extract its problem's **structural signature** (AST diff pattern, error type, tool sequence) and index it without binding to the original skill. During routing, query the dendritic index for cross-matching signatures. Strong match → boost candidate skill score + inject donor skill's execution trace.

### 2. Session Mycelium Graph

Maintain a graph: nodes = past sessions, edges = similarity (shared tools, overlapping files, analogous errors). New session starts → 2-hop graph walk to find "neighbor-of-neighbor" sessions that solved structurally similar but surface-different problems.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                Cross-Domain Immune System                │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────┐    structural     ┌────────────────┐  │
│  │ Session A    │─────signature────►│                │  │
│  │ (refactoring)│                   │  Dendritic     │  │
│  └──────────────┘                   │  Index         │  │
│                                     │  (structural   │  │
│  ┌──────────────┐    structural     │   features →   │  │
│  │ Session B    │─────signature────►│   skill)       │  │
│  │ (debugging)  │                   │                │  │
│  └──────────────┘                   └───────┬────────┘  │
│                                              │          │
│  ┌──────────────┐    2-hop walk             │          │
│  │ Session C    │◄──────────────────────────┘          │
│  │ (deployment) │                                      │
│  └──────────────┘                                      │
│         │                                              │
│         ▼                                              │
│  ┌─────────────────────────────────────────────┐      │
│  │ Route: trigger match + dendritic boost      │      │
│  │ + 2-hop trace injection                     │      │
│  └─────────────────────────────────────────────┘      │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## Implementation Scope

### Files Modified

| File | Change | Lines |
|------|--------|-------|
| `ai_coding/core/sop_router.py` | Add dendritic index, cross-presentation scoring | ~60 |
| `ai_coding/core/sop_router.py` | Add session graph, 2-hop walk | ~50 |
| `ai_coding/domain/session.py` | Add session signature field | ~5 |
| `ai_coding/memory/service.py` | Add structural signature extraction | ~30 |

### Files Created

| File | Purpose |
|------|---------|
| `tests/test_cross_domain_immune.py` | TDD tests |

## Test Plan

1. **Dendritic index**: Skill A succeeds → structural signature indexed
2. **Cross-presentation match**: New query matches signature → score boost
3. **Trace injection**: Cross-match → donor trace injected
4. **Session graph**: 3 sessions with shared tools → edges created
5. **2-hop walk**: Session C → finds Session A via Session B
6. **Cross-domain transfer**: Refactoring session helps debugging session

## Success Criteria

- [ ] All 6 test cases pass
- [ ] ruff/mypy clean
- [ ] No regressions
- [ ] Cross-presentation boosts relevant skills by 20-40%
- [ ] 2-hop walk finds analogous sessions in O(n) time
