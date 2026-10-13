# r16 — Skill 系统对齐（E-1..E-7）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 skill 系统的七个缺口按已拍板方案补齐：Home 面板可种入 `@name`、composer 支持 `@` 自动补全、@mention 直接注入 skill 全文（OC 语义，volatile 提及轮）、`disable-model-invocation` 标准字段生效、per-skill 权限（allow/ask/deny）、skill 读取输出带 base dir + 文件清单、workspace 级 `.claude/.agents` 兼容根。

**Architecture:** 后端保持"一个 registry → 一个渲染器 → 三个消费点（catalog/tool/mention）"；新增 `skills/render.py`（唯一的内容渲染）与 `skills/policy.py`（唯一的权限归一化）。前端的 `dialog.skills.show` 从 app 层下沉到两个路由层（谁有 composer 谁注册），`@` 补全复用 composer 现有建议列表机制 + 一个新的纯函数模块 `util/mention.ts`。

**Tech Stack:** Python 3.10+ / pydantic / pytest；bun + SolidJS + @opentui/keymap 0.1.x / fuzzysort。

**Spec / 设计依据:**
- `docs/data-contracts.md`（R11c skill 契约 689-731 行、M9 作用域契约 762-790 行）
- `docs/architecture.md`（skills 行 52、启动链 80-90 行）
- 调研报告：本对话（OC `packages/core/src/skill.ts`、`tool/plugin/skill.ts`、`skill/instructions.ts`；我方全链路核对）
- **决策记录（本轮拍板）**：E-3=仅提及轮 volatile；E-5=ask 只拦模型自主调用（deny 三处全拦，alias 归一）；E-7=仅 workspace 级兼容根。

## 全局约束

- TDD/RIPR：每个任务 **Red（先写失败用例并亲眼看到失败）→ Implement → Pass（跑用例）→ Refactor（仅在不改变行为时）**，缺一不可。
- 最小化、第一性原理：只触碰与目标直接相关的行；不新增抽象除非有 ≥2 个真实调用点（`render.py`、`policy.py` 各有 2-3 个调用点，故成立）。
- 无新依赖（fuzzysort 已在 `frontend/package.json`）。
- 门禁：`mypy ai_coding` 必须干净；`ruff check ai_coding tests` 相对基线（87 errors）零增量；`bun test` + `bun run typecheck` 全绿。
- pytest 基线：`tests/test_loop_digest.py` 2 红（预存）、`tests/test_mcp_lifecycle.py::test_the_pause_grows_and_stays_under_the_cap` flaky（预存）——除此外零新增失败。
- 不改动 `tests/` 中与本计划无关的断言；**允许且必须**更新的既有测试仅限：`test_tool_skill.py:98`（read 输出契约变更）、`test_skill_interop.py:119-130`（`disable-model-invocation` 从 extra 升为受控字段）——两处都是被批准的契约变更，计划中已逐一列出。
- 前置：先把上一轮（compaction P3-4）的未提交改动提交；否则每个任务的 `git add <本任务文件>` 会混入无关改动。若选择不提交，跳过各任务的 commit 步骤，但不要用 `git add -A`。
- 文件级优先级：后端任务先于 T4/T6 对 `request_assembly.py` 的两次编辑按 T3→T6 顺序串行。

---

## 1. 影响分析（现状 → 目标）

| 点 | 触达文件 | 复用的现有契约 | 数据流变化 |
|---|---|---|---|
| E-1 Home 种入 | `frontend/src/app.tsx`（删注册）、`routes/home.tsx`（加注册+seed+client prop）、`component/dialog-skills.tsx`（onSelect 必填）、`routes/session.tsx`（注释） | `Prompt` 的 `seed/onSeedDone`（`prompt.tsx:109-118`）；`DialogSkills.onSelect`；keymap `useBindings`（后者注册者胜出） | 命令注册从"app 层只读兜底"改为"路由层各注册一条带 onSelect 的"；palette 重复行随双注册同时消失 |
| E-2 @ 补全 | 新 `util/mention.ts`、`component/prompt.tsx`、`app.tsx`（skills 拉取）、`routes/home.tsx`+`routes/session.tsx`（透传） | `slashQuery/filterSlashCommands` 的算法形状；`skills.list` IPC（`handlers.py:471`）；`resolve_mentions` 的后端解析（`registry.py:428`） | composer 建议列表从"仅 /"扩展为 "/ 或 @"；选中 @ 项把 `@token` 原位替换为 `@name ` |
| E-3 全文注入 | `core/request_assembly.py:234-242` | `resolve_mentions`；`volatile_block("recommended_skills")`；新 `render_skill_content` | 提及轮内每迭代注入 `<requested-skills>`+`<skill_content>` 块（尾部），下一 user 消息后自然消失；模型不再需要先调 tool read |
| E-4 disable-model-invocation | `skills/registry.py`、`skills/catalog.py:107`、`core/sop_router.py` 3 处 | `is_archived` 的既有过滤形状；`_KNOWN_KEYS`/extra 机制 | catalog 与 SOP 路由的候选集条件从 `not is_archived` 变为 `is_auto_invocable`；读取（tool/mention）不受影响 |
| E-5 per-skill 权限 | `config/models.py`、`config.example.yaml`、新 `skills/policy.py`、`security/permission_gate.py`、`cli.py:164`、`core/subagent.py:481`、`core/agent_loop.py:1230`、`request_assembly.py`（T3 的 helper） | dispatcher 的 WARN→approval 链（`dispatch.py:349-361`）；`from_config` 策略读取点；`AutoApproveCallback` 的子代理语义 | 模型自主 `skill(read)` 过闸：deny→DENY、ask→WARN(面板)；catalog/mention 侧用 `denied_skill_ids` 隐藏/拒注；`list`/mention 不受 ask 影响 |
| E-6 读取输出 | 新 `skills/render.py`、`tools/skill_tool.py` | `Skill.body/path/version/status`；`_suffix` 现有逻辑（迁入 render） | read 输出变为 `<skill_content>` 包裹（base dir + ≤10 文件清单）；list 输出不变 |
| E-7 兼容根 | `paths.py:173`、`skills/registry.py:337`（shadowed 标签）、`server/world.py:99`（警告文案） | `SkillRegistry(roots)` 多根"后胜"；`shadowed()` | 扫描根 2→4：user < ws/.claude < ws/.agents < ws/skills；冲突警告标签改为真实路径 |

## 2. 目标数据流（架构图）

```
                     ┌─────────────────────────── TUI (bun/Solid) ───────────────────────────┐
                     │                                                                        │
   /skills 面板       │  Home 路由 (T4)   Session 路由                                      │
   (E-1)             │   dialog.skills.show 注册（带 onSelect，T4 后唯一）                     │
                     │        │ setPromptSeed("@name ")                                      │
                     │        ▼                                                              │
   @ 自动补全 (E-2)   │  Prompt(seed / skills=[]) ──@token──► replaceMentionToken ──► "@name " │
                     │        │ onSubmit(text)                                                │
                     └────────┼───────────────────────────────────────────────────────────────┘
                              ▼ session.prompt {text}
┌──────────────────────── AgentLoop / RequestAssembler (T3, T6) ─────────────────────────────┐
│  user_input ──resolve_mentions(registry)──► [id, ...]          ← alias/id 两种拼写          │
│       │                                                                                     │
│       ├─ E-5 deny? ── policy.denied_skill_ids ──► "已被配置禁止"（不注入）                    │
│       ├─ healthy? ── render.render_skill_content (T1) ──► <skill_content>…</skill_content>  │
│       └─ broken?  ──► "- name: 无法加载（err）"                                               │
│                     ▼                                                                       │
│   volatile tail: volatile_block("recommended_skills", 推荐块 + <requested-skills>块)          │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
┌─────────────── 模型自主路径（E-4/E-5 生效于此） ───────────────┐
│  system prompt <skills> 目录 = build_catalog(filter(is_auto_invocable & !deny)) │
│  模型 ── skill(read, name) ──► PermissionGate.check:                          │
│          deny→DENY │ ask→WARN→(approval 面板) │ allow→执行                    │
│          allow 执行 ──► render_skill_content (T1，与 mention 同一形状)          │
└───────────────────────────────────────────────────────────────────────────────┘
发现层 (T7)：SkillRegistry 根 = [~/.local/share/ai-coding/skills, <ws>/.claude/skills,
              <ws>/.agents/skills, <ws>/skills]  —— 后者覆盖前者，shadowed() 报告冲突
```

## 3. 任务

### Task 1 — E-6：`skills/render.py` 唯一渲染器 + tool read 换形

**Files:**
- Create: `ai_coding/skills/render.py`
- Create: `tests/test_skill_render.py`
- Modify: `ai_coding/tools/skill_tool.py:73-87,138-146`
- Modify: `tests/test_tool_skill.py:88-99`（契约变更，被批准）
- Modify: `docs/data-contracts.md`（R11c 段落 `skill list / skill read` 行 728）

**Interfaces:**
- Produces: `render_skill_content(skill: Skill, *, limit: int = MAX_SKILL_FILES) -> str`；`skill_files(skill, *, limit=MAX_SKILL_FILES) -> list[str]`；`skill_suffix(skill) -> str`；`MAX_SKILL_FILES = 10`。T3 依赖 `render_skill_content`。

- [ ] **Step 1 (Red)：写失败用例**

`tests/test_skill_render.py`：

```python
"""TDD (r16/E-6): what a loaded skill looks like to the model.

One renderer for the tool's read and the @mention injection — two formatters
would drift the day one of them learns about files or status markers. The
format mirrors OpenCode's `Skill.prepare`/`toModelOutput`
(packages/core/src/skill.ts:36-65).
"""

from ai_coding.skills.registry import parse_skill
from ai_coding.skills.render import MAX_SKILL_FILES, render_skill_content, skill_files


def _skill(tmp_path, frontmatter="description: d\n", body="Do the thing.\n", files=()):
    root = tmp_path / "skills" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    (root / "SKILL.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return parse_skill(root / "SKILL.md"), root


def test_the_content_is_wrapped_like_opencodes(tmp_path):
    skill, root = _skill(tmp_path)

    text = render_skill_content(skill)

    assert text.startswith('<skill_content name="demo">')
    assert "# Skill: demo\n\nDo the thing." in text
    assert f"Base directory for this skill: {root}" in text
    assert "Relative paths in this skill" in text
    assert text.endswith("</skill_files>\n</skill_content>")


def test_the_file_list_is_sorted_absolute_and_excludes_skill_md(tmp_path):
    skill, root = _skill(tmp_path, files=["scripts/run.sh", "reference/api.md"])

    files = skill_files(skill)

    assert files == sorted(files)
    assert str(root / "reference" / "api.md") in files
    assert str(root / "scripts" / "run.sh") in files
    assert all(not f.endswith("SKILL.md") for f in files)


def test_the_file_list_is_capped(tmp_path):
    skill, _ = _skill(tmp_path, files=[f"notes/f{i:02d}.md" for i in range(12)])

    assert len(skill_files(skill)) == MAX_SKILL_FILES


def test_a_skill_without_extra_files_still_declares_the_empty_list(tmp_path):
    skill, _ = _skill(tmp_path)

    text = render_skill_content(skill)

    assert "<skill_files>\n</skill_files>" in text


def test_the_status_marker_keeps_its_one_spelling(tmp_path):
    skill, _ = _skill(tmp_path, frontmatter="description: d\nversion: 3\nstatus: archived\n")

    text = render_skill_content(skill)

    assert "# Skill: demo (v3) [archived]" in text
```

- [ ] **Step 2 (Red)：跑并确认失败**

Run: `python -m pytest tests/test_skill_render.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'ai_coding.skills.render'`

- [ ] **Step 3 (Implement)：`ai_coding/skills/render.py`**

```python
"""What a loaded skill looks like to the model — one renderer, two callers.

The skill tool's ``read`` and the @mention injection must hand the model the
same shape; two formatters would drift the day one of them learned about the
base directory or the file list first. Format mirrors OpenCode's
``Skill.prepare``/``toModelOutput`` (packages/core/src/skill.ts:36-65): body,
then where relative paths resolve from, then a sampled file listing.
"""

from __future__ import annotations

from ai_coding.skills.registry import DEFAULT_VERSION, Skill

#: Files sampled into the listing.  OpenCode's scanner uses the same cap
#: (skill.ts:57-63): enough to reveal a scripts/ or reference/ layout, small
#: enough that a skill with a media folder does not spend a request on it.
MAX_SKILL_FILES = 10


def skill_suffix(skill: Skill) -> str:
    """The read/list marker: only what differs from the default is shown.

    A v1 active skill keeps its exact pre-R11c text, so an unversioned
    directory of skills does not suddenly sprout noise.
    """
    out = ""
    if skill.version != DEFAULT_VERSION:
        out += f" (v{skill.version})"
    if skill.is_archived:
        out += " [archived]"
    if not skill.is_healthy:
        out += " [broken]"
    return out


def skill_files(skill: Skill, *, limit: int = MAX_SKILL_FILES) -> list[str]:
    """Absolute paths beside ``SKILL.md``, sorted and capped.

    Recursive and dot-inclusive like OpenCode's scan; ``SKILL.md`` itself is
    excluded because the model already has its body. Best effort: a directory
    that cannot be walked lists what it can.
    """
    root = skill.path.parent
    try:
        found = sorted(
            str(path)
            for path in root.rglob("*")
            if path.is_file() and path.name != "SKILL.md"
        )
    except OSError:
        return []
    return found[:limit]


def render_skill_content(skill: Skill, *, limit: int = MAX_SKILL_FILES) -> str:
    """The full ``<skill_content>`` block: what loading a skill means."""
    files = skill_files(skill, limit=limit)
    return "\n".join(
        [
            f'<skill_content name="{skill.id}">',
            f"# Skill: {skill.id}{skill_suffix(skill)}",
            "",
            skill.body,
            "",
            f"Base directory for this skill: {skill.path.parent}",
            "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
            "Note: file list is sampled.",
            "",
            "<skill_files>",
            *(f"<file>{path}</file>" for path in files),
            "</skill_files>",
            "</skill_content>",
        ]
    )


__all__ = ["MAX_SKILL_FILES", "render_skill_content", "skill_files", "skill_suffix"]
```

- [ ] **Step 4 (Implement)：`skill_tool.py` 换用渲染器**

`ai_coding/tools/skill_tool.py`：
1. 顶部 import 改为 `from ai_coding.skills.render import render_skill_content, skill_suffix`（删除 `DEFAULT_VERSION, Skill` 中不再用的部分：`Skill` 仍被 `list_skills` 的类型注释用？核对后：`_suffix` 被删除后 `Skill` 不再被引用则一并从 import 删掉，只留 `SkillRegistry`）。
2. 删除 `_suffix` 静态方法（第 73-87 行整块），`list` 动作里 `self._suffix(skill)` 改为 `skill_suffix(skill)`。
3. `read` 动作的返回（第 138-146 行）改为：

```python
            return ToolResult(
                success=True,
                output=render_skill_content(skill),
                data={
                    "name": skill.id,
                    "version": skill.version,
                    "status": skill.status,
                },
            )
```

- [ ] **Step 5 (Pass)：更新被批准的契约断言并跑绿**

`tests/test_tool_skill.py:98`：
```python
    assert r.data == {"name": "deploy", "version": "3", "status": "archived"}
    # One marker for both actions: read and list cannot disagree about a skill.
    assert r.output.startswith('<skill_content name="deploy">')
    assert "# Skill: deploy (v3) [archived]" in r.output
```
Run: `python -m pytest tests/test_skill_render.py tests/test_tool_skill.py tests/test_skill_reference.py tests/test_skill_catalog.py tests/test_skill_progressive_disclosure.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6 (Refactor + Docs)：**

`docs/data-contracts.md` 的 `skill list / skill read` 契约行改为：`read` 返回 OpenCode 形状的 `<skill_content>`（正文 + Base directory + ≤10 文件清单，`render.py` 唯一渲染器，mention 注入共用）；list 标记逻辑不变。

- [ ] **Step 7：Commit**

```bash
git add ai_coding/skills/render.py ai_coding/tools/skill_tool.py tests/test_skill_render.py tests/test_tool_skill.py docs/data-contracts.md
git commit -m "feat(skills): one renderer for loaded-skill content (E-6)"
```

---

### Task 2 — E-4：`disable-model-invocation` 生效

**Files:**
- Modify: `ai_coding/skills/registry.py:51-65,85-138,238-255`
- Modify: `ai_coding/skills/catalog.py:103-107`
- Modify: `ai_coding/core/sop_router.py:355,471,525`
- Test: `tests/test_skill_registry.py`、`tests/test_skill_catalog.py`、`tests/test_sop_router.py`、`tests/test_skill_interop.py:119-135`
- Modify: `docs/data-contracts.md`（标准字段契约行 722、archived 语义行 723）

**Interfaces:**
- Produces: `Skill.disable_model_invocation: bool`；`Skill.is_auto_invocable -> bool`。T6 不动此契约。

- [ ] **Step 1 (Red)：三处失败用例**

`tests/test_skill_registry.py` 追加（复用文件已有的 `_parse` helper）：

```python
def test_disable_model_invocation_is_read_from_the_standard_field(tmp_path):
    on = _parse(tmp_path, "a", "---\ndescription: d\ndisable-model-invocation: true\n---\nbody")
    off = _parse(tmp_path, "b", "---\ndescription: d\n---\nbody")
    loose = _parse(tmp_path, "c", "---\ndescription: d\ndisable-model-invocation: 'yes'\n---\nbody")

    assert on.disable_model_invocation is True
    assert off.disable_model_invocation is False
    assert loose.disable_model_invocation is True


def test_a_disabled_skill_is_not_auto_invocable_but_stays_addressable(tmp_path):
    skill = _parse(tmp_path, "a", "---\ndescription: d\ndisable-model-invocation: true\n---\nbody")

    assert skill.is_auto_invocable is False
    assert skill.is_archived is False, "the two lifecycle reasons stay distinct"
```

`tests/test_skill_catalog.py` 追加（复用该文件既有 `_skill(tmp_path, name, description, **frontmatter)` helper；连字符 key 用 `**{...}` 传）：

```python
def test_a_skill_the_author_disabled_is_not_listed(tmp_path):
    quiet = _skill(tmp_path, "quiet", "d", **{"disable-model-invocation": True})
    loud = _skill(tmp_path, "loud", "d")

    text = build_catalog([quiet, loud])

    assert "quiet" not in text
    assert "loud" in text
```

`tests/test_sop_router.py` 追加（用文件顶部既有的 `_write_skill`，frontmatter 一次写全）：

```python
def test_a_disabled_skill_is_not_recommended(tmp_path):
    from ai_coding.core.sop_router import SOPRouter

    _write_skill(
        tmp_path,
        "quiet-fix",
        "name: quiet-fix\ndescription: d\ndisable-model-invocation: true\n"
        "triggers:\n  keywords: ['沙箱']\n",
    )
    router = SOPRouter(SkillRegistry(tmp_path), threshold=1.0)

    assert router.route("沙箱越界了", [], "", []) == []
```

- [ ] **Step 2 (Red)：跑并确认失败**

Run: `python -m pytest tests/test_skill_registry.py tests/test_skill_catalog.py tests/test_sop_router.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'Skill' object has no attribute 'disable_model_invocation'`（catalog/router 用例表现为 skill 仍被列出/推荐）

- [ ] **Step 3 (Implement)：registry.py**

1. `_KNOWN_KEYS` 增加 `"disable-model-invocation",`（放在 `"metadata",` 之后）。
2. 加 helper（放在 `_as_tuple` 之后）：

```python
def _as_bool(value: object) -> bool:
    """A YAML boolean as a bool; the loose spellings an author may write too.

    YAML gives ``true``/``1`` as real booleans; ``'yes'``/``'on'`` arrive as
    strings and mean the same thing to every runtime that reads this field.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "on", "1"}
    return False
```

3. `Skill` 增加字段（放在 `metadata` 之后，注释说明是标准字段）：

```python
    # Agent Skills standard: the author's "do not offer this to the model".
    # Read and acted on (E-4) — the one standard field whose behaviour landed.
    disable_model_invocation: bool = False
```

4. `parse_skill` 的 `Skill(...)` 构造增加：

```python
        disable_model_invocation=_as_bool(data.get("disable-model-invocation")),
```

5. 新增属性（放在 `is_archived` 之后）：

```python
    @property
    def is_auto_invocable(self) -> bool:
        """Whether the catalog/router may offer this skill on their own.

        False for ``status: archived`` (our lifecycle) and for the standard's
        ``disable-model-invocation`` (the author's). Both stay readable by
        name — what they lose is the proactive offer, not the address.
        """
        return not self.is_archived and not self.disable_model_invocation
```

- [ ] **Step 4 (Implement)：catalog.py + sop_router.py**

`catalog.py:107`：
```python
    listed = [skill for skill in skills if skill.is_auto_invocable]
```
docstring 103-105 行同步改写：`Archived skills are not listed, and neither are skills whose author set the standard's disable-model-invocation (E-4). Both stay readable by name — "do not offer this proactively" is the shared meaning, not "this is gone".`

`sop_router.py` 三个站点（355/471/525），把 `if skill.is_archived:` 全部改为：
```python
            if not skill.is_auto_invocable:
```
第一处注释改写：`# "Do not recommend this proactively" — archived by us or disabled by its author. It still reads by name; what it must not do is occupy the tail block every turn.`

- [ ] **Step 5 (Pass)：更新被批准的一组断言并跑绿**

`tests/test_skill_interop.py`：`test_those_fields_are_kept_rather_than_silently_dropped`（119-127 行）改为：

```python
def test_those_fields_are_kept_rather_than_silently_dropped(tmp_path):
    """Dropping them is how an author ends up believing `context: fork` is
    doing something here. ``disable-model-invocation`` left this set in E-4:
    it is honoured now, so it is no longer inert data."""
    skill = parse_skill(_write(tmp_path, "code-review", CODE_REVIEW))

    assert skill.extra["context"] == "fork"
    assert skill.extra["effort"] == "high"
    assert "disable-model-invocation" not in skill.extra
```

同文件新增：

```python
def test_disable_model_invocation_is_honoured_the_standard_way(tmp_path):
    skill = parse_skill(_write(tmp_path, "code-review", CODE_REVIEW))

    assert skill.disable_model_invocation is True
    assert "code-review" not in build_catalog([skill])
```

Run: `python -m pytest tests/test_skill_registry.py tests/test_skill_catalog.py tests/test_sop_router.py tests/test_skill_interop.py tests/test_tool_skill.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6 (Refactor + Docs)：**

`docs/data-contracts.md`：722 行标准字段段落改为"`disable-model-invocation` 自 r16 起生效（`is_auto_invocable`，catalog/router 不再主动提供，按名仍可读）；其余标准字段仍只持有不生效"；723 行 archived 语义行补一句"与 disable-model-invocation 共用 `is_auto_invocable` 一个判定"。

- [ ] **Step 7：Commit**

```bash
git add ai_coding/skills/registry.py ai_coding/skills/catalog.py ai_coding/core/sop_router.py tests/test_skill_registry.py tests/test_skill_catalog.py tests/test_sop_router.py tests/test_skill_interop.py docs/data-contracts.md
git commit -m "feat(skills): honour disable-model-invocation (E-4)"
```

---

### Task 3 — E-3：@mention 注入全文（提及轮 volatile）

**Files:**
- Modify: `ai_coding/core/request_assembly.py:230-242`（调用点）+ 新增私有方法
- Test: `tests/test_skill_reference.py`
- Modify: `docs/data-contracts.md`（新增契约行）

**Interfaces:**
- Consumes: `render_skill_content`（T1）、`resolve_mentions`（现有）。
- Produces: `RequestAssembler._requested_skills_block(requested: Sequence[str]) -> str`（T6 将在此方法内加 deny 分支）。

- [ ] **Step 1 (Red)：新用例（追加到 `tests/test_skill_reference.py`）**

```python
def test_the_named_skill_content_travels_with_the_request(tmp_path):
    """E-3: the model acts in the naming turn — no round trip through the
    tool. This is OpenCode's @-mention semantics (prompt.ts:56-76)."""
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下")

    assert '<skill_content name="tidy">' in text
    assert "tidy 的正文" in text
    assert 'skill(action="read"' not in text, "the tool pointer is gone: the content is here"


def test_the_injected_skill_carries_its_base_directory(tmp_path):
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下")

    assert f"Base directory for this skill: {tmp_path / 'tidy'}" in text


def test_a_broken_skill_says_why_instead_of_injecting_a_body(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text(
        "---\nname: [unclosed\n---\nbody\n", encoding="utf-8"
    )

    text = _request_text(SkillRegistry(tmp_path), "@broken 走一遍")

    assert "无法加载" in text
    assert "<skill_content" not in text
```

将既有 `test_the_named_skill_is_told_how_to_load_itself` 改名为 `test_the_named_skill_is_spelled_the_way_the_tool_takes_it`（断言不变：`'name="tidy"' in text` 现由 `<skill_content name="tidy">` 满足；旧名描述的旧契约已不存在）。

- [ ] **Step 2 (Red)：跑并确认失败**

Run: `python -m pytest tests/test_skill_reference.py -q -p no:cacheprovider`
Expected: FAIL — `<skill_content` 不在文本中；`skill(action="read"` 反而在

- [ ] **Step 3 (Implement)：替换调用点**

`request_assembly.py` 230-242 行替换为：

```python
            # A skill the human named is the same kind of signal, stated by a
            # person rather than a heuristic — and (E-3) it arrives loaded:
            # the full content rides this tail block, so the turn needs no
            # round trip through the tool. The block is volatile: rebuilt from
            # the last user message every iteration, gone with the next one.
            requested = resolve_mentions(user_input, self._skills)
            if requested:
                recommended_skills_block += "\n\n" + self._requested_skills_block(requested)
```

在 helpers 区（`_extract_user_input` 之前）新增：

```python
    def _requested_skills_block(self, requested: Sequence[str]) -> str:
        """The named skills' full content, the same shape a read returns.

        A broken skill says so instead of injecting a half-parsed body — the
        tool would have returned the same verdict, one call later.
        """
        lines = [
            "<requested-skills>",
            "用户显式指定了以下 skill，完整内容已加载，无需再调用 skill 工具读取：",
        ]
        for name in requested:
            skill = self._skills.resolve(name) if self._skills is not None else None
            if skill is None:  # unreachable: resolve_mentions yields resolved names
                lines.append(f"- {name}: 无法加载。")
                continue
            if not skill.is_healthy:
                lines.append(f"- {name}: 无法加载（{skill.error}）。")
                continue
            lines.append(render_skill_content(skill))
        lines.append("</requested-skills>")
        return "\n".join(lines)
```

顶部 import 增加 `from ai_coding.skills.render import render_skill_content`。

- [ ] **Step 4 (Pass)：**

Run: `python -m pytest tests/test_skill_reference.py tests/test_request_assembly.py tests/test_compaction_oc_parity.py -q -p no:cacheprovider`
Expected: PASS（`test_request_assembly.py` 无 mentions 场景，回归不受影响）

- [ ] **Step 5 (Refactor)：**

旧指令块文案（"动手前先读取"）已不存在；确认 `tests/test_skill_reference.py` 顶部 docstring 的"that is a UI symptom"叙述更新为当前语义（@ 已注入全文）。

- [ ] **Step 6：Commit**

```bash
git add ai_coding/core/request_assembly.py tests/test_skill_reference.py docs/data-contracts.md
git commit -m "feat(skills): @mention injects the full skill content (E-3)"
```

---

### Task 4 — E-1：`dialog.skills.show` 下沉到路由层（Home 可种入）

**Files:**
- Modify: `frontend/src/app.tsx:22,313-319,437-440`（删注册、Home 传 client）
- Modify: `frontend/src/routes/home.tsx`（注册 + seed + client prop）
- Modify: `frontend/src/component/dialog-skills.tsx`（onSelect 必填、footer 简化、docstring）
- Modify: `frontend/src/routes/session.tsx:196-203`（注释）
- Create: `frontend/test/home-skills.test.tsx`

**Interfaces:**
- Consumes: `Prompt.seed/onSeedDone`（既有）；`DialogSkills`。
- Produces: `HomeProps.client: IpcClient`（T5 继续在此文件加 `skills`）。

- [ ] **Step 1 (Red)：失败用例 `frontend/test/home-skills.test.tsx`**

```tsx
/**
 * r16/E-1: the Home panel must seed the composer, not just close.
 *
 * The key path (return -> dialog row -> onSelect) is not drivable in this
 * harness (see dialog-mcp-render.test.tsx), so this pins the half that is:
 * Home registers the command WITH a selection handler — the panel it opens
 * says "enter add to prompt" instead of the old read-only "esc close". The
 * seeding itself is verified by the r16 tmux run.
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogProvider } from "../src/dialog/context"
import { Home } from "../src/routes/home"
import { setupKeymap } from "../src/keymap"

describe("Home skills panel", () => {
  test("opening it from Home lands the session-aware panel", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    const client = {
      request: async (method: string) =>
        method === "skills.list"
          ? { skills: [{ name: "demo-skill", description: "A demo", phase: "" }] }
          : {},
    }
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={keymap}>
          <DialogProvider renderer={base.renderer}>
            <Home
              workspace="/tmp/ws"
              version="0.1.0"
              model="m"
              provider="p"
              branch=""
              onSubmit={() => {}}
              client={client as never}
            />
          </DialogProvider>
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()

    const result = keymap.runCommand("dialog.skills.show")
    await setup.flush()
    await setup.waitForVisualIdle()
    const frame = setup.captureCharFrame().split("\n").join(" ")
    setup.renderer.destroy()

    expect(result.ok).toBe(true)
    expect(frame).toContain("enter add to prompt")
    expect(frame).not.toContain("esc close")
  })
})
```

- [ ] **Step 2 (Red)：跑并确认失败**

Run: `cd frontend && bun test test/home-skills.test.tsx`
Expected: FAIL — `result.ok` 为 false（Home 层没有该命令），frame 无 "enter add to prompt"

- [ ] **Step 3 (Implement)：`dialog-skills.tsx`**

docstring 第 1-11 行替换为：

```
/**
 * Skills dialog (M3) — the registry's skills with their description and phase.
 *
 * Opened from the route that owns the composer (Home, session); the selection
 * hands the name back and the composer writes `@name` — the same spelling the
 * request assembler resolves back into a skill (OpenCode writes `@${skill}` at
 * `prompt/index.tsx:590` for the identical gesture).
 */
```

签名与 choose/footer 改为：

```tsx
export function DialogSkills(props: { client: IpcClient; onSelect: (name: string) => void }) {
  ...
  const choose = (option: SelectOption) => {
    props.onSelect(String(option.value ?? option.title))
    dialog.close()
  }

  return (
    <DialogSelect
      title="Skills"
      options={options()}
      onSelect={choose}
      footer={error() || "enter add to prompt"}
    />
  )
}
```

- [ ] **Step 4 (Implement)：`home.tsx`**

1. import 增加：`import { useBindings } from "@opentui/keymap/solid"`、`import { createSignal } from "solid-js"`、`import { DialogSkills } from "../component/dialog-skills"`、`import { useDialog } from "../dialog/context"`、`import type { IpcClient } from "../ipc"`。
2. `HomeProps` 增加 `client: IpcClient`。
3. 组件体顶部：

```tsx
  const dialog = useDialog()
  const [promptSeed, setPromptSeed] = createSignal("")

  // E-1: Home has a composer too — the old app-layer registration called this
  // panel read-only on the belief that it did not. Selecting a skill seeds
  // `@name`, so a session can start on a skill, not only during one.
  useBindings(() => ({
    commands: [
      {
        name: "dialog.skills.show",
        title: "Skills",
        group: "Skill",
        desc: "List available skills",
        palette: true,
        run: () =>
          dialog.open(
            () => (
              <DialogSkills
                client={props.client}
                onSelect={(name: string) => setPromptSeed(`@${name} `)}
              />
            ),
            "large",
          ),
      },
    ],
  }))
```

4. `<Prompt>` 增加 `seed={promptSeed()} onSeedDone={() => setPromptSeed("")}`。

- [ ] **Step 5 (Implement)：`app.tsx` + `session.tsx`**

`app.tsx`：删除 313-319 行的 `dialog.skills.show` 整个条目；删除第 22 行 `import { DialogSkills } ...`；Home 调用（437-440 行）加 `client={props.client}`。
`session.tsx` 196-203 行注释替换为：

```
        // Registered here rather than at the app layer (E-1): the command
        // seeds `@name` into this route's composer, so each composer-owning
        // route registers its own variant. Registering both layers was how
        // the palette showed two identical "Skills" rows.
```

- [ ] **Step 6 (Pass)：**

Run: `cd frontend && bun test test/home-skills.test.tsx && bun run typecheck`
Expected: PASS

- [ ] **Step 7 (Refactor)：** 无（此任务本身就是删重复）。

- [ ] **Step 8：Commit**

```bash
git add frontend/src/app.tsx frontend/src/routes/home.tsx frontend/src/routes/session.tsx frontend/src/component/dialog-skills.tsx frontend/test/home-skills.test.tsx
git commit -m "fix(tui): the Home skills panel seeds the composer (E-1)"
```

---

### Task 5 — E-2：composer `@` 自动补全

**Files:**
- Create: `frontend/src/util/mention.ts`
- Create: `frontend/test/mention.test.ts`
- Modify: `frontend/src/component/prompt.tsx`
- Modify: `frontend/src/app.tsx`（skills 信号 + 拉取 + 下发）
- Modify: `frontend/src/routes/home.tsx`、`frontend/src/routes/session.tsx`（透传 `skills`）

**Interfaces:**
- Consumes: `HomeProps.client`（T4）、`SkillOption`（本任务定义）。
- Produces: `mentionQuery(text, cursor) -> string | null`、`filterSkillMentions(query, skills, limit=10) -> SkillOption[]`、`replaceMentionToken(text, cursor, name) -> { text, cursor }`；`SkillOption { name: string; description?: string | null; phase?: string }`。

- [ ] **Step 1 (Red)：`frontend/test/mention.test.ts`**

```ts
/**
 * r16/E-2: the `@skill` half of the composer's suggestions.
 *
 * The trigger boundary must agree with the registry's `_MENTION`
 * (skills/registry.py:428): a mention the panel suggests but the backend
 * ignores would be a dead reference.
 */

import { describe, expect, test } from "bun:test"
import { filterSkillMentions, mentionQuery, replaceMentionToken } from "../src/util/mention"

describe("mentionQuery", () => {
  test("opens on a word boundary and takes the text after @", () => {
    expect(mentionQuery("fix @ti", 7)).toBe("ti")
    expect(mentionQuery("@", 1)).toBe("")
  })

  test("an email is not a mention", () => {
    expect(mentionQuery("mail me a@b.com", 15)).toBeNull()
  })

  test("a space closes the token", () => {
    expect(mentionQuery("@ti then", 8)).toBeNull()
  })

  test("only text before the cursor counts", () => {
    expect(mentionQuery("@tidy 后缀", 5)).toBe("tidy")
  })
})

describe("replaceMentionToken", () => {
  test("the token is replaced in place and the cursor lands after the space", () => {
    expect(replaceMentionToken("@ti", 3, "tidy")).toEqual({ text: "@tidy ", cursor: 6 })
    expect(replaceMentionToken("用 @ti", 5, "tidy")).toEqual({ text: "用 @tidy ", cursor: 8 })
  })

  test("text after the cursor is preserved", () => {
    expect(replaceMentionToken("@ti 之后", 3, "tidy").text).toBe("@tidy  之后")
  })
})

describe("filterSkillMentions", () => {
  const skills = [
    { name: "demo-skill", description: "A demo" },
    { name: "review-helper", description: "Reviews diffs" },
  ]

  test("an empty query keeps the list, capped", () => {
    expect(filterSkillMentions("", skills).map((s) => s.name)).toEqual([
      "demo-skill",
      "review-helper",
    ])
  })

  test("matches on name and description", () => {
    expect(filterSkillMentions("review", skills).map((s) => s.name)).toEqual(["review-helper"])
    expect(filterSkillMentions("demo", skills).map((s) => s.name)).toEqual(["demo-skill"])
  })

  test("a skill without a description still matches on its name", () => {
    const bare = [{ name: "demo-skill" }]
    expect(filterSkillMentions("de", bare).map((s) => s.name)).toEqual(["demo-skill"])
  })
```

- [ ] **Step 2 (Red)：**

Run: `cd frontend && bun test test/mention.test.ts`
Expected: FAIL — `Cannot find module '../src/util/mention'`

- [ ] **Step 3 (Implement)：`frontend/src/util/mention.ts`**

```ts
/**
 * `@skill` suggestions, the pure half (r16/E-2).
 *
 * Mirrors `slashQuery`'s boundary rule (start or whitespace before the
 * trigger, no whitespace after) and the registry's mention pattern
 * (`skills/registry.py:428`) closely enough that anything this panel
 * suggests resolves server-side after submit.
 */

import { go } from "fuzzysort"

export interface SkillOption {
  name: string
  description?: string | null
  phase?: string
}

/** The `@…` query before the cursor, or null when no token is active. */
export function mentionQuery(text: string, cursor: number): string | null {
  const upto = text.slice(0, Math.max(0, Math.min(cursor, text.length)))
  for (let index = upto.lastIndexOf("@"); index >= 0; index = upto.lastIndexOf("@", index - 1)) {
    const before = index === 0 ? undefined : upto[index - 1]
    if (before !== undefined && !/\s/.test(before)) continue
    const query = upto.slice(index + 1)
    if (/\s/.test(query) || query.includes("@")) return null
    return query
  }
  return null
}

/** Fuzzy-match the skill list; an empty query keeps it (top `limit`). */
export function filterSkillMentions(
  query: string,
  skills: readonly SkillOption[],
  limit = 10,
): SkillOption[] {
  const needle = query.trim()
  if (!needle) return skills.slice(0, limit)
  return go(needle, skills, {
    keys: ["name", "description"],
    scoreFn: (result) => (result[0]?.score ?? 0) * 2 + (result[1]?.score ?? 0),
    threshold: 0,
  })
    .slice(0, limit)
    .map((result) => result.obj)
}

/** Replace the active `@token` with `@name ` and report the new cursor. */
export function replaceMentionToken(
  text: string,
  cursor: number,
  name: string,
): { text: string; cursor: number } {
  const upto = text.slice(0, Math.max(0, Math.min(cursor, text.length)))
  const at = upto.lastIndexOf("@")
  if (at < 0) return { text, cursor }
  return {
    text: `${text.slice(0, at)}@${name} ${text.slice(upto.length)}`,
    cursor: at + name.length + 2,
  }
}
```

- [ ] **Step 4 (Pass)：**

Run: `cd frontend && bun test test/mention.test.ts`
Expected: PASS

- [ ] **Step 5 (Implement)：`prompt.tsx`**

1. import 增加：
```ts
import type { SkillOption } from "../util/mention"
import { filterSkillMentions, mentionQuery, replaceMentionToken } from "../util/mention"
```
2. props 增加 `skills?: SkillOption[]`（docstring：composer 的 @ 补全数据，由 Shell 统一拉取）。
3. 建议项统一为内部类型（放在组件文件顶部）：

```ts
/** One row of the suggestion list: `/command` or `@skill`. */
interface Suggestion {
  label: string
  description: string
  run: () => void
}
```

4. `refresh` 改为双模式：

```ts
  const refresh = () => {
    const text = input?.plainText ?? ""
    const cursor = input?.cursorOffset ?? text.length
    const slash = slashQuery(text, cursor)
    if (slash !== null) {
      const list = filterSlashCommands(slash).map(slashSuggestion)
      setSuggestions(list)
      setSelected((current) => Math.min(current, Math.max(0, list.length - 1)))
      return
    }
    const mention = mentionQuery(text, cursor)
    if (mention !== null) {
      const list = filterSkillMentions(mention, props.skills ?? []).map(mentionSuggestion)
      setSuggestions(list)
      setSelected((current) => Math.min(current, Math.max(0, list.length - 1)))
      return
    }
    setSuggestions([])
  }
```

5. 两个构造器（替换旧的 `select()` 分支逻辑；`select()` 本体变为执行选中的 `run`）：

```ts
  const slashSuggestion = (entry: SlashCommand): Suggestion => ({
    label: `/${entry.name}`,
    description: entry.description,
    run: () => {
      input?.setText("")
      // A text entry (no keymap command) is submitted verbatim: the backend's
      // dispatcher owns what a slash line means (A2.3, SlashCommand's contract).
      const text = slashSelectionText(entry)
      if (text !== undefined) {
        props.onSubmit(text)
        return
      }
      keymap.runCommand(entry.command!)
    },
  })

  const mentionSuggestion = (skill: SkillOption): Suggestion => ({
    label: `@${skill.name}`,
    description: skill.description ?? "",
    run: () => {
      if (!input) return
      const inserted = replaceMentionToken(input.plainText, input.cursorOffset, skill.name)
      input.setText(inserted.text)
      input.cursorOffset = inserted.cursor
      input.focus()
    },
  })

  const select = () => {
    const choice = suggestions()[selected()]
    if (!choice) return
    setSuggestions([])
    choice.run()
  }
```

6. `suggestions` 信号类型改为 `Suggestion[]`；渲染行 `{`/${command.name}`.padEnd(10)}` 改为 `{command.label.padEnd(14)}`；`move` 逻辑不变。

- [ ] **Step 6 (Implement)：Shell 拉取 + 透传**

`app.tsx`：
1. `import type { SkillOption } from "./util/mention"`。
2. Shell 内（`refreshSessions` 旁）：

```tsx
  const [skills, setSkills] = createSignal<SkillOption[]>([])
  // The composer's @ list. skill_review can add skills mid-session, so the
  // table is refreshed with the same cadence as the session list.
  const refreshSkills = async () => {
    try {
      const data = (await props.client.request("skills.list")) as { skills?: SkillOption[] }
      setSkills(data.skills ?? [])
    } catch {
      /* the composer keeps the last list; the panel refetches on open */
    }
  }
```
3. `onMount` 首行加 `void refreshSkills()`；Shell 的 `turn.finished` 分支（`if (event.data["action"] === "quit")` 之前）加 `void refreshSkills()`。
4. Home / Session 调用各加 `skills={skills()}`。

`home.tsx`：`HomeProps` 加 `skills: SkillOption[]`，`<Prompt ... skills={props.skills}>`。
`session.tsx`：`SessionProps` 加 `skills: SkillOption[]`，`<Prompt ... skills={props.skills}>`。

- [ ] **Step 7 (Pass)：全前端回归**

Run: `cd frontend && bun test && bun run typecheck`
Expected: PASS（224 旧 + ~10 新）

- [ ] **Step 8 (Refactor)：** `slash.test.ts` 无需改动（纯函数未变）；确认 `slash.ts` 文件头注释仍准确（未涉）。

- [ ] **Step 9：Commit**

```bash
git add frontend/src/util/mention.ts frontend/test/mention.test.ts frontend/src/component/prompt.tsx frontend/src/app.tsx frontend/src/routes/home.tsx frontend/src/routes/session.tsx
git commit -m "feat(tui): @skill autocomplete in the composer (E-2)"
```

---

### Task 6 — E-5：per-skill 权限（allow/ask/deny）

**Files:**
- Modify: `ai_coding/config/models.py`（AIConfig）
- Modify: `config.example.yaml`
- Create: `ai_coding/skills/policy.py`、`tests/test_skill_policy.py`
- Modify: `ai_coding/security/permission_gate.py`
- Modify: `tests/test_permission_gate.py`
- Modify: `ai_coding/cli.py:164`
- Modify: `ai_coding/core/subagent.py:481`
- Modify: `ai_coding/core/agent_loop.py:1230-1235`
- Modify: `ai_coding/core/request_assembly.py`（T3 的 `_requested_skills_block` 加 deny 分支）
- Test: `tests/test_skill_reference.py`（deny 用例）、`tests/test_agent_loop_optional_components.py`（catalog 隐藏用例）
- Modify: `docs/data-contracts.md`（新契约段）

**Interfaces:**
- Consumes: `SkillRegistry.resolve`；T3 的 `_requested_skills_block`。
- Produces: `AIConfig.skill_permissions: dict[str, Literal["allow","ask","deny"]]`；`denied_skill_ids(registry, permissions) -> frozenset[str]`；`PermissionGate(skill_permissions=..., skill_resolver=...)`（解析器返回规范 id，构造时归一化）。

- [ ] **Step 1 (Red)：四组用例**

`tests/test_skill_policy.py`（新）：

```python
"""TDD (r16/E-5): one reading of ai.skill_permissions — deny is a boundary.

The gate judges a call, the catalog decides what is offered, the mention block
decides what a user's @name receives. All three normalize aliases through this
module so a deny cannot be bypassed by the other spelling.
"""

from ai_coding.skills.policy import denied_skill_ids
from ai_coding.skills.registry import SkillRegistry


def _skill(root, name, alias=None, status=None):
    lines = [f"name: {alias or name}", "description: d"]
    if status:
        lines.append(f"status: {status}")
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text("---\n" + "\n".join(lines) + "\n---\nbody\n")


def test_a_deny_key_is_normalized_through_the_registry(tmp_path):
    _skill(tmp_path, "tidy", alias="clean")
    registry = SkillRegistry(tmp_path)

    assert denied_skill_ids(registry, {"clean": "deny"}) == frozenset({"tidy"})
    assert denied_skill_ids(registry, {"tidy": "deny"}) == frozenset({"tidy"})


def test_non_deny_values_are_not_boundaries(tmp_path):
    _skill(tmp_path, "tidy")
    registry = SkillRegistry(tmp_path)

    assert denied_skill_ids(registry, {"tidy": "ask"}) == frozenset()
    assert denied_skill_ids(registry, {}) == frozenset()


def test_an_unresolvable_key_still_denies_its_literal_name(tmp_path):
    assert denied_skill_ids(SkillRegistry(tmp_path), {"ghost": "deny"}) == frozenset({"ghost"})
```

`tests/test_permission_gate.py` 追加（复用该文件既有的 gate 构造风格）：

```python
def test_a_denied_skill_cannot_be_read_by_the_model():
    gate = PermissionGate(skill_permissions={"danger": "deny"})

    decision = gate.check("skill", {"action": "read", "name": "danger"})

    assert decision == PermissionDecision.DENY


def test_an_ask_skill_routes_the_read_through_approval():
    gate = PermissionGate(skill_permissions={"careful": "ask"})

    decision = gate.check("skill", {"action": "read", "name": "careful"})

    assert decision == PermissionDecision.WARN
    assert "careful" in decision.reason


def test_the_policy_is_keyed_by_canonical_id_not_spelling():
    def resolve(name: str) -> str | None:
        return "tidy" if name in {"tidy", "clean"} else None

    gate = PermissionGate(skill_permissions={"clean": "deny"}, skill_resolver=resolve)

    assert gate.check("skill", {"action": "read", "name": "tidy"}) == PermissionDecision.DENY
    assert gate.check("skill", {"action": "read", "name": "clean"}) == PermissionDecision.DENY


def test_listing_and_unlisted_skills_are_untouched():
    gate = PermissionGate(skill_permissions={"danger": "deny"})

    assert gate.check("skill", {"action": "list"}) == PermissionDecision.ALLOW
    assert gate.check("skill", {"action": "read", "name": "plain"}) == PermissionDecision.ALLOW
```

`tests/test_skill_reference.py` 追加（先把文件内 `_assembler`/`_request_text` 加一个可选 `permissions` 参数）：

```python
def _assembler(registry, permissions=None) -> RequestAssembler:
    ...
    class _Settings:
        working_memory_chars = 1200
        skill_permissions = permissions or {}
    ...
    return RequestAssembler(..., settings=_Settings(), ...)


def _request_text(registry, user_text: str, permissions=None) -> str:
    ...
    assembler = _assembler(registry, permissions)
    ...
```

新用例：

```python
def test_a_denied_skill_is_withheld_from_the_mention_block(tmp_path):
    _write_skill(tmp_path, "tidy")

    text = _request_text(SkillRegistry(tmp_path), "用 @tidy 收拾一下", permissions={"tidy": "deny"})

    assert "已被配置禁止" in text
    assert "tidy 的正文" not in text
```

`tests/test_agent_loop_optional_components.py` 追加（照 `test_instructions_assembled_when_prompt_injected` 的形状，sessions 用外部变量）：

```python
@pytest.mark.asyncio
async def test_a_denied_skill_is_hidden_from_the_model_catalog(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill for testing\n---\nbody\n",
        encoding="utf-8",
    )
    engine = _Engine()
    sessions = SessionService(tmp_path / "sessions")
    loop = AgentLoop(
        engine,
        sessions,
        _registry(),
        prompt=PromptAssembler("base"),
        skills=SkillRegistry(tmp_path / "skills"),
        settings=AIConfig(stop_policy="natural", skill_permissions={"demo": "deny"}),
    )

    await loop.process_input(sessions.create(), "go")

    assert "demo skill for testing" not in (engine.requests[0]["instructions"] or "")
```

- [ ] **Step 2 (Red)：**

Run: `python -m pytest tests/test_skill_policy.py tests/test_permission_gate.py tests/test_skill_reference.py tests/test_agent_loop_optional_components.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: ai_coding.skills.policy`；gate 构造器不接受 `skill_permissions`；AIConfig 不接受 `skill_permissions`

- [ ] **Step 3 (Implement)：config + policy 模块**

`ai_coding/config/models.py` AIConfig（`auto_compact` 之后）：

```python
    # E-5: per-skill policy for the model's skill tool (allow | ask | deny).
    # deny also hides the skill from the model's catalog and withholds
    # @mention injection; ask routes the model's own reads through the
    # approval panel. A typo is a load error, not a silent allow.
    skill_permissions: dict[str, Literal["allow", "ask", "deny"]] = Field(default_factory=dict)
```

`config.example.yaml`（`auto_compact` 之后）：

```yaml
  # E-5: per-skill policy for the model's skill tool. deny hides the skill
  # from the model's catalog and withholds @mention injection too; ask sends
  # the model's own reads through the approval panel. Example:
  # skill_permissions:
  #   some-imported-skill: ask
  skill_permissions: {}
```

`ai_coding/skills/policy.py`：

```python
"""One reading of ``ai.skill_permissions`` (E-5).

A deny is an operator boundary and must mean the same thing at every judge:
the gate (the model's read is refused), the catalog (the skill is not offered)
and the mention block (a user's @name does not inject it). The gate resolves
spellings through an injected resolver (it takes no registry dependency);
these helpers apply the same alias-aware normalization on the registry side.
"""

from __future__ import annotations

from collections.abc import Mapping

from ai_coding.skills.registry import SkillRegistry


def denied_skill_ids(
    registry: SkillRegistry | None, permissions: Mapping[str, str] | None
) -> frozenset[str]:
    """The canonical ids behind every ``deny`` entry, aliases resolved.

    An entry whose key matches no skill stays under its literal name: a typo
    still denies exactly the spelling it names, never more.
    """
    denied: set[str] = set()
    for key, value in (permissions or {}).items():
        if str(value) != "deny":
            continue
        skill = registry.resolve(str(key)) if registry is not None else None
        denied.add(skill.id if skill is not None else str(key))
    return frozenset(denied)


__all__ = ["denied_skill_ids"]
```

- [ ] **Step 4 (Implement)：gate**

`permission_gate.py`：
1. import 增加 `from collections.abc import Callable`（与 Iterable, Mapping 同段）。
2. 构造器签名增加：

```python
        skill_permissions: Mapping[str, str] | None = None,
        skill_resolver: Callable[[str], str | None] | None = None,
```

3. 体内（`_allowed_tools` 赋值旁）：

```python
        # E-5: the model's skill tool is judged per skill id (alias-normalized
        # once, here, so check() is a dict lookup).  ``skill_resolver`` maps a
        # spelling to the canonical id; without one, spellings must match the
        # config literally.
        self._skill_resolver = skill_resolver
        self._skill_permissions = {
            (skill_resolver(key) or key) if skill_resolver is not None else key: str(value)
            for key, value in (skill_permissions or {}).items()
        }
```

4. `check()` 在步骤 2（grants）与步骤 3（allow-list）之间插入：

```python
        # 2.5. E-5: a per-skill policy for the model's skill tool.  Only the
        # model-initiated read is judged: a human's @mention is its own
        # authorization, and ``list`` names skills but reads none.
        if name == "skill" and str(params.get("action", "")).lower() == "read":
            target = str(params.get("name", "")).strip()
            canonical = self._skill_resolver(target) if self._skill_resolver is not None else None
            policy = (
                self._skill_permissions.get(canonical or "")
                or self._skill_permissions.get(target)
                or "allow"
            )
            if policy == "deny":
                return Decision(
                    PermissionDecision.DENY,
                    f"skill '{target}' is denied by configuration",
                )
            if policy == "ask":
                return Decision(
                    PermissionDecision.WARN,
                    f"skill '{target}' requires confirmation",
                )
```

5. `from_config` 增加参数并透传：

```python
    def from_config(
        cls,
        app_cfg: Any,
        *,
        workspace: str | None = None,
        skill_resolver: Callable[[str], str | None] | None = None,
    ) -> PermissionGate:
```
返回的 `cls(...)` 增加 `skill_permissions=getattr(getattr(app_cfg, "ai", None), "skill_permissions", None) or {}, skill_resolver=skill_resolver`。

- [ ] **Step 5 (Implement)：三个消费点**

`cli.py` `_build_loop`（164 行附近）：

```python
        # E-5: the skill policy needs a spelling→id resolver, and the registry
        # is the only thing that can answer it (``skills`` is a required param
        # of _build_loop, so the resolver is always answerable).
        def _resolve_skill(name: str) -> str | None:
            skill = skills.resolve(name)
            return skill.id if skill is not None else None

        gate = PermissionGate.from_config(
            app_cfg, workspace=str(ws), skill_resolver=_resolve_skill
        )
```
（把原来那一行 `gate=PermissionGate.from_config(app_cfg, workspace=str(ws))` 换成变量后传入。）

`core/subagent.py`：`AgentLoop(...)` 调用加 `gate=self._skill_gate(),`，并新增方法：

```python
    def _skill_gate(self) -> Any | None:
        """The operator's skill policy for a child that cannot prompt.

        Only the skill rule travels — a child's MCP surface is not this
        task's to change. ``ask`` degrades the way every WARN does for a
        child (``AutoApproveCallback``); ``deny`` still blocks.
        """
        from ai_coding.security.permission_gate import PermissionGate

        raw = getattr(getattr(self._app_config, "ai", None), "skill_permissions", None) or {}
        if not raw:
            return None
        skills = self._skills

        def resolve(name: str) -> str | None:
            skill = skills.resolve(name) if skills is not None else None
            return skill.id if skill is not None else None

        return PermissionGate(skill_permissions=raw, skill_resolver=resolve)
```

`core/agent_loop.py` `_build_instructions`（1230 行附近）：

```python
        if self.skills is not None:
            current = self.skills.all()
            denied = denied_skill_ids(
                self.skills, getattr(self.settings, "skill_permissions", None)
            )
            if denied:
                current = tuple(skill for skill in current if skill.id not in denied)
            catalog = build_catalog(current)
```
import 增加 `from ai_coding.skills.policy import denied_skill_ids`。

`request_assembly.py` 的 `_requested_skills_block` 内（T3 产物）——`lines = [...]` 初始化之后、`for name in requested:` 之前插入 deny 集合读取，循环体最前插入判定（`name` 已经是 `resolve_mentions` 归一化后的规范 id）：

```python
        denied = denied_skill_ids(
            self._skills, getattr(self._settings, "skill_permissions", None)
        )
        for name in requested:
            if name in denied:
                lines.append(f"- {name}: 已被配置禁止（skill_permissions: deny），不要读取。")
                continue
```
import 增加 `from ai_coding.skills.policy import denied_skill_ids`。

- [ ] **Step 6 (Pass)：**

Run: `python -m pytest tests/test_skill_policy.py tests/test_permission_gate.py tests/test_skill_reference.py tests/test_agent_loop_optional_components.py tests/test_permission_gate_mcp.py tests/test_gate_grants.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 7 (Refactor + Docs)：**

`docs/data-contracts.md` 新增一行到 R11c/E 契约区：`ai.skill_permissions`（allow|ask|deny，alias 归一；deny 三面全拦：gate/catalog/mention；ask 仅拦模型自主 read，走既有 WARN 审批；子代理 ask 降级为自动放行，deny 依旧）。

- [ ] **Step 8：Commit**

```bash
git add ai_coding/config/models.py config.example.yaml ai_coding/skills/policy.py ai_coding/security/permission_gate.py ai_coding/cli.py ai_coding/core/subagent.py ai_coding/core/agent_loop.py ai_coding/core/request_assembly.py tests/test_skill_policy.py tests/test_permission_gate.py tests/test_skill_reference.py tests/test_agent_loop_optional_components.py docs/data-contracts.md
git commit -m "feat(skills): per-skill permissions, deny/ask/allow (E-5)"
```

---

### Task 7 — E-7：workspace 级 `.claude/.agents` 兼容根

**Files:**
- Modify: `ai_coding/paths.py:173-181`
- Modify: `ai_coding/skills/registry.py:336-352`（shadowed 标签）
- Modify: `ai_coding/server/world.py:98-99`（警告文案）
- Test: `tests/test_paths.py`（若无则新建）、`tests/test_skill_registry.py`

**Interfaces:**
- Produces: `Paths.compat_skill_roots -> tuple[Path, ...]`；`skill_roots` 顺序变为 4 根。`SkillRegistry` 构造/优先级语义不变。

- [ ] **Step 1 (Red)：**

`tests/test_paths.py`（先 `ls tests | grep paths` 确认；不存在则新建）：

```python
"""TDD (r16/E-7): the skill roots now include the workspace's foreign layouts.

Precedence stays "nearer wins": the user library is the floor, the workspace's
own `skills/` is the ceiling, and the compat roots sit between them so a
skill written for Claude Code or Codex loads without being louder than the
repo's own conventions.
"""

from ai_coding.paths import Paths


def test_compat_roots_sit_between_user_and_project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = tmp_path / "proj"
    paths = Paths.for_workspace(workspace)

    assert paths.skill_roots == (
        paths.user_skills,
        workspace / ".claude" / "skills",
        workspace / ".agents" / "skills",
        paths.project_skills,
    )
```

（`XDG_DATA_HOME` 隔离是为了断言 `user_skills` 的路径形状，不读真实 home。）

`tests/test_skill_registry.py`：先把既有 `_multi(tmp_path, monkeypatch, *, user=(), project=())`（169 行）扩展两个参数——在循环里追加 compat 根，其余不动：

```python
def _multi(tmp_path, monkeypatch, *, user=(), project=(), claude=(), agents=()):
    ...
    claude_root, agents_root = paths.compat_skill_roots
    for root, names in (
        (paths.user_skills, user),
        (claude_root, claude),
        (agents_root, agents),
        (paths.project_skills, project),
    ):
        ...
```

再加两个用例：

```python
def test_a_compat_root_skill_loads_and_the_project_native_one_wins(tmp_path, monkeypatch):
    registry, paths = _multi(
        tmp_path, monkeypatch, claude=["portable"], project=["portable"]
    )
    (paths.project_skills / "portable" / "SKILL.md").write_text(
        "---\nname: portable\ndescription: ours\n---\nour body\n", encoding="utf-8"
    )

    skill = registry.resolve("portable")

    assert skill is not None and "our body" in skill.body
    assert ("portable", str(paths.compat_skill_roots[0])) in registry.shadowed(), (
        "four roots: the losing scope is named by its path"
    )


def test_a_skill_only_in_the_compat_root_is_still_discovered(tmp_path, monkeypatch):
    registry, _ = _multi(tmp_path, monkeypatch, agents=["portable"])

    assert [skill.id for skill in registry.all()] == ["portable"]
```

（`_multi` 的现有调用签名全部向后兼容——新参数带默认值。）

- [ ] **Step 2 (Red)：**

Run: `python -m pytest tests/test_paths.py tests/test_skill_registry.py -q -p no:cacheprovider`
Expected: FAIL — `AttributeError: 'Paths' object has no attribute 'compat_skill_roots'`；shadowed 标签仍是 "root1"

- [ ] **Step 3 (Implement)：paths.py**

```python
    @property
    def compat_skill_roots(self) -> tuple[Path, ...]:
        """Workspace skill libraries written for other agents (r16/E-7).

        Claude Code's ``.claude/skills`` and the ``.agents/skills``
        convention, workspace-scoped only: a repo that ships its skills for
        several agents loads them here unmodified, and a personal library
        does not silently flood every project's catalog.
        """
        return (
            self.workspace / ".claude" / "skills",
            self.workspace / ".agents" / "skills",
        )

    @property
    def skill_roots(self) -> tuple[Path, ...]:
        """Lowest precedence first. The project root is last, so it wins.

        Order is the precedence, and ``SkillRegistry`` resolves in it. A nearer
        scope beats a broader one, which is the same rule ``AGENTS.md`` follows:
        the repo's own instructions are facts about the repo; the user's are
        defaults. The compat roots sit between the two: a repo's foreign-layout
        skills are project assets, but its native ``skills/`` speaks loudest.
        """
        return (self.user_skills, *self.compat_skill_roots, self.project_skills)
```

- [ ] **Step 4 (Implement)：shadowed 标签 + world 文案**

`registry.py` `shadowed()`：

```python
        scopes = (
            ("user", "project")
            if len(self.roots) <= 2
            else tuple(str(root) for root in self.roots)
        )
```

`world.py:98-99`：

```python
    for name, scope in skill_registry.shadowed():
        _log.warning("skill %r at %s is shadowed (project scope wins)", name, scope)
```

- [ ] **Step 5 (Pass)：**

Run: `python -m pytest tests/test_paths.py tests/test_skill_registry.py tests/test_skill_catalog.py tests/test_skill_install.py -q -p no:cacheprovider`
Expected: PASS（`test_skill_registry.py:215` 的两根标签断言不受影响）

- [ ] **Step 6：Commit**

```bash
git add ai_coding/paths.py ai_coding/skills/registry.py ai_coding/server/world.py tests/test_paths.py tests/test_skill_registry.py
git commit -m "feat(skills): read workspace .claude/.agents skill roots (E-7)"
```

---

## 4. 实施后的最终形态（验证自检用）

- **Home 开局**：Home 输入 `/skills` → 面板 footer "enter add to prompt" → Enter 选 skill → composer 出现 `@demo-skill ` → 补一句话提交 → 会话启动，模型该轮直接可用 skill（无需 tool 调用）；palette 中 "Skills" 只出现一行。
- **会话内**：输入 `@de` → 建议列表出现 `@demo-skill` → Enter → `@demo-skill `（原 token 原位替换）；手打 `@demo-skill ` 同样有效（后端 `resolve_mentions` 已有）。
- **注入审计**：该轮请求尾部含 `<requested-skills>` + `<skill_content name=…>`（正文 + Base directory + `<skill_files>`）；下一条用户消息后该块消失；模型若仍调 `skill read` 返回同形内容（幂等形状）。
- **E-4**：`disable-model-invocation: true` 的 skill 不在系统目录、不被 SOP 路由，但 `skill(read)` 与 `@名字` 仍可加载。
- **E-5**：`skill_permissions: {x: deny}` → 模型目录无 x、@x 不注入（提示"已被配置禁止"）、模型读 x 被 DENY；`{y: ask}` → 模型读 y 弹审批面板（reason 含 skill 名），用户放行后返回同形内容；`list`/mention 不受 ask 影响；子代理 deny 依旧拦截。
- **E-7**：`<ws>/.claude/skills/portable/SKILL.md` 出现在 `/skills` 面板与模型目录；同名时 `<ws>/skills` 胜出并有 shadow 警告（标签为真实路径）。
- **未变**：`status: archived` 语义、catalog 预算降级、`.pending` 审批流、skill_review、SOP 路由格式、`skill list` 输出。

## 5. 验证计划

每任务内联的 pytest/bun 命令之外，收尾全量门禁：

```bash
python -m pytest -q -p no:cacheprovider        # 预期：仅 2 个预存 loop_digest 红；mcp_lifecycle 偶发
mypy ai_coding                                  # 必须 Success
ruff check ai_coding tests                      # 与基线 87 比零新增
cd frontend && bun test && bun run typecheck    # 全绿
```

tmux 端到端（真机，`python -m ai_coding --path config.local.yaml --workspace /tmp/tui-e2e`，skills 用现有 demo-skill/review-helper）：

1. Home：`/skills` → footer 断言 → Enter → composer 断言 `@demo-skill`（E-1）。
2. 会话：`@de` → 建议行 `@demo-skill` → Enter → composer `@demo-skill `；补 "说DEMO" 提交 → 回答 DEMO 且**无**"已读取技能"工具卡（E-2/E-3 一起验）。
3. 在 /tmp/tui-e2e 放 `.claude/skills/portable/SKILL.md` → `/skills` 面板可见（E-7）。
4. 临时把 `demo-skill: deny` 写进 config.local.yaml → 重启 → @demo-skill 提交：模型回复里 skill 内容不出现（注入被扣）；恢复配置。
5. E-5 ask 面板路径不做 tmux（触发模型自主读需构造场景，成本高）：由 gate/dispatcher 单测覆盖，tmux 脚本里记录为已知缺口。

## 6. 自检与风险

- **Spec 覆盖**：E-1→T4；E-2→T5；E-3→T3；E-4→T2；E-5→T6；E-6→T1；E-7→T7。无遗漏。
- **契约一致性**：`render_skill_content` 是 tool-read 与 mention 的唯一形状来源；`denied_skill_ids` 是 catalog/mention 的唯一归一化来源；gate 的归一化发生在构造期（一次），检查期是纯 dict 查询。
- **风险 1**：`useBindings` 的 effect 重注册语义（keymap 层序随 `dialog.isOpen` 等依赖刷新）——T4 后 app 层不再注册同名命令，两个路由层不存在同时存活的可能（`Show` 二选一），该风险结构性消除。
- **风险 2**：`skill_tool.py` 的 `Skill` import 若被其他方法使用（`list_skills` 返回 list[str]），删 import 前用 `grep -n "Skill\b"` 核对。
- **风险 3**：`test_agent_loop_optional_components.py` 的新用例对 `sessions` 变量的用法照抄既有测试（外部 `sessions.create()`），不要发明 `loop.sessions`。
- **明确不做**：`.opencode/skills` 兼容根（可后补一行）；用户级 `~/.claude/skills`（35 个，已评估）；@ 的 CJK 边界放宽（前端与后端一致要求空格，改了要两边同步，收益低）；ask 拦 mention（装配期无交互通道）；mention 落盘（已拍板 volatile）。
- **基线豁免**：`tests/test_loop_digest.py` 2 红与 `test_mcp_lifecycle` flaky 是预存，不以本计划为转移。

## 7. 执行顺序

`T1 → T2 → T3 → T4 → T5 → T6 → T7`（T1 是 T3 的依赖；T3 先于 T6 因其改同一方法；T4 先于 T5 因同改 Home/app）。每个任务独立可验证、独立可 commit；任一任务被否决不影响其余（T3 依赖 T1 除外）。
