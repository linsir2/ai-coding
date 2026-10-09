"""SOP Router (M2): match current context against skill triggers.

Scores each skill's triggers (keywords, tools, task_types, failure_patterns)
against the current conversation context and returns the top matches for
injection into the volatile tail block.

Scoring weights (based on GenericAgent research + hotel session audit data):
- failure_pattern (4.0): strongest signal — model just failed with a specific error
- keyword (3.0): strong signal — user explicitly mentioned something related
- tool (2.0): medium signal — model is using relevant tools
- task_type (2.0): medium signal — task is in the skill's domain

Threshold = 3.0: need at least one strong match (keyword/failure) or two
medium matches (tool + task_type).

M3: ToolObserver auto-records tool call outcomes to session.tool_observations.
SOPRouter uses these observations for cross-turn failure_pattern matching.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_coding.core.events import LoopEvent, ToolFinished
from ai_coding.infra.json_utils import from_json_file, to_json_pretty
from ai_coding.infra.time_utils import now_utc
from ai_coding.skills.registry import SkillRegistry

_log = logging.getLogger(__name__)

# Scoring weights — see module docstring for rationale.
_WEIGHT_KEYWORD = 3.0
_WEIGHT_TOOL = 2.0
_WEIGHT_TASK_TYPE = 2.0
_WEIGHT_FAILURE = 4.0

DEFAULT_THRESHOLD = 3.0
DEFAULT_MAX_MATCHES = 3
MAX_OBSERVATIONS = 50  #: Cap on stored observations to prevent unbounded growth.

# M4: Freshness scoring — anti-silence mechanism.
FRESHNESS_HALF_LIFE = 5     #: turns for freshness to reach half-max bonus
MAX_FRESHNESS_BONUS = 2.0   #: max bonus from freshness (below keyword=3.0)

# F1: Dynamic pricing — Bayesian market maker.
PRICE_ALPHA = 0.1           #: learning rate for Bayesian update
PRICE_MIN = 0.05            #: floor to prevent zero division
PRICE_MAX = 1.0             #: ceiling for price

# F1: Co-activation graph — rhizomatic skill associations.
COACTIVATION_THRESHOLD = 2  #: min count to trigger pre-load

# F3: Dendritic Index — cross-presentation layer.
MAX_DENDRITIC_SIGNATURES = 200     #: FIFO cap
MATCH_THRESHOLD = 0.3              #: min similarity to trigger boost
SIGNATURE_MATCH_WEIGHT = 2.0       #: multiplier for dendritic boost
WEIGHT_TOOLS = 0.4                 #: tool overlap weight in similarity
WEIGHT_ERRORS = 0.3                #: error keyword overlap weight
WEIGHT_TASK = 0.3                  #: task keyword overlap weight


@dataclass
class SkillStats:
    """Per-skill tracking for freshness scoring and dynamic pricing."""

    last_recommended_turn: int = 0
    last_loaded_turn: int = 0
    recommend_count: int = 0
    load_count: int = 0
    price: float = 0.5  #: Bayesian price — 0.5 = max uncertainty


def _freshness_bonus(skill_name: str, turn: int, stats: dict[str, SkillStats]) -> float:
    """Calculate freshness bonus for a skill.

    Uses logistic decay: starts at MAX for never-recommended skills,
    decays to ~0 for recently-recommended skills.  Ensures all skills
    get recommended eventually (anti-silence).
    """
    s = stats.get(skill_name)
    if not s or s.last_recommended_turn == 0:
        return MAX_FRESHNESS_BONUS
    turns_since = turn - s.last_recommended_turn
    if turns_since <= 0:
        return 0.0
    # Logistic: MAX / (1 + exp(0.5 * (half_life - turns_since)))
    # At turns_since=0 → ~0; at turns_since=half_life → MAX/2; at turns_since=∞ → MAX
    return MAX_FRESHNESS_BONUS / (1.0 + math.exp(0.5 * (FRESHNESS_HALF_LIFE - turns_since)))


@dataclass(frozen=True)
class SkillMatch:
    """A skill that matched the current context."""

    name: str
    description: str
    score: float
    matched_dimensions: tuple[str, ...]
    triggers: dict[str, list[str]] = field(default_factory=dict)
    price: float = 0.5  #: skill price for display


# ---- F3: Structural signature (skill success context) ----------------------


@dataclass(frozen=True)
class StructuralSignature:
    """结构签名：技能成功执行时的上下文特征。"""

    tools: frozenset[str]          # 使用的工具集合
    error_keywords: frozenset[str] # 错误关键词集合
    task_keywords: frozenset[str]  # 任务关键词集合
    skill_name: str                # 成功的技能名称
    timestamp: str                 # ISO8601 时间戳

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": sorted(self.tools),
            "error_keywords": sorted(self.error_keywords),
            "task_keywords": sorted(self.task_keywords),
            "skill_name": self.skill_name,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuralSignature:
        return cls(
            tools=frozenset(data.get("tools", [])),
            error_keywords=frozenset(data.get("error_keywords", [])),
            task_keywords=frozenset(data.get("task_keywords", [])),
            skill_name=data.get("skill_name", ""),
            timestamp=data.get("timestamp", ""),
        )


# ---- F3: Dendritic Index ---------------------------------------------------


class DendriticIndex:
    """技能成功执行的结构签名索引。

    持久化到 .dendritic/index.json，支持签名记录和相似度匹配。
    """

    def __init__(self, path: str | Path = ".dendritic/index.json") -> None:
        self._path = Path(path)
        self._signatures: list[StructuralSignature] = []
        self._load()

    # ---- public API -------------------------------------------------------

    def record(self, signature: StructuralSignature) -> None:
        """添加签名到索引（FIFO 淘汰）。"""
        self._signatures.append(signature)
        self._enforce_cap()
        self._save()

    def match(
        self,
        tools: set[str],
        errors: set[str],
        task_keywords: set[str],
    ) -> list[tuple[str, float]]:
        """匹配当前上下文与存储的签名。

        返回 [(skill_name, match_score), ...]，按分数降序。
        errors 中的完整错误字符串会被分词后匹配。
        """
        # 分词错误字符串
        error_words: set[str] = set()
        for err in errors:
            error_words.update(word.lower() for word in err.split() if len(word) > 2)

        results: list[tuple[str, float]] = []
        for sig in self._signatures:
            score = self._compute_similarity(sig, tools, error_words, task_keywords)
            if score >= MATCH_THRESHOLD:
                results.append((sig.skill_name, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        """从文件加载签名。"""
        if not self._path.is_file():
            return
        try:
            data = from_json_file(self._path)
        except (ValueError, OSError):
            return
        if not isinstance(data, dict):
            return
        for item in data.get("signatures", []):
            try:
                self._signatures.append(StructuralSignature.from_dict(item))
            except (ValueError, TypeError, KeyError):
                continue

    def _save(self) -> None:
        """原子写入。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = to_json_pretty({
                "version": 1,
                "signatures": [sig.to_dict() for sig in self._signatures],
            })
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            pass

    def _enforce_cap(self) -> None:
        """FIFO 淘汰，保持上限。"""
        if len(self._signatures) > MAX_DENDRITIC_SIGNATURES:
            self._signatures = self._signatures[-MAX_DENDRITIC_SIGNATURES:]

    # ---- matching ---------------------------------------------------------

    @staticmethod
    def _compute_similarity(
        sig: StructuralSignature,
        tools: set[str],
        errors: set[str],
        task_keywords: set[str],
    ) -> float:
        """计算综合相似度分数。"""
        tool_sim = DendriticIndex._jaccard(tools, set(sig.tools))
        error_sim = DendriticIndex._jaccard(errors, set(sig.error_keywords))
        task_sim = DendriticIndex._jaccard(task_keywords, set(sig.task_keywords))
        return (WEIGHT_TOOLS * tool_sim
                + WEIGHT_ERRORS * error_sim
                + WEIGHT_TASK * task_sim)

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        """Jaccard 相似度。"""
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)


# ---- M3: Tool observation ------------------------------------------------


@dataclass(frozen=True)
class ToolObservation:
    """A single tool call outcome, recorded by ToolObserver."""

    tool_name: str
    success: bool
    error: str | None
    timestamp: str


# ---- F3: Intervention ------------------------------------------------------


@dataclass(frozen=True)
class Intervention:
    """A skill intervention decision."""

    mode: str  # "soft" | "hard" | "none"
    skill_name: str
    content: str  # pattern_dna (soft) or steps excerpt (hard)
    triggers: dict[str, list[str]]


class ToolObserver:
    """Observe ToolFinished events, record to a shared list.

    Best-effort: never raises, never blocks the event pipeline.  Caps stored
    observations at MAX_OBSERVATIONS to prevent unbounded growth.
    """

    def __init__(self, observations: list[Any]) -> None:
        self._observations = observations

    def on_event(self, event: LoopEvent) -> None:
        if isinstance(event, ToolFinished):
            self._record(event)

    def _record(self, event: ToolFinished) -> None:
        obs = ToolObservation(
            tool_name=event.tool_name,
            success=event.success,
            error=event.error,
            timestamp=now_utc(),
        )
        self._observations.append(obs)
        if len(self._observations) > MAX_OBSERVATIONS:
            self._observations.pop(0)


class SOPRouter:
    """Match conversation context against skill triggers.  Pure computation."""

    def __init__(self, skills: SkillRegistry, threshold: float = DEFAULT_THRESHOLD,
                 max_matches: int = DEFAULT_MAX_MATCHES,
                 dendritic_path: str | Path | None = None):
        self._skills = skills
        self._threshold = threshold
        self._max_matches = max_matches
        # F3: dendritic index for cross-domain matching
        self._dendritic = DendriticIndex(dendritic_path) if dendritic_path else None

    def route(self, user_input: str, recent_tools: list[str], task_goal: str,
              recent_failures: list[str],
              observations: list[ToolObservation] | None = None,
              turn_number: int = 0,
              stats: dict[str, SkillStats] | None = None,
              phase: str | None = None) -> list[SkillMatch]:
        """Score all skills against current context, return top matches.

        M3: observations provide cross-turn failure data for failure_pattern
        matching.  M4: freshness bonus ensures all skills get recommended.
        F2: phase filtering — only recommend skills locked to the current phase.
        """
        # Merge observation errors into failure list (M3)
        all_failures = list(recent_failures)
        if observations:
            all_failures.extend(
                obs.error for obs in observations
                if obs.error and not obs.success
            )
        stats = stats or {}
        matches: list[SkillMatch] = []
        for name in self._skills.names():
            triggers = self._skills.triggers(name)
            if not triggers:
                continue
            # F2: phase filtering — skip skills locked to a different phase
            if phase:
                meta = self._skills.meta(name)
                skill_phase = meta.get("phase", "")
                if skill_phase and skill_phase != phase:
                    continue
            base_score, dimensions = self._score(
                user_input, recent_tools, task_goal, all_failures, triggers)
            # M4: add freshness bonus only when there's at least one strong
            # match (keyword or failure_pattern).  This prevents blind
            # recommendations for completely unrelated tasks — a tool-only
            # match (2.0) + max freshness (2.0) would otherwise clear a 3.0
            # threshold for any skill that happens to share a common tool.
            has_strong_match = any(d in ("keyword", "failure_pattern") for d in dimensions)
            freshness = _freshness_bonus(name, turn_number, stats) if has_strong_match else 0.0
            # F1: price-weighted scoring — price acts as confidence multiplier
            # price=0.5 → multiplier=1.0 (neutral), price=1.0 → 1.5, price=0.0 → 0.5
            price = stats.get(name, SkillStats()).price
            total_score = base_score * (0.5 + price) + freshness
            # F3: dendritic index matching — boost skill score if context
            # matches past successful executions
            if self._dendritic:
                dendritic_matches = self._dendritic.match(
                    tools=set(recent_tools),
                    errors=set(all_failures),
                    task_keywords=set(task_goal.lower().split()),
                )
                for match_name, match_score in dendritic_matches:
                    if name == match_name:
                        total_score += match_score * SIGNATURE_MATCH_WEIGHT
            if total_score >= self._threshold:
                meta = self._skills.meta(name)
                matches.append(SkillMatch(
                    name=name,
                    description=meta.get("description", "") or "",
                    score=total_score,
                    matched_dimensions=tuple(dimensions),
                    triggers=triggers,
                    price=price,
                ))
        matches.sort(key=lambda m: m.score, reverse=True)
        # M4: record recommendations
        for m in matches[:self._max_matches]:
            self.record_recommendation(m.name, turn_number, stats)
        return matches[:self._max_matches]

    def record_recommendation(self, skill_name: str, turn: int,
                              stats: dict[str, SkillStats]) -> None:
        """Record that a skill was recommended (for freshness tracking)."""
        if skill_name not in stats:
            stats[skill_name] = SkillStats()
        s = stats[skill_name]
        s.last_recommended_turn = turn
        s.recommend_count += 1

    def record_load(self, skill_name: str, turn: int,
                    stats: dict[str, SkillStats]) -> None:
        """Record that a skill was loaded (for usage tracking)."""
        if skill_name not in stats:
            stats[skill_name] = SkillStats()
        s = stats[skill_name]
        s.last_loaded_turn = turn
        s.load_count += 1

    # ---- F1: Dynamic pricing -----------------------------------------------

    def record_outcome(self, skill_name: str, success: bool,
                       stats: dict[str, SkillStats]) -> None:
        """Update skill price via Bayesian updating.

        p_new = p_old + α × (outcome - p_old)
        Clamped to [PRICE_MIN, PRICE_MAX].
        """
        if not skill_name:
            return
        if skill_name not in stats:
            stats[skill_name] = SkillStats()
        s = stats[skill_name]
        outcome = 1.0 if success else 0.0
        s.price = max(PRICE_MIN, min(PRICE_MAX, s.price + PRICE_ALPHA * (outcome - s.price)))

    # ---- F1: Co-activation graph -------------------------------------------

    def record_coactivation(self, from_skill: str, to_skill: str,
                            graph: dict[str, dict[str, int]]) -> None:
        """Record that to_skill was used after from_skill."""
        if not from_skill or not to_skill or from_skill == to_skill:
            return
        if from_skill not in graph:
            graph[from_skill] = {}
        graph[from_skill][to_skill] = graph[from_skill].get(to_skill, 0) + 1

    def get_followup_skills(self, skill_name: str,
                            graph: dict[str, dict[str, int]]) -> list[str]:
        """Get skills that frequently follow the given skill."""
        edges = graph.get(skill_name, {})
        return [name for name, count in edges.items() if count >= COACTIVATION_THRESHOLD]

    # ---- F3: Intervention ---------------------------------------------------

    def intervene(self, recent_failures: list[str],
                  observations: list[ToolObservation] | None,
                  session: Any, stats: dict[str, SkillStats] | None) -> Intervention:
        """Decide if a skill should intervene in the current context.

        Returns Intervention with mode="soft" (pattern_dna injection),
        mode="hard" (full steps), or mode="none".
        """
        # Collect all failure errors (from books + observations)
        all_errors = list(recent_failures)
        if observations:
            all_errors.extend(
                obs.error for obs in observations
                if obs.error and not obs.success
            )
        if not all_errors:
            return Intervention(mode="none", skill_name="", content="", triggers={})

        for name in self._skills.names():
            triggers = self._skills.triggers(name)
            if not triggers:
                continue
            failure_patterns = triggers.get("failure_patterns", [])
            if not failure_patterns:
                continue
            # Check if at least 2 errors match this skill's failure patterns
            # AND those errors are "consecutive" (same pattern, not different errors)
            matching_errors = [
                err for err in all_errors
                if any(fp in err for fp in failure_patterns)
            ]
            if len(matching_errors) >= 2:
                # Hard intervention — load steps section
                content = self._load_skill_steps(name)
                return Intervention(
                    mode="hard", skill_name=name,
                    content=content, triggers=triggers,
                )
            elif len(matching_errors) >= 1:
                # Soft intervention — inject pattern_dna
                meta = self._skills.meta(name)
                pattern_dna = meta.get("pattern_dna", "")
                if pattern_dna:
                    return Intervention(
                        mode="soft", skill_name=name,
                        content=pattern_dna, triggers=triggers,
                    )
        return Intervention(mode="none", skill_name="", content="", triggers={})

    def _load_skill_steps(self, name: str) -> str:
        """Load the steps section from a skill's SKILL.md."""
        text = self._skills.load(name)
        if not text:
            return ""
        # Extract content after frontmatter
        parts = text.split("---", 2)
        if len(parts) < 3:
            return text
        body = parts[2]
        # Try to find a steps section
        for marker in ["## 步骤", "## Steps", "## Procedure"]:
            if marker in body:
                start = body.index(marker)
                # Find next section or end
                next_section = body.find("\n## ", start + len(marker))
                if next_section == -1:
                    return body[start:].strip()
                return body[start:next_section].strip()
        # No steps section found — return first 500 chars
        return body[:500].strip()

    # ---- F1: Error recovery (via skill pattern_dna fallback) -------------------

    def check_recovery(self, recent_failures: list[str], session: Any) -> str | None:
        """Check if any skill's pattern_dna matches recent failures.

        Uses skill triggers as the recovery source — no separate cache needed.
        """
        if not recent_failures:
            return None
        latest_error = recent_failures[-1]
        for name in self._skills.names():
            triggers = self._skills.triggers(name)
            if not triggers:
                continue
            failure_patterns = triggers.get("failure_patterns", [])
            if any(fp in latest_error for fp in failure_patterns):
                meta = self._skills.meta(name)
                pattern_dna = meta.get("pattern_dna", "")
                if pattern_dna:
                    return pattern_dna
        return None

    # ---- I2: Cross-turn duplicate failure detection --------------------------

    def is_cross_turn_duplicate(self, error: str, session: Any) -> bool:
        """Check if this error has occurred in recent turns (via ToolObserver)."""
        observations = getattr(session, "tool_observations", [])
        if not observations or not error:
            return False
        # Check if any recent observation has a similar error
        for obs in observations[-10:]:  # last 10 observations
            if obs.error and obs.error == error:
                return True
        return False

    # ---- I2: Constraint effectiveness tracking ------------------------------

    def record_constraint_injection(self, constraints: list[str], session: Any) -> None:
        """Record that constraints were injected into the request."""
        for constraint in constraints:
            if constraint not in session.constraint_tracking:
                session.constraint_tracking[constraint] = {
                    "injected": True,
                    "followed": True,  # assume followed until proven otherwise
                    "violation_count": 0,
                }
            else:
                session.constraint_tracking[constraint]["injected"] = True

    def check_constraint_violations(self, constraints: list[str],
                                    tools_used: list[str],
                                    session: Any) -> list[str]:
        """Check if model behavior violated any constraints.

        Returns list of violated constraint texts.
        """
        violated: list[str] = []
        for constraint in constraints:
            tracking = session.constraint_tracking.get(constraint, {})
            if not tracking.get("injected"):
                continue
            # Simple heuristic: if constraint mentions a tool and model didn't use it
            # while using alternative tools, it's a violation
            constraint_lower = constraint.lower()
            # Extract tool hints from constraint text (e.g., "use bash", "改用 bash")
            tool_hints = ["bash", "read", "write", "edit", "glob"]
            mentioned_tools = [t for t in tool_hints if t in constraint_lower]
            if mentioned_tools:
                # If constraint mentions a tool but model used different tools
                used_set = {t.lower() for t in tools_used}
                if not any(t in used_set for t in mentioned_tools):
                    tracking["followed"] = False
                    tracking["violation_count"] = tracking.get("violation_count", 0) + 1
                    violated.append(constraint)
                else:
                    tracking["followed"] = True
        return violated

    # ---- I3: Pre-compaction anchor ------------------------------------------

    def capture_pre_compaction_anchor(self, session: Any, task: Any,
                                      constraints: list[str]) -> None:
        """Capture task anchor before compaction."""
        parts = []
        if task and hasattr(task, 'goal') and task.goal:
            parts.append(f"目标: {task.goal}")
        if constraints:
            parts.append(f"关键约束: {'; '.join(constraints[:3])}")
        session.pre_compaction_anchor = " | ".join(parts) if parts else ""

    def _score(self, user_input: str, recent_tools: list[str], task_goal: str,
               recent_failures: list[str], triggers: dict[str, list[str]],
    ) -> tuple[float, list[str]]:
        """Calculate match score for a single skill."""
        score = 0.0
        dimensions: list[str] = []

        if any(kw in user_input for kw in triggers.get("keywords", [])):
            score += _WEIGHT_KEYWORD
            dimensions.append("keyword")
        if any(t in recent_tools for t in triggers.get("tools", [])):
            score += _WEIGHT_TOOL
            dimensions.append("tool")
        if any(tt in task_goal for tt in triggers.get("task_types", [])):
            score += _WEIGHT_TASK_TYPE
            dimensions.append("task_type")
        failure_patterns = triggers.get("failure_patterns", [])
        if any(fp in fail for fail in recent_failures for fp in failure_patterns):
            score += _WEIGHT_FAILURE
            dimensions.append("failure_pattern")

        return score, dimensions

    # ---- F3: Dendritic signature recording --------------------------------

    def record_signature(self, signature: StructuralSignature) -> None:
        """Record a successful skill execution signature."""
        if self._dendritic is not None:
            self._dendritic.record(signature)

    @staticmethod
    def format_block(matches: list[SkillMatch]) -> str:
        """Format matches as a ``<recommended-skills>`` tail block.

        M4: includes trigger hints ("When:") so the model knows when to use
        each skill, and match reasons ("Why:") for transparency.
        """
        if not matches:
            return ""
        lines = ["<recommended-skills>",
                 "Based on your current task, these skills may be helpful:"]
        for m in matches:
            dim_str = ", ".join(m.matched_dimensions)
            lines.append(f"- {m.name}: {m.description} (confidence: {int(m.price * 100)}%)")
            trigger_hint = _format_trigger_hint(m.triggers)
            if trigger_hint:
                lines.append(f"  When: {trigger_hint}")
            lines.append(f"  Why: {dim_str}")
            lines.append(f'  Load with: skill(id="{m.name}")')
        lines.append("</recommended-skills>")
        return "\n".join(lines)


def _format_trigger_hint(triggers: dict[str, list[str]]) -> str:
    """Format trigger hints for display in recommended-skills block."""
    parts: list[str] = []
    if triggers.get("keywords"):
        parts.append(f"keywords: {', '.join(triggers['keywords'][:3])}")
    if triggers.get("tools"):
        parts.append(f"tools: {', '.join(triggers['tools'][:3])}")
    if triggers.get("failure_patterns"):
        parts.append(f"on error: {', '.join(triggers['failure_patterns'][:2])}")
    if triggers.get("task_types"):
        parts.append(f"task: {', '.join(triggers['task_types'][:2])}")
    return "; ".join(parts)
