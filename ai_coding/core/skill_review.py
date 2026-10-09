"""Background skill review (M1).

Reviews conversation deltas after every N turns and optionally creates
a skill file. Best-effort: failures are logged and dropped.

Design (m1-analysis §3): after ``skill_review_interval`` turns,
:func:`run_skill_review` is called with the conversation delta (messages
since the last review) and the list of existing skill names.  If the model
judges there is a reusable workflow, a ``SKILL.md`` is written to the skills
directory atomically.  The review runs as a fire-and-forget asyncio task
after the user has received its response.

Quality improvements (M1-Q):
- Prompt includes concrete positive/negative examples
- ``_validate_skill_content`` enforces SKILL.md structure before writing
- ``_filter_messages`` removes noise (exit codes, blanks, very short)
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_coding.ai.llm_helpers import _chat
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.model_config import ModelConfig

_log = logging.getLogger(__name__)

MAX_REVIEW_MESSAGES = 30  #: Cap on messages sent to the review LLM per call.
MIN_CONTENT_LENGTH = 100  #: Minimum chars for a valid SKILL.md.
MIN_MESSAGE_CONTENT = 2  #: Messages shorter than this are noise.

# Frontmatter patterns (same style as skills/registry.py)
_FRONTMATTER_BLOCK = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FM_NAME = re.compile(r"(?im)^name\s*:\s*(.+)$")
_FM_DESC = re.compile(r"(?im)^description\s*:\s*(.+)$")

# Section markers (Chinese or English)
_WHEN_PATTERNS = ["## 使用场景", "## When to use", "## 何时使用", "## Trigger"]
_STEP_PATTERNS = ["## 步骤", "## Steps", "## 流程", "## Procedure"]
_VERIFY_PATTERNS = ["## 验证", "## Verification", "## 检查", "## Check"]


_REVIEW_SYSTEM_PROMPT = (
    "你是技能提炼助手。审查以下对话中是否有值得保存为 skill 的可复用工作流。\n\n"
    "## 值得保存的 skill（例如）\n"
    "- 工作区沙箱越界 → 改用 bash heredoc 写文件（5+ 次失败后找到的 workaround）\n"
    "- FastAPI 路由验证 → 框架版本不兼容 → 降级解决（框架考古模式）\n"
    "- 删房 500 错误 → 外键约束 → 需要先查关联预订（数据库约束排查）\n\n"
    "## 不值得保存（例如）\n"
    "- 1-2 步的简单任务（如「写一个 hello.py」）\n"
    "- 已在记忆中的信息（用户偏好、项目约定、环境约束）\n"
    "- 模型已有的通用编程知识（如「Python 的 list 用法」）\n"
    "- 纯项目特定的执行步骤（如「实现 hotel 项目的第 4 步」）\n"
    "- 不太可能重复的一次性操作\n\n"
    "已有 skill（不要重复创建）：{existing_skills}\n\n"
    "## 输出格式\n"
    "只输出 JSON，不要输出任何其他文字：\n"
    + '{{"create": true/false, '
    + '"name": "kebab-case-name", '
    + '"description": "一行描述（不超过 80 字）", '
    + '"content": "完整的 SKILL.md 内容（见下方格式）", '
    + '"reason": "为什么值得保存（不超过 50 字）"}}\n\n'
    + "## content 格式要求\n"
    + "content 必须是完整的 SKILL.md，结构如下：\n"
    + "---\n"
    + "name: kebab-case-name\n"
    + "description: 一行描述\n"
    + "version: 1\n"
    + "---\n\n"
    + "## 使用场景\n"
    + "描述何时使用此 skill（至少 2 行）\n\n"
    + "## 步骤\n"
    + "1. 第一步（具体可执行）\n"
    + "2. 第二步\n"
    + "3. ...\n\n"
    + "## 验证\n"
    + "如何确认 skill 执行成功（至少 1 行）\n\n"
    + "## 约束\n"
    + "- name 必须是 kebab-case（小写字母、数字、连字符），2-4 个词\n"
    + "- content 至少 100 字符\n"
    + "- 使用场景、步骤、验证三个章节必须齐全\n"
    + "- 步骤必须具体可执行，不能是空泛的建议\n"
)


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of a background skill review."""

    created: bool
    skill_name: str = ""
    skill_path: str = ""
    reason: str = ""


def _messages_to_text(messages: Sequence[ChatMessage]) -> str:
    """Format messages for the review prompt.  Blank content is skipped."""
    parts: list[str] = []
    for m in messages:
        body = (m.content or "").strip()
        if body:
            parts.append(f"[{m.role}] {body}")
    return "\n".join(parts)


def _filter_messages(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    """Filter noise messages before review.

    Removes:
    - Messages with very short content (less than MIN_MESSAGE_CONTENT chars)
    - Tool results that are only exit codes or status lines
    - Blank messages
    """
    filtered: list[ChatMessage] = []
    for m in messages:
        body = (m.content or "").strip()
        if not body:
            continue
        if len(body) < MIN_MESSAGE_CONTENT:
            continue
        # Filter pure tool noise (exit codes, status lines)
        if m.role == "tool":
            # Keep tool results that have actual content (code, errors, data)
            if _is_tool_noise(body):
                continue
        filtered.append(m)
    return filtered


def _is_tool_noise(body: str) -> bool:
    """Check if a tool result is pure noise (exit codes, empty status)."""
    noise_patterns = [
        r"^Exit code:\s*\d+$",
        r"^exit code:\s*\d+$",
        r"^\s*$\n",
        r"^Success$",
        r"^success$",
        r"^Done$",
        r"^done$",
        r"^OK$",
        r"^ok$",
        r"^无输出$",
        r"^没有输出$",
    ]
    for pattern in noise_patterns:
        if re.match(pattern, body, re.MULTILINE):
            return True
    return False


def _validate_skill_content(content: str) -> tuple[bool, str]:
    """Validate SKILL.md content has proper structure.

    Returns (is_valid, reason).
    """
    if not content or len(content) < MIN_CONTENT_LENGTH:
        return False, f"content too short ({len(content)} < {MIN_CONTENT_LENGTH})"

    # Check frontmatter
    fm_match = _FRONTMATTER_BLOCK.search(content)
    if not fm_match:
        return False, "missing frontmatter (---...---)"

    fm_body = fm_match.group(1)

    # Check name field
    if not _FM_NAME.search(fm_body):
        return False, "frontmatter missing 'name' field"

    # Check description field
    if not _FM_DESC.search(fm_body):
        return False, "frontmatter missing 'description' field"

    # Check required sections
    rest = content[fm_match.end():]

    has_when = any(p in rest for p in _WHEN_PATTERNS)
    if not has_when:
        return False, "missing '使用场景' / 'When to use' section"

    has_steps = any(p in rest for p in _STEP_PATTERNS)
    if not has_steps:
        return False, "missing '步骤' / 'Steps' section"

    has_verify = any(p in rest for p in _VERIFY_PATTERNS)
    if not has_verify:
        return False, "missing '验证' / 'Verification' section"

    return True, ""


def _parse_review_response(raw: str) -> dict[str, Any] | None:
    """Parse JSON from model response.  Tolerant of fenced code blocks."""
    if not raw:
        return None
    text = raw.strip()
    # Strip fenced code blocks (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        lines = text.split("\n")
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


async def run_skill_review(
    messages: Sequence[ChatMessage],
    existing_skills: list[str],
    model_config: ModelConfig,
    skill_dir: Path,
) -> ReviewResult:
    """Review a conversation delta and optionally create a skill file.

    Best-effort: any failure degrades to ``ReviewResult(created=False)``.
    """
    if not messages:
        return ReviewResult(created=False, reason="no messages")

    # Filter noise messages
    filtered = _filter_messages(messages)
    if not filtered:
        return ReviewResult(created=False, reason="all messages filtered as noise")

    # Cap to last N messages
    delta = filtered[-MAX_REVIEW_MESSAGES:]
    conversation_text = _messages_to_text(delta)
    if not conversation_text:
        return ReviewResult(created=False, reason="empty conversation")

    prompt = _REVIEW_SYSTEM_PROMPT.format(
        existing_skills=", ".join(existing_skills) if existing_skills else "(none)"
    )
    user_text = f"请审查以下对话：\n\n{conversation_text}"

    try:
        raw = await _chat(
            model_config,
            prompt,
            user_text,
            max_tokens=2048,
        )
    except Exception as e:  # noqa: BLE001 — best-effort
        _log.debug("review LLM call failed: %s", e)
        return ReviewResult(created=False, reason=f"LLM error: {e}")

    data = _parse_review_response(raw)
    if not data:
        return ReviewResult(created=False, reason="unparseable response")

    if not data.get("create", False):
        return ReviewResult(
            created=False, reason=data.get("reason", "model chose not to")
        )

    name = data.get("name", "")
    content = data.get("content", "")
    reason = data.get("reason", "")

    # Validate name
    if not name or "/" in name or ".." in name or len(name) > 80:
        return ReviewResult(created=False, reason=f"invalid skill name: {name}")

    # Check for duplicates
    if name in existing_skills:
        return ReviewResult(
            created=False, reason=f"skill '{name}' already exists"
        )

    # Validate content structure
    is_valid, validation_reason = _validate_skill_content(content)
    if not is_valid:
        return ReviewResult(
            created=False, reason=f"content validation failed: {validation_reason}"
        )

    # Write skill file atomically (same pattern as MemoryStore._save)
    try:
        target = skill_dir / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        _log.info("skill created: %s (%d chars)", name, len(content))
        return ReviewResult(
            created=True,
            skill_name=name,
            skill_path=str(target),
            reason=reason,
        )
    except OSError as e:  # noqa: BLE001 — best-effort
        _log.debug("skill write failed: %s", e)
        return ReviewResult(created=False, reason=f"write error: {e}")
