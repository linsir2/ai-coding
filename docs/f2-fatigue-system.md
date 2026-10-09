# F2: Fatigue System — Paris' Law Error Accumulation + Inflammation Budget

> **Status**: Design complete, implementation pending
> **Priority**: High (prevents cascading failures, simple to implement)
> **Depends on**: Nothing (standalone, additive changes only)

## Problem

Current error handling is **binary**: either a hard failure (trigger recovery) or ignore (continue). Sub-threshold errors (slightly wrong tool calls, near-misses, recoverable hallucinations) accumulate silently until the trajectory collapses unpredictably.

Additionally, defensive actions (sub-agent spawns, deep memory searches, skill cascades) have no cost accounting. The agent can spend its entire context budget reacting to a single perceived threat.

## Design

### 1. Paris' Law Error Accumulation

Track a per-context `fatigue_index`. Every sub-threshold error contributes:

```
ΔD = C × (severity)^m
```

- `severity` = token waste + deviation from expected trajectory (1-5 scale)
- `C`, `m` = material constants (calibratable from trace logs)
- When `fatigue_index` crosses threshold → graceful fracture (reset/escalate/switch)

### 2. Inflammation Budget

Every defensive action has a cost. Maintain a per-session sliding-window inflammation budget. When budget exceeded, downgrade from *adaptive* response (expensive) to *innate* response (cheap).

## Architecture

```
┌──────────────────────────────────────────────────┐
│              Fatigue Governor                     │
├──────────────────────────────────────────────────┤
│                                                  │
│  fatigue_index = Σ C × severity^m × decay(t)    │
│                                                  │
│  ┌─────────────────────────────────────────┐    │
│  │  Level 0 (fatigue < 0.3): Normal mode    │    │
│  │  Level 1 (0.3-0.6): Reduce sub-agents    │    │
│  │  Level 2 (0.6-0.8): Force simplify       │    │
│  │  Level 3 (> 0.8): Graceful fracture      │    │
│  └─────────────────────────────────────────┘    │
│                                                  │
│  inflammation_budget = sliding_window(60s)       │
│  Defense costs: sub-agent=0.3, deep_search=0.2   │
│  Budget exhausted → degrade to innate response   │
│                                                  │
└──────────────────────────────────────────────────────────────────┘
```

## Implementation Scope

### Files Modified

| File | Change | Lines |
|------|--------|-------|
| `ai_coding/core/turn_books.py` | Add fatigue_index tracking, inflammation_budget field | ~20 |
| `ai_coding/core/turn_policy.py` | Add fatigue-based decision rules | ~30 |
| `ai_coding/core/agent_loop.py` | Add inflammation cost accounting, degradation logic | ~40 |

### Files Created

| File | Purpose |
|------|---------|
| `tests/test_fatigue_system.py` | TDD tests |

## Test Plan

1. **Fatigue accumulation**: 3 sub-threshold errors → fatigue_index > 0
2. **Fatigue threshold**: fatigue > 0.8 → graceful fracture triggered
3. **Inflammation cost**: Spawn sub-agent → budget decreases
4. **Budget exhaustion**: Budget = 0 → degrade to innate response
5. **Budget recovery**: After 60s → budget recovers
6. **Level transitions**: fatigue 0.3 → Level 1, 0.6 → Level 2, 0.8 → Level 3

## Success Criteria

- [ ] All 6 test cases pass
- [ ] ruff/mypy clean
- [ ] No regressions in existing tests
- [ ] Fatigue index predicts failure ~5 turns before collapse
- [ ] Inflammation budget prevents >80% context spend on single threat
