# B3 Skill 调研方案

> 范围：SKL-01 ~ SKL-06。基线 commit `fd5a929`；工作树还带有其他批次（B1/B2 等）的未提交改动，本文件只做只读调研。
> 路径基准：源码路径均相对包根 `ai_coding/`（任务里的 `core/...`、`skills/...` 即 `ai_coding/core/...`、`ai_coding/skills/...`）。
> 本文件之外不改任何代码；不 commit / push、不改默认值、不弱化断言、不新增依赖、不联网。

## 0. 结论摘要

| 项 | 是否有推荐方案 | 落点 |
|---|---|---|
| SKL-01 deny 未覆盖 SOP 自动推荐 | 有 | `core/sop_router.py`、`core/request_assembly.py` |
| SKL-02 安装只查顶层软链 | 有 | `skills/install.py` |
| SKL-03 注入无预算 | 有 | `core/sop_router.py`、`skills/catalog.py`、`core/request_assembly.py` |
| SKL-04 `<skills>` 双重包裹 | 有 | `ai/prompt.py` |
| SKL-05 `./path` 被当 git 仓库 | 有 | `skills/install.py` |
| SKL-06 Dendritic / phase | 有（review cursor 归 B4） | `core/sop_router.py`、`core/request_assembly.py` |

**最危险的 2 项**

1. **SKL-02**：`copytree(symlinks=False)` 会把技能目录里任意深度的软链目标**内容**复制进技能库。安装一个不可信仓库即可把 `/etc/shadow`、`~/.ssh/id_rsa` 等带进模型可读的技能目录，且现有 `_inspect` 只查顶层，完全看不到。
2. **SKL-01**：`ai.skill_permissions` 的 `deny` 是运维边界。目录、`@mention`、工具门禁三个面都已生效，唯独 SOP 自动推荐没查，模型会在每轮尾块里被推荐一个“禁止读取”的技能；`check_recovery` 还会直接注入它的 `pattern_dna`。

**与既定方向/锚点的冲突与需要确认的点**（详见 §5）

- 任务给的多个锚点与当前工作树不符：`request_assembly.py:892-899`、`:550`、`:346-347`，`ai/prompt.py:187-190`，`agent_loop.py:2039-2040` 都不是描述中的代码。本文件使用实测行号。
- SKL-05 真正的分发点是 `cli.py:536-540`，不在白名单内。只在 `git_source_url` 里“先判本地路径”会让 `./path` 从“克隆失败”变成“直接报错”，仍然装不上；必须在 `install_from_git` 内委托 `install_from_path`。
- SKL-06 的 phase 若按方向改为“不匹配也不排除”，`tests/test_phase_locking.py` 的测试名/注释语义会过时（该文件不在白名单，且实测仍会通过，见 §1.6）。
- SKL-01 方向只点名 `route`/`intervene`；实测还有第四个注入面 `check_recovery`（`sop_router.py:522-536`，`request_assembly.py:697`）。

---

## 1. 逐项分析

### SKL-01 SOP 自动推荐不查 `deny`

**现状（file:line）**

- `core/sop_router.py:322-329`：`SOPRouter.__init__(skills, threshold, max_matches, dendritic_path)` 没有任何权限入参。
- `core/sop_router.py:351-364`：`route` 遍历 `self._skills.all()`，只跳过“无 triggers”“`not is_auto_invocable`”“phase 不匹配”，没有跳过 deny。
- `core/sop_router.py:458-501`：`intervene` 同样只判 `is_auto_invocable`，命中后直接返回 `pattern_dna`（soft）或 `steps`（hard）。
- `core/sop_router.py:522-536`：`check_recovery` 也只看 `is_auto_invocable`，命中失败模式就返回被禁技能的 `pattern_dna`；调用点 `core/request_assembly.py:697-703`。
- `core/request_assembly.py:517`：组装器构造路由器时只有 `SOPRouter(skills)`，拿不到 `settings.skill_permissions`。
- 已经生效的三个面：目录 `core/agent_loop.py:3511-3516` 与 `app/commands.py:282-288`；`@mention` `core/request_assembly.py:1054-1064`；工具门禁 `security/permission_gate.py:764-765`。
- `skills/policy.py:17-31`：`denied_skill_ids(registry, permissions)` 已经把 alias 归一为 `skill.id`，无 registry 时保留字面名。

**根因**

`deny` 的实现方式是“在每个判定点各查一次”，而不是路由器携带的一条边界。路由器的构造函数早于/独立于 settings，`route`/`intervene` 只问“是否允许主动推荐”（`is_auto_invocable`），没问“是否被运维禁止”。因此同一份配置在三个面一致、第四个面漏掉；`check_recovery` 是第五个面。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 保持现状 | 不改 | 零改动 | 验收“四个面全拒绝”直接失败；tail 每轮推荐被禁技能 |
| B 路由器接收 `denied`（推荐） | 构造时注入 `frozenset[str]`，`route`/`intervene`/`check_recovery` 打分前过滤 | 与 catalog/@mention 同一口径；路由器仍是纯计算；测试无需 settings | 权限变化后需重建 assembler（当前 settings 是构造期静态，可接受） |
| C 路由器直接读 settings | 构造函数接收 settings | 少一个参数 | 纯计算单元依赖配置对象；测试要造 settings；与现有 `SOPRouter(reg)` 的大量用法不兼容 |
| D 只在 `format_block` 过滤 | 输出前删掉被禁项 | 改动最小 | 太晚：`record_recommendation` 已计分、hard intervention 走的是另一条分支、`intervene` 返回的 content 已生成；且 APIs 其它消费者仍拿到被禁项 |

**推荐方案**

```python
# core/sop_router.py
def __init__(
    self,
    skills: SkillRegistry,
    threshold: float = DEFAULT_THRESHOLD,
    max_matches: int = DEFAULT_MAX_MATCHES,
    dendritic_path: str | Path | None = None,
    *,
    denied: frozenset[str] = frozenset(),
) -> None:
    ...
    self._denied = denied

def _blocked(self, skill: Skill) -> bool:
    return skill.id in self._denied
```

- `route`：在 `name = skill.id` 之后立即 `if self._blocked(skill): continue`，位于 triggers/phase 判断之前（不打分、不记 recommendation）。
- `intervene`：循环首行 `if self._blocked(skill) or not skill.is_auto_invocable: continue`。
- `check_recovery`：同上（补齐第五个面；不扩大行为，只让 deny 一致）。

```python
# core/request_assembly.py:517
denied = (
    denied_skill_ids(skills, getattr(settings, "skill_permissions", None))
    if skills is not None
    else frozenset()
)
self._router = SOPRouter(skills, denied=denied) if skills is not None else None
```

`denied_skill_ids` 已在 `request_assembly.py:53` 导入，无新增依赖。`denied` 用 keyword-only，避免破坏现有位置参数调用。

**测试计划**

`tests/test_sop_router.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_denied_skill_is_not_routed` | `SOPRouter(reg, threshold=1.0, denied=frozenset({"sandbox-fix"}))` 对“沙箱”返回 `[]` | 返回 1 条 |
| `test_denied_skill_does_not_intervene` | 两条失败均命中 `failure_patterns`，`intervene(...).mode == "none"` | 返回 hard |
| `test_denied_skill_does_not_recover` | `check_recovery([...], session) is None` | 返回 `pattern_dna` |
| `test_denial_is_not_shared_by_other_skills` | 只禁 `a`，`b` 仍被推荐 | 过度过滤时失败 |

`tests/test_skill_reference.py`（已有 `_assembler(registry, permissions)` 桩，`:80-112`）：

| 用例 | 断言 |
|---|---|
| `test_assembler_passes_denials_to_the_router` | `skill_permissions={"alias": "deny"}` 时组装出的请求不含 `<recommended-skills>`，且 `_router._denied == {"canonical-id"}` |
| `test_denied_mention_still_withheld` | 既有行为不回归（`:217` 已有用例） |

**风险**

- 权限只在 assembler 构造时快照；若未来允许运行中 `/config` 改 `skill_permissions`，需要重建 router。当前 `AIConfig` 是构造期静态，暂不处理，测试中注明。
- 若 skill 未注册（`registry.resolve` 返回 None），`denied_skill_ids` 保留字面名；路由器按 `skill.id` 比较，字面名恰好就是目录名时仍生效，和现有工具门禁口径一致。

---

### SKL-02 安装只查顶层软链

**现状（file:line）**

- `skills/install.py:45-60`：`_inspect` 只检查 `path.is_symlink()` 与 `(path / "SKILL.md").is_symlink()`。
- `skills/install.py:69-76`：`_skill_dirs` 用 `child.is_dir()` 枚举（会跟随软链目录）；被枚举到的软链目录本身能由 `_inspect` 的顶层检查拦住。
- `skills/install.py:140`：`shutil.copytree(candidate, staging, symlinks=False)`。`symlinks=False` 的语义是**解引用**：软链指向的文件/目录内容会被真实复制。
- `skills/install.py:12-13` 的模块 docstring 已承诺“a symlink is refused”，但实现只覆盖顶层。

**根因**

威胁模型是“仓库里的任意文件”，实现却按“候选目录本身 + SKILL.md”检查。技能可以合法携带 `scripts/`、`reference/`；其中任意一层放一个 `scripts/x -> /etc/shadow`，`_inspect` 看不到，`copytree` 会把目标内容复制进技能库。之后模型 `read` 该技能文件即可读到宿主文件。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 保持现状 | 仅顶层检查 | 零改动 | 任意深度的软链都能穿透 |
| B 递归检测并拒绝（推荐） | `os.walk(followlinks=False)` 扫描整棵候选树，发现任何 symlink 即 `InstallError` 并给相对路径 | 覆盖完整威胁模型；不跟随软链，不会在检测阶段越界；错误可定位 | 每个候选多一次树遍历（技能目录通常很小） |
| C `copytree(symlinks=True)` | 保留软链而不是解引用 | 不泄露目标内容 | 技能库里仍留下指向外部的链接，后续 `read`/`scripts` 仍可越界；不解决根因 |
| D 只查 `SKILL.md` 和 `scripts/` | 枚举已知子目录 | 改动少 | 任意新目录（`reference/`、`assets/`）都会成为缺口 |

**推荐方案**

```python
# skills/install.py
import os

def _first_symlink(root: Path) -> Path | None:
    """候选树里第一个 symlink（含悬空链接），没有则 None。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = Path(dirpath) / name
            if candidate.is_symlink():
                return candidate
    return None

def _inspect(path: Path) -> Skill:
    if path.is_symlink():
        raise InstallError(f"{path}: refusing a symlink; a real skill is a real file")
    found = _first_symlink(path)
    if found is not None:
        rel = found.relative_to(path)
        raise InstallError(
            f"{path}: refusing a symlink at {rel}; a real skill is a real file"
        )
    ...
```

- 先保留顶层检查（错误文案与现有测试兼容），再递归。
- `followlinks=False` 保证扫描不会穿出候选目录；软链目录会出现在 `dirnames` 中并命中，但不会被进入。
- `_first_symlink` 同时能看到悬空链接（`is_symlink()` 不要求目标存在）。
- 删除替代方案：不删除现有 `SKILL.md`/description 检查，保持安装门槛不变。

**测试计划**

`tests/test_skill_install.py`：

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_a_nested_directory_symlink_is_refused` | `source/scripts -> outside_dir` | `InstallError`，消息含 `symlink` 与相对路径 `scripts` |
| `test_a_nested_file_symlink_is_refused` | `source/reference/secret -> outside_file` | 同上，含 `reference/secret` |
| `test_a_dangling_symlink_is_refused` | `source/x -> missing` | 同上 |
| `test_supporting_files_without_symlinks_still_install` | 既有 `scripts/run.py`（`:162-171`） | 行为不回归 |
| `test_the_refusal_names_the_offending_path` | 多层嵌套 | 消息精确到相对路径，便于定位 |

**风险**

- **TOCTOU**：`_inspect` 与 `copytree` 之间仍有窗口；`symlinks=False` 本身仍是解引用。彻底消除需要“复制后再次扫描 staging”或改为 `symlinks=True` + 拒绝策略，本批次按方向只做“安装前拒绝”，在实现文档中标注残余风险。
- `os.walk` 默认忽略不可读子目录；这种目录后续 `copytree` 也会失败，因此不会被静默安装成功，但拒绝原因可能变成复制错误而非 symlink。
- 大技能目录（数千文件）多一次 walk；与安装本身的复制成本同阶，可接受。

---

### SKL-03 注入无预算

**现状（file:line）**

- `core/sop_router.py:638-661`：`format_block` 原样输出 `m.description`（`:650`），没有长度上限。
- `skills/catalog.py:33`：`_MAX_DESCRIPTION_CHARS = 200`；`:67-71` 的 `_short_description` 只被 catalog 使用，路由器拿不到。
- `core/sop_router.py:503-518`：`_skill_steps` 找到 `## 步骤` 后返回**整个小节**；只有“没找到步骤小节”的兜底才截 500 字符（`:517-518`）。
- `core/request_assembly.py:1046-1074`：`_requested_skills_block` 对每个 `@mention` 调 `render_skill_content(skill)`，正文与文件列表全量注入；`skills/render.py:55-75` 也没有预算。
- `core/request_assembly.py:76-77`：`_FILE_TOKENS_CAP = 5_000` 只用于 `_referenced_files_block` 的单文件预算（`:1001`），技能块不经过它。
- 现有 catalog 预算 `skills/catalog.py:28`（8,000 字符）只保护系统提示里的目录，不保护尾块。

**根因**

三条注入路径（推荐块、硬干预、`@mention` 全文）各自演化，catalog 的“渐进披露 + 200 字符描述”没有成为共享工具；`format_block` 拿到的是 `SkillMatch.description`（字符串），不是 `Skill`，因此没有复用 `_short_description`。`_skill_steps` 的 500 只写在兜底分支，正常分支反而无界。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 保持现状 | 无上限 | 零改动 | 一个 50 KB 的 SKILL.md 在每轮 `@mention` 时全额重发；推荐块描述可撑爆尾块 |
| B 按路径分别设上限（推荐） | 描述复用 200；hard steps 500；`@mention` 按 `_FILE_TOKENS_CAP` 截断并标记 | 与现有 catalog/文件块口径一致；改动局部；可单测 | `N` 个 mention 仍是线性增长（见风险） |
| C 给推荐尾块设全局预算 | 整个 `recommended-skills`/`requested-skills` 共享一个 token 上限 | 总量有界 | 需要跨块记账、降级策略；改动面大于本项；与方向不符 |
| D 超长技能直接不推荐/不注入 | 超过阈值丢弃 | 实现简单 | 技能“消失”不可诊断；`@mention` 是用户明确要求，丢弃违背语义 |

**推荐方案**

1. `skills/catalog.py`：把裁剪逻辑公开，避免路由器复制一份：

```python
MAX_DESCRIPTION_CHARS = 200  # 保留 _MAX_DESCRIPTION_CHARS 作为别名或直接替换

def shorten_description(text: str) -> str:
    if len(text) > MAX_DESCRIPTION_CHARS:
        return text[:MAX_DESCRIPTION_CHARS].rstrip() + "…"
    return text

def _short_description(skill: Skill) -> str:
    return shorten_description(skill.description)
```

`__all__` 增 `MAX_DESCRIPTION_CHARS`、`shorten_description`。

2. `core/sop_router.py:650`：

```python
from ai_coding.skills.catalog import shorten_description
...
lines.append(
    f"- {m.name}: {shorten_description(m.description)} "
    f"(confidence: {int(m.price * 100)}%)"
)
```

3. `core/sop_router.py`：新增常量并让所有返回路径过同一裁剪：

```python
MAX_INTERVENTION_STEPS_CHARS = 500

def _clip(text: str, limit: int = MAX_INTERVENTION_STEPS_CHARS) -> str:
    text = text.strip()
    return text[:limit].rstrip() + "…" if len(text) > limit else text
```

`_skill_steps` 的三个返回点（找到小节、无下一节、兜底）都改为 `return _clip(...)`。这比在 `_format_intervention` 里截更靠源头：`Intervention.content` 本身也有界，其他消费者不会拿到超长值。（与总方案表格的措辞略有差异：总方案写的是 `_format_intervention`；若必须在展示层截，可在 `request_assembly.py:1119-1127` 再包一次，但会重复。）

4. `core/request_assembly.py:1046-1074`：逐个技能按 token 上限裁剪正文，并保留合法标签：

```python
from dataclasses import replace  # 文件顶部已有 dataclass

body = skill.body
if estimate_tokens(body) > _FILE_TOKENS_CAP:
    body = chars_for_tokens(body, _FILE_TOKENS_CAP)
    skill = replace(
        skill,
        body=body + "\n\n[截断：完整内容请用 skill(action=\"read\", name=\""
                   f"{skill.id}\") 读取]",
    )
lines.append(render_skill_content(skill))
```

只裁 `skill.body`，不裁 `render_skill_content` 的结果，避免把 `<skill_content>` 标签截断。`estimate_tokens` / `chars_for_tokens` 已在 `request_assembly.py:31-35` 导入。

**测试计划**

`tests/test_sop_router.py`：

| 用例 | 断言 |
|---|---|
| `test_format_block_shortens_a_long_description` | 400 字符描述输出≤201 字符且以 `…` 结尾；原串不全量出现 |
| `test_format_block_keeps_a_short_description` | 短描述逐字保留 |
| `test_hard_intervention_steps_are_capped` | 2,000 字符“## 步骤”经 `intervene` 返回的 `content` ≤500+1，且以 `…` 结尾 |

`tests/test_skill_reference.py`：

| 用例 | 断言 |
|---|---|
| `test_a_long_mentioned_skill_is_truncated_and_marked` | 40,000 字符正文；`<skill_content>` 标签完整，正文 ≤ `_FILE_TOKENS_CAP` 估算，含 `[截断` |
| `test_a_short_mentioned_skill_is_unchanged` | 小正文不出现 `[截断`（回归 `:181-198`） |

**风险**

- `_FILE_TOKENS_CAP` 是“单文件”上限；同一条消息 `@` 5 个长技能仍是 5×5,000 token。方向只要求按该常量截断，本批次不做块级总预算；在验收里记录残余风险。
- `format_block` 的 `When:` 触发器列表仍有单项字符串长度无上限（只限制了条数），本次不处理。
- `chars_for_tokens` 对 CJK 是精确前缀反函数，裁点可能落在多字节字符边界之外？该函数按字符扫描，返回的是字符数，`skill.body[:n]` 安全。
- 截断标记放在 `<skill_content>` 内，模型可能把标记当正文；文案已明确指向 `skill(action="read")`，与 `_no_room_line` 的既有做法一致。

---

### SKL-04 `<skills>` 双重包裹

**现状（file:line）**

- `skills/catalog.py:38-41`：`_HEADER` 已带 `<skills>Available skills. Read one with skill(action='read', name=...).`；`:165` 返回 `_HEADER + ... + "\n</skills>"`。
- `ai/prompt.py:235-238`：

```python
if skills_catalog.strip():
    sections.append(
        f"<skills>Available skills:\n{skills_catalog.strip()}</skills>"
    )
```

- 实际产物是 `<skills>Available skills:\n<skills>Available skills. Read one ...</skills></skills>`；两套标题文字还不一样。
- `core/agent_loop.py:3516` 与 `:3544` 是唯一的生产调用：`catalog = build_catalog(current)` → `prompt.build(skills_catalog=catalog, ...)`。
- 现有 `tests/test_prompt.py:24-34` 等用例传入的是裸文本 `"- bash: ..."`，所以从未断言只有一个 `<skills>`。

**根因**

`build_catalog` 的契约从“返回正文”改成了“返回完整块”，但 `PromptAssembler` 仍按旧契约包外层。没有任何测试对真实 `build_catalog` 输出做端到端断言，所以双重标签长期存在；它浪费前缀 token，也让模型看到两个 `Available skills` 标题。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A prompt 侧剥掉 catalog 自带标签 | `skills_catalog` 去掉首尾 `<skills>` | 标签只有一个 | 字符串匹配脆弱；catalog 改标题/属性就失效；且 `superseded_note` 还在标签外 |
| B 删除 prompt 外层包裹（推荐） | 直接把 `skills_catalog.strip()` 作为一个 section | 契约清晰：catalog 拥有标签；改动一行；保留所有调用方 | 需要更新 `test_prompt.py` 的 4 处裸文本 fixture |
| C 让 catalog 只返回正文 | 改 `build_catalog` 的返回值 | prompt 不变 | 破坏 `agent_loop`/TUI `/skills`/catalog 测试等所有调用方；`superseded_note` 拼接点也要改 |

**推荐方案**

```python
# ai/prompt.py:235-238
if skills_catalog.strip():
    sections.append(skills_catalog.strip())
```

- 在 `PromptAssembler.build` docstring 里写明：`skills_catalog` 必须是已带 `<skills>` 标签的完整块（`build_catalog` 的输出）。
- `superseded_note` 仍由 `agent_loop.py:3522-3524` 追加在 `</skills>` 之后；删除外层包裹后它成为独立段落。这符合语义：变更声明是“指令”，不是“目录条目”，不应假装在目录标签内。若要更严格，可在 `agent_loop` 里把它包成 `<system-reminder>`，但那超出本项。

**测试计划**

`tests/test_prompt.py`：

| 用例 | 改动/断言 |
|---|---|
| `test_appends_project_and_skills_sections` | fixture 改为带标签的 catalog；断言 `out.count("<skills>") == 1`、`out.count("</skills>") == 1` |
| `test_base_only` | 不变（无 catalog 无标签） |
| `test_methods_section_joins_between_project_and_skills` | fixture 带标签，顺序断言仍成立 |
| `test_blank_optional_sections_omitted` | 不变 |
| `test_plan_mode_sits_between_methods_and_skills`（`:138-140`） | fixture 带标签，顺序不变 |
| `test_agents_follow_skills`（`:393-398`） | fixture 带标签，`<skills>` 先于 `<agents>` |
| 新增 `test_a_real_catalog_is_not_wrapped_twice` | `build_catalog([...])` → `build(...)`，`count("<skills>") == 1` 且不出现 `<skills>Available skills:\n<skills>` |

**风险**

- `tests/test_context_compression_cache_friendliness.py:312-316` 本来就传 `<skills>x</skills>`，改后只出现一次，断言（无秒级时间戳）不受影响。
- `tests/test_m1b_agent_history.py:90` 传 `"SKILLS"`，改后仍包含该串，顺序断言需实测（若不依赖 `<skills>` 标签则通过）。
- 若存在白名单外的调用方传裸文本，输出会缺少 `<skills>` 包裹；`agent_loop` 是唯一生产调用方，风险可控。实现时可在 prompt docstring 明示契约。

---

### SKL-05 `skill install ./path` 被误判为 git 仓库

**现状（file:line）**

- `ai_coding/cli.py:536-540`（白名单外）：`looks_remote = "://" in source or source.count("/") == 1`；`./my-skill` 的 `count("/") == 1` 且不含 `://`，于是走 `install_from_git`。
- `skills/install.py:150-161`：`git_source_url("./my-skill")` 不匹配 `"://"`、不以 `git@` 开头、`count("/") == 1` 且不以 `/` 开头 → 返回 `https://github.com/./my-skill.git`；随后 `git clone` 失败。
- `skills/install.py:105-144` 的本地安装 `install_from_path` 根本没被调用。
- `skills/install.py:164-198` 的 `install_from_git` 直接拿 `source` 去 clone。

**根因**

分类规则是纯文本的 `count("/")`，且在判断“本地路径是否存在/是否以路径前缀开头”之前执行；同一条规则还分散在 CLI 与 `git_source_url` 两处。`./x`、`../x`、`/abs`、`~/x`、以及当前目录下真实存在的 `foo/bar` 都被归到远端。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 改 `cli.py` 的 `looks_remote` | 在 CLI 里加路径判断 | 直接 | `cli.py` 不在白名单，按约束不能改 |
| B 在 `install.py` 内兜底（推荐） | `_looks_local` 判定；`install_from_git` 先委托 `install_from_path`；`git_source_url` 对本地形态抛 `InstallError` | 不碰 CLI；所有入口（CLI、测试、未来调用方）一致；本地优先符合用户直觉 | `install_from_git` 名字在本地路径下名不副实（文档注明它是分发入口） |
| C 先试本地、失败再 clone | try `install_from_path`，异常后 clone | 无需前缀判断 | 拼错本地路径会触发网络；错误信息误导；违反“没有说清来源就不拉取”的安装原则 |
| D 只改 `git_source_url` 抛错 | 让 clone 路径早失败 | 改动更小 | `./path` 仍然装不上，只是错误从“clone failed”变成“not a repository”，没有修好 |

**推荐方案**

```python
# skills/install.py
_LOCAL_PREFIXES = ("./", "../", "/", "~")

def _looks_local(source: str) -> bool:
    """本地路径优先：显式前缀，或当前文件系统里确实存在。"""
    if source.startswith(_LOCAL_PREFIXES):
        return True
    try:
        return Path(source).expanduser().exists()
    except OSError:
        return False

def git_source_url(source: str) -> str:
    if _looks_local(source):
        raise InstallError(
            f"{source}: looks like a local path, not a repository reference"
        )
    if "://" in source or source.startswith("git@"):
        return source
    if source.count("/") == 1 and not source.startswith("/"):
        return f"https://github.com/{source}.git"
    raise InstallError(f"{source}: not a repository reference (use owner/repo or a URL)")

async def install_from_git(source: str, library, *, force=False, timeout=60.0) -> InstallResult:
    if _looks_local(source):
        return install_from_path(Path(source).expanduser(), library, force=force)
    ...
```

- 先判本地再判 `owner/repo`，与方向一致。
- `install_from_git` 保持 async 签名，直接 `return install_from_path(...)`（无 await，返回值正确）。
- `git_source_url` 的本地拒绝用于保护直接调用方，让错误在“不是仓库”处发生，而不是克隆一个 `github.com/./path`。

**测试计划**

`tests/test_skill_install.py`（现有文件无 git 分类用例，`:1-20` 只导入 `install_from_path`）：

| 用例 | 断言 |
|---|---|
| `test_a_dot_slash_path_is_not_a_repository_reference` | `git_source_url("./x")` 抛 `InstallError`，消息含 `local path` |
| `test_absolute_and_home_paths_are_not_repository_references` | `"/tmp/x"`、`"~/x"` 均抛 |
| `test_an_existing_relative_directory_is_local` | `monkeypatch.chdir(tmp_path)` 后 `_looks_local("owner/repo")` 为 True（目录存在） |
| `test_owner_repo_still_maps_to_github` | `git_source_url("acme/skills") == "https://github.com/acme/skills.git"` |
| `test_install_from_git_delegates_a_local_directory` | `asyncio.run(install_from_git(str(source), library))` 安装成功，未触发网络 |
| `test_full_url_and_scp_style_are_unchanged` | `https://...`、`git@host:owner/repo.git` 原样返回 |

**风险**

- 当前目录下恰好存在名为 `owner/repo` 的目录时，会被当成本地路径；这正是“本地优先”的预期，但需要在帮助文本/文档里说清。
- `~otheruser/x` 依赖 `expanduser` 的行为；不支持时按普通文本继续判 `owner/repo`（`count("/") == 1` 会被当远端），属边界情况。
- `_looks_local` 的 `exists()` 会访问文件系统；对 `owner/repo` 形态是一次额外的 stat，成本可忽略。
- CLI 的 `looks_remote` 仍会把 `./path` 送进 `install_from_git`，但后者现在会转本地；CLI 的提示文案不变，用户结果正确。

---

### SKL-06 分三小块

#### 6.1 review cursor 仅在 `created=True` 前进（归 B4，不在 B3 实现）

**现状（file:line）**

- `core/agent_loop.py:2058-2059`：读取 `session.skill_review_cursor` 作为 delta 起点。
- `core/agent_loop.py:2072-2073`：只有 `if result.created:` 才 `session.skill_review_cursor = len(session.messages)`。
- `core/skill_review.py:266-296`：模型判断“不值得保存”、命名非法、内容校验失败等都会返回 `created=False`。

**根因**：游标被当成“已创建”的提交位点，而不是“已审阅”的位置；`created=False` 时下一轮会重复审阅同一段 delta，浪费 LLM 调用，且随着消息增长反复处理旧内容。

**本批次处理**：只记录证据与建议，不改 `core/agent_loop.py`，不写实现。B4 修复时应把游标推进移到 `run_skill_review` 返回之后、与 `created` 无关（例如 `finally` 或紧跟 `result = ...`），并补 `tests/test_skill_review.py` 的“未创建也推进”用例。

#### 6.2 Dendritic 休眠

**现状（file:line）**

- `core/sop_router.py:324-329`：构造函数接受 `dendritic_path`，有值才建 `DendriticIndex`。
- `core/sop_router.py:378-388`：`route` 内用 `self._dendritic.match(...)` 给同名技能加分，**在 per-skill 循环里调用**。
- `core/sop_router.py:632-635`：`record_signature` 委托给索引。
- `core/request_assembly.py:517`：`self._router = SOPRouter(skills)`，**从不传 `dendritic_path`**；因此 `self._dendritic is None`。
- `core/agent_loop.py:1916-1940`：`_finish_turn` 每轮都构造 `StructuralSignature` 并 `record_signature`；因路由器没接索引，实际是 no-op。
- 离线可测性已经被证明：`tests/test_cross_domain_immune.py:265-377`（boost、无索引、持久化、record API）、`tests/test_e2e_adaptive_learning.py:229-266` 都在 `tmp_path` 下跑，无需网络。因此“无法离线稳定测试就删除”的前提不成立。

**根因**：F3 的纯函数、测试、调用点都齐了，唯独 composition root 没把可选路径传进去；同时匹配放在循环里，一旦接通会变成 `技能数 × 签名数` 的重复计算。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 删除休眠代码 | 移除 DendriticIndex/boost/record | 减少死代码 | 丢掉已测过的跨域推荐能力；`agent_loop` 的 record 调用也要删（白名单外） |
| B 接线并提前计算（推荐） | `RequestAssembler` 从 workspace 派生 `.ai-coding/dendritic/index.json`；`route` 循环外算一次 `match` | 功能真正生效；离线可测；避免 N 倍重复匹配 | 每轮成功加载技能会写一个小 JSON（已有 200 条 FIFO 上限与原子替换） |
| C 接线但不改匹配位置 | 只传 path | 改动最小 | 接通后立即引入每技能一次 match 的开销 |

**推荐方案**

```python
# core/request_assembly.py:516-520
dendritic_path = (
    Path(workspace).expanduser() / ".ai-coding" / "dendritic" / "index.json"
    if workspace
    else None
)
self._router = (
    SOPRouter(skills, dendritic_path=dendritic_path, denied=denied)
    if skills is not None
    else None
)
```

```python
# core/sop_router.py: route()
dendritic_best: dict[str, float] = {}
if self._dendritic is not None:
    for match_name, match_score in self._dendritic.match(
        tools=set(recent_tools),
        errors=set(all_failures),
        task_keywords=set(task_goal.lower().split()),
    ):
        dendritic_best[match_name] = max(
            dendritic_best.get(match_name, 0.0), match_score
        )
...
# 循环内删掉原来的 match 调用
total_score += dendritic_best.get(name, 0.0) * SIGNATURE_MATCH_WEIGHT
```

- 路径放工作区状态目录（`Paths.state` 即 `<workspace>/.ai-coding`），不落到进程 CWD；`DendriticIndex._save` 会 `mkdir(parents=True)`。
- `workspace` 为空（旧构造器/测试）时不接索引，保持 no-op。
- `record_signature` 与 `_finish_turn` 不动（`agent_loop` 白名单外）。

#### 6.3 phase 未校验 / 不匹配即永不推荐

**现状（file:line）**

- `core/sop_router.py:363-364`：`if phase and skill.phase and skill.phase != phase: continue` —— 硬过滤。
- `skills/registry.py:216`：`phase=as_str(data.get("phase"))`，任意字符串都接受，没有枚举校验。
- `core/request_assembly.py:1099-1115`：`_compute_phase` 只返回 `planning`/`executing`/`debugging`/`reviewing`。
- `tests/test_phase_locking.py:124-134` 把“不匹配就过滤”写成测试名；但输入“计划任务”只命中 plan-skill 的关键词，所以即使删掉硬过滤，该用例仍会通过。

**根因**：phase 被实现成“路由闸门”，但声明值是自由文本、计算值是闭集；一个拼写错误（`phase: debuging`）会让技能在所有真实 phase 下都被跳过，且没有任何提示。catalog 仍然显示 `[phase: ...]`，模型看得到却永远收不到推荐。

**候选与取舍**

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A 保持硬过滤 + 校验 phase 枚举 | 未知值视为无锁 | 修正拼写错误 | 合法的“暂时不匹配”仍会永久排除；phase 仍无法作为软提示 |
| B 删除硬过滤（推荐） | 不匹配也照常打分；phase 仅作为 catalog 提示 | 符合方向“回退为不限”；不会因拼写/阶段变化永久失联；改动最小 | phase 不再影响路由排序（如需偏好可另加小权重，本批次不做） |
| C 不匹配加负分 | 保留偏好但可推荐 | 排序更精细 | 方向只要求“不限”；负分会与阈值交互，行为难解释 |

**推荐方案**

- 删除 `sop_router.py:363-364` 的 `continue`；保留 `phase` 形参（现有测试与 API 依赖），在 docstring 写明“phase 是展示提示，不是路由闸门”。
- 不新增 phase 枚举校验：既然不再排除，未知值不再产生“永不推荐”的故障；catalog 会如实显示声明值。
- 可选（不推荐在本批次做）：对 phase 匹配加 `+1.0` 排序权重；会改变现有分数断言，收益不明确。

**测试计划**

`tests/test_sop_router.py` / `tests/test_skill_reference.py`：

| 用例 | 断言 | 修复前失败点 |
|---|---|---|
| `test_a_mismatched_phase_is_still_recommended` | `phase: debugging` + 关键词“沙箱”，`route(..., phase="planning")` 返回该技能 | 返回 `[]` |
| `test_an_unknown_phase_value_is_not_a_permanent_lock` | `phase: debuging`（拼错），`phase="debugging"` 仍返回 | 返回 `[]` |
| `test_matching_phase_still_works` | 原有 `phase="planning"` 用例保持通过 | — |
| `test_assembler_wires_dendritic_index` | `workspace=tmp_path` 的 assembler `_router._dendritic is not None`；`record_signature` 后 `.ai-coding/dendritic/index.json` 存在 | `_dendritic is None` |
| `test_no_workspace_keeps_dendritic_off` | `workspace=""` → `_dendritic is None`，不写文件 | — |
| `test_dendritic_match_runs_once_per_route` | monkeypatch `DendriticIndex.match` 计数；3 个技能 route 一次 → 调用 1 次 | 调用 3 次（接线后） |

**风险**

- 删除 phase 闸门会让“调试技能在 planning 阶段因关键词命中而被推荐”。这是方向明确要求的行为；catalog 的 `[phase: ...]` 仍提示模型。
- `test_phase_locking.py` 不在白名单，无法修改；实测其三条路由用例在新语义下仍通过（输入只命中各自关键词），但测试名“filters_by_phase”会名不副实，建议 B4/后续批次改注释。
- Dendritic 索引是工作区级持久状态；若两个 AgentLoop 并发写同一 workspace，`_save` 的原子替换只保证文件不半写，不保证不丢更新（后写覆盖）。现有 200 条 FIFO 可接受；本批次不引入锁。

---

## 2. 批次内顺序与依赖

按“同一文件合并改动、先安全后体验”的顺序：

1. **SKL-02 + SKL-05（`skills/install.py`）**
   - 先做安全项（递归 symlink 拒绝），再做分类器（本地优先）。
   - 验证：`python -m pytest tests/test_skill_install.py -q`。
2. **SKL-01（`core/sop_router.py` + `core/request_assembly.py`）**
   - 先加 `denied` 参数与三处过滤，再在 assembler 里注入；测试覆盖 route/intervene/check_recovery 与 `@mention` 回归。
   - 验证：`python -m pytest tests/test_sop_router.py tests/test_skill_policy.py tests/test_skill_reference.py -q`。
3. **SKL-06（`core/sop_router.py` + `core/request_assembly.py`）**
   - 复用第 2 步的构造点同时传 `dendritic_path`；同一次改动把 `match` 提到循环外并删 phase 闸门。
   - 验证：`python -m pytest tests/test_sop_router.py tests/test_phase_locking.py tests/test_cross_domain_immune.py -q`。
4. **SKL-03（`skills/catalog.py` + `core/sop_router.py` + `core/request_assembly.py`）**
   - 先公开 `shorten_description`，再改 `format_block`、`_skill_steps`、`_requested_skills_block`。
   - 验证：`python -m pytest tests/test_skill_catalog.py tests/test_sop_router.py tests/test_skill_reference.py tests/test_skill_progressive_disclosure.py -q`。
5. **SKL-04（`ai/prompt.py` + `tests/test_prompt.py`）**
   - 独立项，最后做，避免 prompt 快照类测试在中间步骤反复失败。
   - 验证：`python -m pytest tests/test_prompt.py tests/test_context_compression_cache_friendliness.py tests/test_m1b_agent_history.py -q`。
6. **全量回归**：`python -m pytest tests -q`（不联网；如有 live 标记按仓库既有约定跳过）。

依赖关系：SKL-06 的 assembler 接线与 SKL-01 共用 `SOPRouter(...)` 构造点，必须合并成一次编辑，避免两次改同一行；SKL-03 也在同一文件，但函数互不重叠，可在其后独立提交。

---

## 3. 明确不做

- **不修改 `core/agent_loop.py`**：SKL-06 的 review cursor（`agent_loop.py:2072-2073`）归 B4；`_finish_turn` 的 `record_signature` 调用保持原样，只让路由器真正拥有索引。
- **不修改 `ai_coding/cli.py`**：SKL-05 在 `skills/install.py` 内通过 `install_from_git` 委托解决，不碰 CLI 的 `looks_remote`。
- **不新增/修改配置默认值**：不新增 `skill_permissions` 之外的旋钮，不改 `config/models.py`。
- **不删除 Dendritic**：离线测试已存在，选择接线而非删除。
- **不新增依赖、不联网、不 commit/push**。
- **不改 `skills/registry.py` 的解析语义**（phase 仍是自由字符串）。
- **不修改 `core/skill_review.py`**：review cursor 的推进点不在该文件，且本批次不处理 B4 项。
- **不修改 `tests/test_phase_locking.py`**（白名单外）；新语义下它仍通过。
- **不做块级总预算**（`N` 个 `@mention` 的线性增长）、**不做 `format_block` 触发器字符串裁剪**、**不做 phase 排序加分**：均为残余风险，记录而非实现。

---

## 4. 文件清单

### 4.1 白名单内、按方案会修改

| 文件 | 涉及项 | 改动 |
|---|---|---|
| `ai_coding/core/sop_router.py` | SKL-01/03/06 | 构造函数增 `denied`；三处 deny 过滤；`format_block` 描述裁剪；`_skill_steps` 500 截断；`route` 循环外算 dendritic、删 phase 闸门 |
| `ai_coding/skills/install.py` | SKL-02/05 | `_first_symlink` + `_inspect` 递归拒绝；`_looks_local`；`install_from_git` 本地委托；`git_source_url` 本地拒绝 |
| `ai_coding/skills/catalog.py` | SKL-03 | 公开 `MAX_DESCRIPTION_CHARS`、`shorten_description`；`_short_description` 复用 |
| `ai_coding/core/request_assembly.py` | SKL-01/03/06 | 构造 router 时注入 `denied` 与 `dendritic_path`；`_requested_skills_block` 按 `_FILE_TOKENS_CAP` 裁剪正文并标记 |
| `ai_coding/ai/prompt.py` | SKL-04 | 去掉外层 `<skills>` 包裹；docstring 写明 catalog 契约 |

### 4.2 白名单内、只改测试

| 文件 | 涉及项 | 改动 |
|---|---|---|
| `tests/test_sop_router.py` | SKL-01/03/06 | deny 路由/干预/recovery；描述裁剪；步骤截断；Dendritic 单次匹配；phase 回退 |
| `tests/test_skill_install.py` | SKL-02/05 | 嵌套/悬空软链拒绝；本地路径分类；`install_from_git` 本地委托 |
| `tests/test_skill_catalog.py` | SKL-03 | `shorten_description` 的 200 字符边界 |
| `tests/test_skill_reference.py` | SKL-01/03/06 | assembler 注入 denied；`@mention` 截断；Dendritic 接线 |
| `tests/test_prompt.py` | SKL-04 | fixture 改为带标签 catalog；单标签断言 |

### 4.3 只读、不改

| 文件 | 原因 |
|---|---|
| `ai_coding/skills/policy.py` | `denied_skill_ids` 已满足需求，直接复用 |
| `ai_coding/skills/render.py` | `render_skill_content` 保持共享语义，裁剪在调用侧 |
| `ai_coding/core/skill_review.py` | cursor 归 B4，本批次不动 |
| `ai_coding/core/agent_loop.py` | 约束明确不写；SKL-06 cursor 归 B4 |
| `ai_coding/cli.py` | 白名单外；SKL-05 在 install.py 兜底 |
| `tests/test_phase_locking.py` | 白名单外；新语义下仍通过 |

---

## 5. 锚点校正与冲突发现

### 5.1 任务给定锚点 vs 实测

| 任务锚点 | 实测 | 说明 |
|---|---|---|
| `core/sop_router.py:331-336` route 签名 | `322-329` 构造；`331-336` 是 `route` 形参 | 基本准确 |
| `core/sop_router.py:352-360` 过滤 | `351-364` 循环与四类跳过 | 准确 |
| `core/sop_router.py:458-460` intervene | `458-501` | 准确 |
| `skills/policy.py:17-31` | 完全一致 | 准确 |
| `skills/install.py:45-60` `_inspect` | 完全一致 | 准确 |
| `skills/install.py:140` copytree | 完全一致 | 准确 |
| `skills/install.py:150-161` git 源判定 | 完全一致 | 准确 |
| `core/sop_router.py:638-661` format_block | 完全一致 | 准确 |
| `core/request_assembly.py:892-899` | 实际是 `_environment_block` 的 `last_compacted_turn` 分支，与技能注入无关 | **锚点错误**；真实注入在 `683-741`、`1046-1074` |
| `core/request_assembly.py` “@mention 全文块约 :550” | `:550` 是 `assemble` 形参 | **锚点错误**；真实在 `1046-1074` |
| `core/sop_router.py:329` Dendritic | 准确（`DendriticIndex` 构造） | 准确 |
| `core/request_assembly.py:346-347` Dendritic 休眠 | 实际是 `_LiveReadingWatch` 的本地计数重置 | **锚点错误**；真实未接线点是 `:517` |
| `core/sop_router.py:363` phase | 完全一致 | 准确 |
| `core/agent_loop.py:2039-2040` review cursor | 实际是 interval 判定；游标推进在 `2072-2073` | **锚点错误**，且归 B4 |
| `ai/prompt.py:187-190` 外层 `<skills>` | 实际是 `build` 的 `agents` 形参；外层包裹在 `235-238` | **锚点错误** |

### 5.2 与方向的冲突/需要决策

1. **SKL-05 的修改位置**：方向写“`git_source_url` 先判本地路径”，但 CLI 在调用前就已选择 `install_from_git`。只改 `git_source_url` 无法让 `./path` 装上。推荐在 `install_from_git` 内委托，`git_source_url` 只做拒绝；这是对方向的必要补全，不改变“本地优先”的结论。
2. **SKL-03 的截断层**：总方案写“`_format_intervention` 的步骤截断到 500”，推荐改为在 `_skill_steps` 源头截断（`Intervention.content` 也受益）。若必须严格按字面，可在 `_format_intervention` 再包一层，但会重复裁剪。
3. **SKL-06 phase 语义**：方向要求“不匹配回退为不限”，这会让既有 `test_phase_locking.py` 的测试名/注释不再准确；该文件不在白名单，实测仍通过，但建议后续批次改名/改注释。
4. **SKL-01 的第五个面**：方向只写 `route`/`intervene`；`check_recovery` 同样按失败模式自动注入被禁技能的 `pattern_dna`。推荐一并过滤，否则验收“配置 deny 后所有自动注入都为 none”仍不成立。
5. **SKL-04 与 `superseded_note`**：`agent_loop.py:3522-3524` 把变更声明拼在 `build_catalog` 之后。去掉 prompt 外层包裹后，声明会落在 `</skills>` 之外；语义上合理（它与目录不同层），但需在实现文档中确认这是预期，而不是“标签外内容丢失”。
6. **SKL-02 的真实解引用点**：方向只点名 `_inspect`，但 `copytree(symlinks=False)` 才是把目标内容复制进来的操作。递归拒绝能覆盖已知路径，TOCTOU 仍是残余风险；若要彻底，需复制后复检或改用手写复制（超出本项范围）。
7. **SKL-06 锚点**：方向把 Dendritic 休眠指向 `request_assembly.py:346-347`，实际未接线点是 `:517`；实现时不要改错位置。
