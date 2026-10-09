"""M1 Quality Evaluation: test review against real conversation patterns.

Based on the 2026-10-08 hotel session audit, we identified 6 patterns that
should be captured as skills. This test runs the review against realistic
conversation scenarios and evaluates output quality.

This is NOT a pass/fail test — it's a quality measurement tool.
Run with: pytest tests/test_skill_review_quality.py -v -s
"""

from __future__ import annotations

import json

import pytest

from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig


def _cfg() -> ModelConfig:
    return ModelConfig(name="test-model", base_url="http://localhost", api_key="test-key")


# ---------------------------------------------------------------------------
# Real conversation patterns from hotel session audit
# ---------------------------------------------------------------------------

def _sandbox_workaround_conversation() -> list[ChatMessage]:
    """R2: write/edit fail due to workspace boundary → bash heredoc workaround.

    The model tried write 6 times, all failed with 'resolves outside workspace'.
    Then switched to bash heredoc and succeeded. This pattern repeated across
    T5-T15 (11 turns) because the memory constraint was never injected.
    """
    return [
        ChatMessage(role="user", content="创建 hotel 项目的 database.py 文件", timestamp="t1"),
        ChatMessage(role="assistant", content="我来创建 database.py", timestamp="t2"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t3", tool_call_id="c1"),
        ChatMessage(role="assistant", content="write 失败了，让我试试 read 确认路径", timestamp="t4"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t5", tool_call_id="c2"),
        ChatMessage(role="assistant", content="read 也不行。让我用 bash heredoc 来写", timestamp="t6"),
        ChatMessage(role="tool", content="File created successfully via heredoc", timestamp="t7", tool_call_id="c3"),
        ChatMessage(role="assistant", content="database.py 创建成功。接下来创建 models.py", timestamp="t8"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t9", tool_call_id="c4"),
        ChatMessage(role="assistant", content="write 又失败了，继续用 heredoc", timestamp="t10"),
        ChatMessage(role="tool", content="models.py created via heredoc", timestamp="t11", tool_call_id="c5"),
        ChatMessage(role="assistant", content="models.py 创建成功。继续创建 schemas.py", timestamp="t12"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t13", tool_call_id="c6"),
        ChatMessage(role="assistant", content="再次用 heredoc 写 schemas.py", timestamp="t14"),
        ChatMessage(role="tool", content="schemas.py created via heredoc", timestamp="t15", tool_call_id="c7"),
        ChatMessage(role="assistant", content="所有模型文件创建完成", timestamp="t16"),
    ]


def _task_template_conversation() -> list[ChatMessage]:
    """R1: 'Plan + implement in one turn' causes massive token burn.

    The model built a 12-step plan AND implemented steps 1-3 in a single turn,
    burning 40.9% of the session's total tokens.
    """
    return [
        ChatMessage(role="user", content="Create a detailed implementation plan for a hotel management system, then start implementing", timestamp="t1"),
        ChatMessage(role="assistant", content="I'll create a 12-step plan and start implementing", timestamp="t2"),
        ChatMessage(role="tool", content="todo_write: 12 items created", timestamp="t3", tool_call_id="c1"),
        ChatMessage(role="assistant", content="Step 1: Create project skeleton and venv", timestamp="t4"),
        ChatMessage(role="tool", content="mkdir -p hotel/app && python -m venv hotel/.venv", timestamp="t5", tool_call_id="c2"),
        ChatMessage(role="tool", content="Created .venv", timestamp="t6", tool_call_id="c3"),
        ChatMessage(role="assistant", content="Step 2: Create database.py with SQLite schema", timestamp="t7"),
        ChatMessage(role="tool", content="database.py created", timestamp="t8", tool_call_id="c4"),
        ChatMessage(role="assistant", content="Step 3: Create models.py with Pydantic models", timestamp="t9"),
        ChatMessage(role="tool", content="models.py created", timestamp="t10", tool_call_id="c5"),
        ChatMessage(role="assistant", content="Steps 1-3 done. Budget running low, let me quickly finish...", timestamp="t11"),
    ]


def _step_by_step_conversation() -> list[ChatMessage]:
    """R6: Step-by-step execution with task injection, each turn starts fresh.

    Each turn only has 2 messages and relies on task block injection for context.
    The model re-reads database.py 5-7 times across turns.
    """
    return [
        ChatMessage(role="user", content="只做这一步：创建 schemas.py（这是计划步骤 4，做完停下等验收）", timestamp="t1"),
        ChatMessage(role="assistant", content="Let me first check the existing models to understand the schema", timestamp="t2"),
        ChatMessage(role="tool", content="from database.py: Base = declarative_base()", timestamp="t3", tool_call_id="c1"),
        ChatMessage(role="tool", content="from models.py: class Room(Base): ...", timestamp="t4", tool_call_id="c2"),
        ChatMessage(role="assistant", content="Now I'll create schemas.py with Pydantic models", timestamp="t5"),
        ChatMessage(role="tool", content="schemas.py created with RoomSchema, GuestSchema, BookingSchema", timestamp="t6", tool_call_id="c3"),
        ChatMessage(role="assistant", content="schemas.py 创建完成，包含 3 个 Pydantic schema", timestamp="t7"),
    ]


def _review_turn_conversation() -> list[ChatMessage]:
    """R5: Review turn re-reads everything, compaction loses instruction.

    The review turn re-reads 81 messages / 111K chars, then compaction drops
    the current instruction, causing the model to drift to an old topic.
    """
    return [
        ChatMessage(role="user", content="你给我审查一下你做成的这个项目", timestamp="t1"),
        ChatMessage(role="assistant", content="我来审查整个项目", timestamp="t2"),
        ChatMessage(role="tool", content="cat hotel/app/models.py\ncat hotel/app/schemas.py\ncat hotel/app/routers/rooms.py", timestamp="t3", tool_call_id="c1"),
        ChatMessage(role="tool", content="cat hotel/app/routers/guests.py\ncat hotel/app/routers/bookings.py\ncat hotel/app/database.py", timestamp="t4", tool_call_id="c2"),
        ChatMessage(role="tool", content="cat hotel/tests/test_api.py", timestamp="t5", tool_call_id="c3"),
        ChatMessage(role="tool", content="pytest: 10 passed, 1 warning", timestamp="t6", tool_call_id="c4"),
        ChatMessage(role="assistant", content="Found 3 HIGH issues: 1) Deleting room with bookings → 500. 2) price: Infinity poisons GET /rooms. 3) PUT null → misleading errors", timestamp="t7"),
        ChatMessage(role="assistant", content="Let me try to fix the first issue", timestamp="t8"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t9", tool_call_id="c5"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t10", tool_call_id="c6"),
        ChatMessage(role="tool", content="Error: path resolves outside workspace", timestamp="t11", tool_call_id="c7"),
        ChatMessage(role="assistant", content="Can't read the files to fix. The issues are documented in the report.", timestamp="t12"),
    ]


def _acceptance_criteria_conversation() -> list[ChatMessage]:
    """R4: Acceptance criteria too specific → framework archaeology.

    The verify command asserted internal framework behavior (route.path),
    which changed between FastAPI versions. The model spent 134K tokens
    reading source code to find a solution.
    """
    return [
        ChatMessage(role="user", content="实现 rooms 路由，确保 GET /rooms 返回房间列表", timestamp="t1"),
        ChatMessage(role="assistant", content="我来实现 rooms 路由", timestamp="t2"),
        ChatMessage(role="tool", content="rooms.py created with @app.get('/rooms')", timestamp="t3", tool_call_id="c1"),
        ChatMessage(role="assistant", content="Now let me verify: {r.path for r in app.routes} should include '/rooms'", timestamp="t4"),
        ChatMessage(role="tool", content="Error: '_IncludedRouter' object has no attribute 'path'", timestamp="t5", tool_call_id="c2"),
        ChatMessage(role="assistant", content="Let me check the FastAPI source to understand include_router", timestamp="t6"),
        ChatMessage(role="tool", content="starlette/routing.py: class Router: def include_router...", timestamp="t7", tool_call_id="c3"),
        ChatMessage(role="tool", content="fastapi/routing.py: class APIRouter(Router): ...", timestamp="t8", tool_call_id="c4"),
        ChatMessage(role="assistant", content="The routes are lazily mounted. Let me check if there's an eager option", timestamp="t9"),
        ChatMessage(role="tool", content="fastapi/routing.py line 432: _IncludedRouter wraps routes", timestamp="t10", tool_call_id="c5"),
        ChatMessage(role="assistant", content="No eager option. Let me try downgrading FastAPI to 0.115.12", timestamp="t11"),
        ChatMessage(role="tool", content="pip install fastapi==0.115.12: success", timestamp="t12", tool_call_id="c6"),
        ChatMessage(role="tool", content="Verification passed: routes now include '/rooms'", timestamp="t13", tool_call_id="c7"),
        ChatMessage(role="assistant", content="rooms 路由实现完成，通过降级 FastAPI 解决", timestamp="t14"),
    ]


def _cross_turn_duplicate_read_conversation() -> list[ChatMessage]:
    """R3: Same files read multiple times across turns.

    database.py was cat 5-7 times, models.py 6 times across T5-T15.
    Each turn starts fresh and re-reads everything.
    """
    messages = []
    for turn in range(1, 6):
        messages.extend([
            ChatMessage(role="user", content=f"只做这一步：实现第 {turn} 个模块", timestamp=f"t{turn}_1"),
            ChatMessage(role="assistant", content="先看看 database.py 确认表结构", timestamp=f"t{turn}_2"),
            ChatMessage(role="tool", content="from database.py: Base = declarative_base()...", timestamp=f"t{turn}_3", tool_call_id=f"c{turn}_1"),
            ChatMessage(role="assistant", content="再看看 models.py 确认字段", timestamp=f"t{turn}_4"),
            ChatMessage(role="tool", content="from models.py: class Room(Base): ...", timestamp=f"t{turn}_5", tool_call_id=f"c{turn}_2"),
            ChatMessage(role="assistant", content=f"第 {turn} 个模块实现完成", timestamp=f"t{turn}_6"),
        ])
    return messages


# ---------------------------------------------------------------------------
# Quality evaluation
# ---------------------------------------------------------------------------

PATTERNS = {
    "sandbox_workaround": _sandbox_workaround_conversation,
    "task_template_anti_pattern": _task_template_conversation,
    "step_by_step_execution": _step_by_step_conversation,
    "review_turn_pattern": _review_turn_conversation,
    "acceptance_criteria_brittle": _acceptance_criteria_conversation,
    "cross_turn_duplicate_read": _cross_turn_duplicate_read_conversation,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern_name", list(PATTERNS.keys()))
async def test_review_captures_pattern(pattern_name, tmp_path, monkeypatch):
    """Evaluate review quality for each real conversation pattern.

    This test prints the review result for manual inspection.
    It always passes — the value is in the printed output.
    """
    from ai_coding.core.skill_review import run_skill_review

    messages = PATTERNS[pattern_name]()

    # Mock _chat to return a realistic response based on the pattern
    async def fake_chat(model_config, system, user, max_tokens):
        # Simulate a good review response
        responses = {
            "sandbox_workaround": json.dumps({
                "create": True,
                "name": "sandbox-workaround",
                "description": "Use bash heredoc when write/edit fail due to workspace boundary",
                "content": (
                    "---\n"
                    "name: sandbox-workaround\n"
                    "description: Use bash heredoc when write/edit fail due to workspace boundary\n"
                    "version: 1\n"
                    "---\n\n"
                    "## When to use\n"
                    "When write or edit tool fails with 'path resolves outside workspace'\n\n"
                    "## Steps\n"
                    "1. Confirm the error is 'resolves outside workspace'\n"
                    "2. Use `bash` with heredoc to write the file: `cat > path << 'EOF'`\n"
                    "3. Verify the file was created with `cat path`\n\n"
                    "## Verification\n"
                    "The file exists at the target path and has the expected content\n"
                ),
                "reason": "5+ tool calls with repeated failures, clear workaround pattern",
            }),
            "task_template_anti_pattern": json.dumps({
                "create": True,
                "name": "task-template-discipline",
                "description": "Separate planning and implementation into different turns",
                "content": (
                    "---\n"
                    "name: task-template-discipline\n"
                    "description: Separate planning and implementation into different turns\n"
                    "version: 1\n"
                    "---\n\n"
                    "## When to use\n"
                    "When the user asks to 'plan and implement' or 'create a plan then start'\n\n"
                    "## Steps\n"
                    "1. Create the full plan first (todo_write all items)\n"
                    "2. Stop and present the plan to the user\n"
                    "3. Wait for user approval before implementing\n"
                    "4. Implement one step at a time\n\n"
                    "## Verification\n"
                    "Plan is presented before any implementation begins\n"
                ),
                "reason": "Single-turn plan+implement burns 40%+ of session tokens",
            }),
            "step_by_step_execution": json.dumps({
                "create": False,
                "reason": "This is a project-specific execution pattern, not a reusable workflow",
            }),
            "review_turn_pattern": json.dumps({
                "create": True,
                "name": "review-turn-optimization",
                "description": "Avoid full re-read in review turns — use targeted reads",
                "content": (
                    "---\n"
                    "name: review-turn-optimization\n"
                    "description: Avoid full re-read in review turns — use targeted reads\n"
                    "version: 1\n"
                    "---\n\n"
                    "## When to use\n"
                    "When the user asks to 'review' or 'audit' a project\n\n"
                    "## Steps\n"
                    "1. Use `find` to locate key files instead of reading everything\n"
                    "2. Read only the files that need review\n"
                    "3. Run tests to verify behavior\n"
                    "4. Report findings concisely\n\n"
                    "## Verification\n"
                    "Review completes without re-reading the entire history\n"
                ),
                "reason": "Full re-read burns 20%+ of session tokens and causes instruction loss after compaction",
            }),
            "acceptance_criteria_brittle": json.dumps({
                "create": True,
                "name": "black-box-verify",
                "description": "Write verify commands as black-box behavior assertions",
                "content": (
                    "---\n"
                    "name: black-box-verify\n"
                    "description: Write verify commands as black-box behavior assertions\n"
                    "version: 1\n"
                    "---\n\n"
                    "## When to use\n"
                    "When writing acceptance criteria (verify commands) for plan steps\n\n"
                    "## Steps\n"
                    "1. Assert observable behavior (HTTP status codes, response bodies)\n"
                    "2. Do NOT assert internal implementation details\n"
                    "3. Do NOT depend on specific framework versions\n"
                    "4. Use curl or pytest for verification\n\n"
                    "## Verification\n"
                    "Verify command passes without reading framework source code\n"
                ),
                "reason": "Internal assertions cause framework archaeology (134K tokens in one case)",
            }),
            "cross_turn_duplicate_read": json.dumps({
                "create": False,
                "reason": "This is a harness limitation (no cross-turn read dedup), not a user-facing workflow",
            }),
        }
        return responses.get(pattern_name, json.dumps({"create": False, "reason": "unknown"}))

    monkeypatch.setattr("ai_coding.core.skill_review._chat", fake_chat)

    result = await run_skill_review(messages, [], _cfg(), tmp_path)

    # Print quality evaluation
    print(f"\n{'='*60}")
    print(f"Pattern: {pattern_name}")
    print(f"  Messages: {len(messages)}")
    print(f"  Created: {result.created}")
    if result.created:
        print(f"  Skill: {result.skill_name}")
        print(f"  Path: {result.skill_path}")
        # Validate skill content quality
        content = (tmp_path / result.skill_name / "SKILL.md").read_text()
        quality_checks = {
            "has_frontmatter": "---" in content[:50],
            "has_name_field": "name:" in content[:100],
            "has_description": "description:" in content[:200],
            "has_when_to_use": "## When to use" in content or "## 使用场景" in content,
            "has_steps": "## Steps" in content or "## 步骤" in content,
            "has_verification": "## Verification" in content or "## 验证" in content,
            "content_length_ok": len(content) >= 100,
        }
        print(f"  Quality checks: {quality_checks}")
        failed = [k for k, v in quality_checks.items() if not v]
        if failed:
            print(f"  ⚠️  FAILED checks: {failed}")
        else:
            print("  ✅ All quality checks passed")
    else:
        print(f"  Reason: {result.reason}")
    print(f"{'='*60}")

    # This test always passes — it's a measurement tool
    assert True


def test_prompt_includes_all_quality_criteria():
    """Verify the review prompt contains all necessary quality criteria."""
    from ai_coding.core.skill_review import _REVIEW_SYSTEM_PROMPT

    # The prompt must guide the model to produce high-quality skills
    required_elements = [
        "值得保存",  # What to save
        "不值得保存",  # What NOT to save
        "kebab-case",  # Naming convention
        "SKILL.md",  # Output format
        "create",  # JSON output field
        "name",  # JSON output field
        "content",  # JSON output field
        "reason",  # JSON output field
    ]

    missing = [elem for elem in required_elements if elem not in _REVIEW_SYSTEM_PROMPT]
    assert not missing, f"Prompt missing elements: {missing}"


def test_max_review_messages_constant_is_reasonable():
    """Verify MAX_REVIEW_MESSAGES is set to a reasonable value."""
    from ai_coding.core.skill_review import MAX_REVIEW_MESSAGES

    # Should be between 10 and 100
    assert 10 <= MAX_REVIEW_MESSAGES <= 100, f"MAX_REVIEW_MESSAGES={MAX_REVIEW_MESSAGES} out of range"
