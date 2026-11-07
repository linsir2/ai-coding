# B3 Skill 实现记录

> 范围：SKL-01 ~ SKL-06（SKL-06 仅 Dendritic 健壮性与 phase 加载校验；review cursor 归 B4）。
> 依据：`01-research.md` 与 `02-design-review.md` 的“裁决/采纳”列。
> 未 commit；未改默认值、未弱化既有断言、未新增依赖、未联网。

## 1. 逐项改动

### SKL-01 SOP 自动推荐未覆盖 deny

- `ai_coding/core/sop_router.py:351-366`
  - `SOPRouter.__init__` 增加 keyword-only `denied: frozenset[str] = frozenset()`，保存为 `self._denied`；新增 `_blocked(skill)`。
  - `route`（`:368` 起）在 `name = skill.id` 后、triggers/phase/打分之前 `continue`，被禁技能既不计分也不写 `record_recommendation`。
  - `intervene`（`:504` 起）循环首行 `if self._blocked(skill) or not skill.is_auto_invocable: continue`。
  - `check_recovery`（`:568` 起）同样过滤，补齐第五个注入面。
- `ai_coding/core/request_assembly.py:519-535`
  - 构造 `SOPRouter` 时传入 `denied=denied_skill_ids(skills, settings.skill_permissions)`；无 registry 时为空集。
  - 复用已有 `skills/policy.py::denied_skill_ids`，未新增依赖。

### SKL-02 安装只查顶层软链

- `ai_coding/skills/install.py:46-60` 新增 `_first_symlink(root)`：`os.walk(followlinks=False)`，同时检查 `dirnames`/`filenames`，能发现悬空链接。
- `ai_coding/skills/install.py:130-142`：`install_from_path` 在 `is_dir` 校验后、`_candidates` 展开前，先对**整个 `source_path`** 递归扫描；命中即 `InstallError`，消息包含相对路径（`found.relative_to(source_path)`）。
- 保留 `_inspect` 原有顶层检查（`:63-66`），错误文案兼容既有测试。
- 覆盖容器级软链（`repo/skills -> /host/skills`）：在展开候选之前就拒绝。

### SKL-03 注入无预算

- `ai_coding/skills/catalog.py:33-44`：`_MAX_DESCRIPTION_CHARS` 改为公开 `MAX_DESCRIPTION_CHARS`，新增 `shorten_description()`；`_short_description` 复用；`__all__` 增两项。
- `ai_coding/core/sop_router.py`
  - `:40` 新增 `MAX_INTERVENTION_STEPS_CHARS = 500`；`:73-80` 新增 `_clip()`。
  - `_skill_steps`（`:549-564`）的三个返回点全部 `_clip`（找到小节 / 无下一节 / 兜底）。
  - `intervene` 的 soft `pattern_dna`（`:531-536`）与 `check_recovery` 的 `pattern_dna`（`:568-572`）同样 `_clip`。
  - `format_block`（`:684` 起）描述改用 `shorten_description(m.description)`，与 catalog 共用 200 上限。
- `ai_coding/core/request_assembly.py:1090-1101`：`@mention` 正文超过 `_FILE_TOKENS_CAP` 时用 `chars_for_tokens` 截断 `skill.body`（不截整块），追加 `[截断：...]`，`<skill_content>` 开闭标签仍由 `render_skill_content` 完整生成。

### SKL-04 `<skills>` 双重包裹

- `ai_coding/ai/prompt.py:235-241`：删除外层 `<skills>Available skills:...` 包裹，直接 `sections.append(skills_catalog.strip())`；空串不产出空标签。
- `PromptAssembler.build` 的 `skills_catalog` 参数注释写明契约：传入的是 `build_catalog` 的完整块。
- `superseded_note` 仍由 `agent_loop` 追加在 `</skills>` 之后（未改 `agent_loop.py`），语义上是独立段落，符合 02 裁决。

### SKL-05 `./path` 被当 git 仓库

- `ai_coding/skills/install.py:178-193`：新增 `_LOCAL_PREFIXES = ("./", "../", "/", "~")` 与 `_looks_local()`（前缀或 `Path(source).expanduser().exists()`）。
- `git_source_url`（`:196-213`）：先判本地，本地形态直接 `InstallError("... looks like a local path ...")`，不再生成 `github.com/./path`。
- `install_from_git`（`:216-235`）：`_looks_local` 时直接委托 `install_from_path(Path(source).expanduser(), ...)`，保持 async 签名；`cli.py` 未改。
- 文档语义已写入 `install_from_git` docstring：`./repo` 现在直装**当前工作树（含未提交文件）**，而不是克隆某个提交。

### SKL-06 Dendritic 健壮性 + phase 加载校验

- `ai_coding/core/sop_router.py:190-205`：`DendriticIndex.match()` 在 `tools`、分词后的 `error_words`、`task_keywords` 全空时返回 `[]`，避免 `_jaccard(∅, ∅) == 1.0` 给所有技能 +2.0。
- `ai_coding/core/sop_router.py:221-233`：`_load()` 对 `signatures` 做 `isinstance(..., list)` 保护，逐项要求 `dict`；字符串/整数/损坏项跳过，不再 `AttributeError`/`TypeError`。
- `ai_coding/core/sop_router.py:388-434`：Dendritic `match` 移到 per-skill 循环外，一次 route 只匹配一次（`dendritic_best` 聚合同名最高分）。
- phase：
  - `ai_coding/skills/registry.py:48-52` 新增 `SKILL_PHASES = ("planning", "executing", "debugging", "reviewing")`；`:181-188` 新增 `_valid_phase()`；解析时 `phase=_valid_phase(...)`（`:222`）。
  - 未知值（如 `debuging`）在**加载时**归一为 `""`，即无限制。
  - `sop_router.py:405-409` **保留**已知 phase 的硬过滤，未删除 phase 闸门（遵循 02 裁决，而非 01 的“删除硬过滤”备选）。
- review cursor 未改：`core/agent_loop.py` 未触碰。

### 测试修正

- `tests/test_skill_reference.py:178-180`：`assert ... == "" or True` 改为 `assert SOPRouter.format_block([]) == ""`。
- 测试新增：
  - `tests/test_sop_router.py`：deny route/intervene/recovery/不误伤其他技能；描述裁剪；步骤/`pattern_dna` 截断；Dendritic 单次匹配；损坏 `signatures`；空查询；未知 phase 不锁死；已知 phase 仍过滤。
  - `tests/test_skill_install.py`：嵌套目录/文件软链、悬空软链、容器级软链；本地路径分类；`install_from_git` 本地委托。
  - `tests/test_skill_reference.py`：assembler 注入 denied 且尾块不推荐；长 `@mention` 截断且标签闭合；短正文不截断；Dendritic 接线/不接线。
  - `tests/test_skill_catalog.py`：`shorten_description` 200 字符边界。
  - `tests/test_prompt.py`：fixture 改为带标签 catalog；新增真实 `build_catalog` 不被二次包裹。

## 2. 测试证据

以下命令均实际运行、退出码 0：

```bash
cd /workspace/ai-coding
.venv/bin/python -m pytest \
  tests/test_sop_router.py tests/test_skill_install.py tests/test_skill_review.py \
  tests/test_skill_reference.py tests/test_prompt.py tests/test_skill_catalog.py -q
# 183 passed（test_sop_router_v2.py 不存在）

.venv/bin/python -m pytest \
  tests/test_phase_locking.py tests/test_cross_domain_immune.py \
  tests/test_skill_registry.py tests/test_skill_policy.py tests/test_skill_render.py \
  tests/test_skill_progressive_disclosure.py -q
# 全部通过

.venv/bin/python -m pytest tests -q          # 全量；退出码 0（仅既有 skip）
.venv/bin/ruff check ai_coding tests/test_sop_router.py tests/test_skill_install.py
# All checks passed!
.venv/bin/mypy ai_coding
# Success: no issues found in 186 source files
```

## 3. 偏离

1. **phase 语义**：采用 02 裁决“保留已知 phase 硬过滤 + 加载时校验”，未采用 01 中“删除 phase 闸门”的备选方案。因此 `tests/test_phase_locking.py::test_filters_by_phase` 仍然名副其实。
2. **SKL-02 扫描位置**：按 02 裁决放在 `install_from_path` 的 `source_path` 整体扫描；`_inspect` 仅保留既有顶层检查，没有在 `_inspect` 里再复制一次递归扫描（整体扫描已覆盖候选）。
3. **SKL-03 截断层**：按 01 §5.2 的建议在 `_skill_steps` 源头截断（而不是 `_format_intervention` 展示层），使 `Intervention.content` 本身有界；未重复裁剪。
4. **SKL-06 `match` 空集判定**：以“分词后的 `error_words`”为空作为错误集合为空的判据（长度 ≤2 的词本就不参与匹配）。

## 4. 已知限制（残余风险）

- **SKL-02 TOCTOU**：`_first_symlink` 扫描与 `copytree(symlinks=False)` 之间仍有时间窗口。按 02 裁决只做“安装前拒绝”；`symlinks=False` 复制后的 staging 已无链接，复制后再扫无效。要彻底消除需改用 `symlinks=True` + 复制后策略或手写复制，超出本批次。
- **SKL-03 块级总预算**：`_FILE_TOKENS_CAP` 是单文件上限；同一条消息 `@mention` N 个长技能仍线性增长，本批次不做跨块总预算。
- **SKL-03 触发器字符串**：`format_block` 的 `When:` 仍只限制条目数，单项字符串长度无上限。
- **SKL-01 权限快照**：`denied` 在 `RequestAssembler` 构造时固化；运行中修改 `skill.permissions` 需要重建 assembler。`AIConfig` 当前为构造期静态，暂不处理。
- **SKL-06 phase**：合法的“当前阶段不匹配”仍会被硬过滤（这是 02 裁决要求的已知行为）；catalog 的 `[phase: ...]` 仍提示模型。
- **SKL-06 review cursor**：`created=False` 时游标不前进的问题未修复，按要求归 B4（`core/agent_loop.py` 未改）。
- **Dendritic 并发**：工作区级索引的 `_save` 仅保证原子替换，不保证并发写不丢更新；沿用既有 200 条 FIFO 上限。

## 5. 评审整改（REQUEST-CHANGES）

### P0 SKL-02 生产路径仍可绕过

- `ai_coding/skills/install.py:129-139`：`install_from_path` 在 `resolve()` **之前**判 `Path(source).expanduser().is_symlink()`，命中直接 `InstallError`。原来的 `resolve()` 会把 `repo/skills` 这条根链解开，`_first_symlink` 的 `os.walk` 不返回根，因此只扫目标内部永远看不到该链。
- `ai_coding/skills/install.py:261-270`：`install_from_git` 在把 `candidate`（`scratch/<container>`）传给 `install_from_path` 前先 `is_symlink()` 拒绝；`is_dir()` 会跟随链接，不判就会去读宿主目录。
- 新增 `tests/test_skill_install.py::test_the_production_call_site_refuses_a_symlinked_skills_root`：直接调用 `install_from_path(repo/"skills")`（`repo/skills` 为软链），断言 `InstallError`、宿主技能未出现在 library、library 未被创建。

### P2-1 Dendritic 空维度仍计分

- `ai_coding/core/sop_router.py:266-290`：`_compute_similarity` 改为逐维度判断，某维度两边都为空则**跳过**（不计分、不计权重）；此前 `_jaccard(∅, ∅)=1.0` 让空 errors 白拿 0.3、空 task 白拿 0.3（合计 0.6）。
- 新增 `tests/test_sop_router.py::test_a_dimension_empty_on_both_sides_is_skipped`：tools 命中、errors/task 两边皆空时，分数恰为 `WEIGHT_TOOLS`（0.4），而不是 1.0。

### P2-2 兜底步骤静默截断

- `ai_coding/core/sop_router.py:574-576`：`_clip(body[:MAX_INTERVENTION_STEPS_CHARS])` 改为 `_clip(body)`；先切片会让 `_clip` 判定"未超限"而不补 `…`。上限仍由 `_clip` 负责。
- 新增 `tests/test_sop_router.py::test_a_body_without_a_steps_section_says_that_it_was_cut`：无步骤小节的 2000 字正文，断言结尾为 `…` 且长度 ≤ 上限 + 1。

### P2-3 裸 catalog 不再包裹

- `ai_coding/ai/prompt.py:237-248`：`skills_catalog.strip()` 若不以 `<skills` 开头，仍包一层 `<skills>Available skills:\n…</skills>`（与整改前格式一致），保持向后兼容；已是完整块则原样追加，不二次包裹。
- 新增 `tests/test_prompt.py::test_a_bare_catalog_string_is_still_wrapped`：裸字符串输入恰好一对 `<skills>`/`</skills>`。

### 整改后验证

```bash
.venv/bin/python -m pytest tests/test_sop_router.py tests/test_skill_install.py \
  tests/test_skill_reference.py tests/test_prompt.py tests/test_skill_catalog.py -q
# 147 passed
.venv/bin/ruff check ai_coding tests/test_skill_install.py tests/test_sop_router.py
# All checks passed!
.venv/bin/mypy ai_coding
# Success: no issues found in 186 source files
```

另跑 `tests/test_m1b_agent_history.py`、`tests/test_context_compression_cache_friendliness.py`、`tests/test_phase_locking.py`、`tests/test_cross_domain_immune.py`（39 passed），确认 prompt 包裹改动未影响既有调用方。
