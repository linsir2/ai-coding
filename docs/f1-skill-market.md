# F1: Skill Market — Dynamic Pricing + Rhizomatic Graph + Memory Staking

> **Status**: Design complete, implementation pending
> **Priority**: Highest (easiest to implement, direct A/B test, immediate improvement)
> **Depends on**: Nothing (standalone, additive changes only)

## Problem

Three static, non-adaptive mechanisms in the current system:

1. **SOP Router scoring** uses hardcoded weights (`keyword=3, tool=2, task_type=2, failure=4`). These never change, even when a skill's effectiveness drifts (e.g., a React skill becomes less relevant as the codebase shifts to Vue).

2. **Memory eviction** uses a fixed formula (`importance + 0.5*hits + 0.5*freshness`). A memory's "worth" is not directly tied to its empirical track record — a memory that has been contradicted 5 times is treated the same as one that has been validated 5 times.

3. **Skill discovery** is passive — the model must fail or stall before discovering the next needed skill. There's no anticipatory loading based on co-activation patterns.

## Design

Three additive signals that replace static configuration with continuous learning:

### 1. Dynamic Skill Pricing (Bayesian Market Maker)

Each skill maintains a price `p ∈ [0,1]` updated via Bayesian updating after each invocation:

```
p_new = p_old + α × (outcome - p_old)
```

- `outcome ∈ {0,1}` (failure/success)
- `α` = learning rate (default 0.3)
- New skills enter at `p = 0.5` (maximum uncertainty)
- Skills with `p < θ` for `T` consecutive uses get quarantined for human review

**Router scoring changes**: `final_score = base_score × p + freshness_bonus`

This replaces the current "score then threshold" with "score × confidence then threshold". A skill with a high base score but low price (historically unreliable) gets deprioritized.

### 2. Rhizomatic Skill Graph (Co-activation Tracking)

A directed graph where edge (A→B) means "after using Skill A, users often needed Skill B". Edge weight = co-activation frequency.

During routing, after selecting the top skill, the system checks the rhizomatic graph for strongly-connected follow-up skills and **pre-loads** their context (frontmatter + first 3 steps) into a warm cache.

This is invisible to the model — it just sees the pre-loaded content in the recommended-skills block.

### 3. Memory Staking with Slashing Conditions

Each memory entry carries a stake `s` (initial = 1.0):

- **Success** (retrieved and used, code passes tests): `s ← s × (1 + reward_rate)`
- **Contradiction** (new evidence contradicts the memory): `s ← s × (1 - slash_rate)`

Memory eviction uses stake-weighted value. Retrieval priority also scales with stake. High-stake but recently-contradicted memories trigger "adversarial review" — the system actively seeks to confirm or refute them.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Skill Market Layer                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    Bayesian update   ┌──────────────────┐        │
│  │  Skill A     │◄─────────────────────│  Outcome         │        │
│  │  p=0.85      │                      │  Feedback        │        │
│  │  stake=1.2   │                      │  (0/1)           │        │
│  └──────┬───────┘                      └──────────────────┘        │
│         │                                                           │
│         │ co-activation graph                                       │
│         ▼                                                           │
│  ┌──────────────┐    pre-load        ┌──────────────────┐        │
│  │  Skill B     │───────────────────►│  Warm Cache      │        │
│  │  p=0.72      │                    │  (frontmatter)   │        │
│  │  stake=0.6   │                    └──────────────────┘        │
│  └──────┬───────┘                                                  │
│         │                                                           │
│         │ stake-weighted                                            │
│         ▼                                                           │
│  ┌──────────────────────────────────────────────┐                  │
│  │  Router Score = base_score × p + freshness   │                  │
│  │  Memory Value = importance + 0.5×hits +      │                  │
│  │                 0.5×freshness + stake        │                  │
│  └──────────────────────────────────────────────┘                  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

## Data Flow

### Skill Price Update Flow
```
Skill invoked → Outcome recorded (success/failure) → Bayesian update → Price persisted
     │                                                              │
     └──────────────────────────────────────────────────────────────┘
     (next routing call uses updated price)
```

### Co-activation Graph Flow
```
Skill A used → Skill B used within 3 turns → Edge(A,B) weight += 1
     │
     └─ Next routing: check edges from top skill → pre-load follow-ups
```

### Memory Stake Flow
```
Memory retrieved → Used successfully → Stake × (1 + reward_rate)
Memory contradicted → Stake × (1 - slash_rate)
     │
     └─ Eviction: stake-weighted value determines survival
     └─ Retrieval: stake boosts priority
```

## Implementation Scope

### Files Modified

| File | Change | Lines |
|------|--------|-------|
| `ai_coding/core/sop_router.py` | Add price field to SkillStats, add Bayesian update method, modify scoring to multiply by price | ~30 |
| `ai_coding/core/sop_router.py` | Add co-activation graph, add pre-load logic | ~40 |
| `ai_coding/memory/store.py` | Add stake field to MemoryNote (or parallel index), modify `_value_of()` | ~15 |
| `ai_coding/memory/service.py` | Add stake update on recall success/contradiction | ~20 |
| `ai_coding/domain/session.py` | Add skill_prices and coactivation_graph fields | ~5 |

### Files Created

| File | Purpose |
|------|---------|
| `tests/test_skill_market.py` | TDD tests for all three features |

## Test Plan

### Test Cases

1. **Price Update**: Skill with initial p=0.5, after 3 successes → p > 0.5
2. **Price Decay**: Skill with initial p=0.8, after 3 failures → p < 0.8
3. **Price Impact on Routing**: Two skills with same base score, different prices → higher price wins
4. **Co-activation Tracking**: Use Skill A then Skill B → edge(A,B) created
5. **Pre-loading**: After co-activation → follow-up skill pre-loaded
6. **Memory Stake Reward**: Memory used successfully → stake increases
7. **Memory Stake Slash**: Memory contradicted → stake decreases
8. **Stake-weighted Eviction**: Low-stake memory evicted before high-stake
9. **Quarantine**: Skill with p < θ for T uses → quarantined flag set
10. **Persistence**: Prices and stakes survive session restart

## Success Criteria

- [ ] All 10 test cases pass
- [ ] ruff clean on modified files
- [ ] mypy clean on modified files
- [ ] Existing tests still pass (no regressions)
- [ ] Price converges to true success rate after ~20 uses
- [ ] Co-activation graph correctly identifies skill sequences
- [ ] Stake-weighted eviction keeps high-value memories longer
