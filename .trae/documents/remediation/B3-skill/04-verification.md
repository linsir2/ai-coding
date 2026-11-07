# B3 Skill 实现验证（S4）

## 结果
- 定向（sop_router / skill_install / skill_review / skill_reference / prompt / phase / catalog 等）→ 183 passed
- 全量 pytest → **exit 0**
- `ruff check` → 全绿；`mypy ai_coding` → 186 文件 0 错误

## 与 01/02 的一致性
- SKL-01：`denied` keyword-only；`route` / `intervene` / `check_recovery` 三面过滤；assembler 注入 `denied_skill_ids`。
- SKL-02：`install_from_path` 对整个 `source_path` 递归扫描（`followlinks=False`），命中即报相对路径。
- SKL-03：描述 200、steps/`pattern_dna` 500、@mention 按 `_FILE_TOKENS_CAP` 且保留闭合标签。
- SKL-04：prompt 不再二次包裹 `<skills>`。
- SKL-05：`_looks_local` 本地优先，`install_from_git` 委托 `install_from_path`；未改 `cli.py`。
- SKL-06：`match()` 空查询返回 `[]`；`signatures` 类型保护；phase 加载校验（未知值视为无限制，已知仍硬过滤）。

## 偏离（已接受）
- phase 采用"保留硬过滤 + 加载校验"，未删除（避免 `_compute_phase` 变死代码）。
- SKL-02 扫描放在 `source_path` 整体，而非在 `_inspect` 内重复扫描。

## 已知限制
- symlink 扫描与 `copytree` 之间仍有 TOCTOU 残余。
- `./repo` 从"克隆"变为"直装当前工作树（含未提交文件）"。
- @mention 注入量随文件数线性增长（单块有上限，总量无上限）。
