# B3 Skill 代码评审（S5）

## 结论：APPROVE（第一轮 REQUEST-CHANGES，整改后通过）

## 第一轮 P0（已修）
**SKL-02 生产路径仍可绕过**：`install_from_path` 先 `.resolve()` 再扫软链，容器级软链（`repo/skills -> 宿主目录`）被解析后检测不到，copytree 会把宿主目录装进技能库（实测可复现）。
→ 整改：`resolve()` **之前**判 `is_symlink()`；`install_from_git` 拒绝 `candidate.is_symlink()`；新增生产调用点用例并断言宿主目录未被复制（`install.py:135-139,265`）。

## 第一轮 P2（已修）
1. 相似度在"两边皆空"时仍拿分（`_jaccard(∅,∅)=1.0`）→ 跳过空维度（`sop_router.py:280-289`）。
2. `body[:500]` 兜底无省略号 → `_clip` 补 `…`（`sop_router.py:576`）。
3. `prompt` 改为必须传完整 `<skills>` 块，裸字符串调用方丢标签 → 不以 `<skills` 开头时仍包一层（`prompt.py:247`）。

## 已核对通过
- SKL-01：`route` / `intervene` / `check_recovery` 三面均过滤；别名经 `denied_skill_ids` 归一；默认空集行为不变。
- SKL-03：描述 200 / 步骤与 `pattern_dna` 500 / @mention 截断且 `<skill_content>` 闭合。
- SKL-04：单层 `<skills>`；空目录无标签；`superseded_note` 在标签外（已声明）。
- SKL-05：`./`、`/`、`~`、`owner/repo` 四类判定正确；未改 `cli.py`。
- SKL-06：`signatures` 类型保护；空查询返回 `[]`；未知 phase 视为无限制、已知仍硬过滤。
- 无 `or True` 等恒真断言残留。

## 已知限制
- symlink 扫描与 `copytree` 之间仍有 TOCTOU 残余（复制后无法再扫，已文档化）。
- `./repo` 由"克隆"变为"直装当前工作树（含未提交文件）"。
