# B3 Skill 方案评审

结论：**CHANGES**（阻塞项已裁决，进入 S3）

## 阻塞项与裁决

| # | 阻塞 | 裁决（S3 必须照做） |
|---|---|---|
| 1 | SKL-02 只从候选目录根扫描，容器级软链（`repo/skills -> 宿主目录`）下失效 | `install_from_path` 先对 `source_path` 整体做 `_first_symlink`，通过后再展开候选；TOCTOU 残余写入已知限制（`symlinks=False` 复制后 staging 已无链接，复制后再扫无效） |
| 2 | SKL-06 Dendritic `from_dict` 对字符串/整数 `signatures` 抛 `AttributeError`/`TypeError`，接线后每请求崩 | `DendriticIndex.from_dict` 对 `signatures` 做 isinstance 保护；`match()` 在 tools/errors/keywords 全空时返回 `[]`（否则 `_jaccard` 空∩空=1.0 给所有技能 +2.0） |

## 非阻塞（采纳为附加要求）

- SKL-03 补第三条注入面 `pattern_dna`（`sop_router.py:495-500,534-535`）同样截断。
- SKL-06 **phase 不删除**：改为“加载时校验，未知 phase 视为无限制”，保留已知 phase 的硬过滤（删掉会让 `_compute_phase` 变死代码）。
- SKL-04 `superseded_note` 留在 `</skills>` 之外可接受；无技能时 `prompt` 已跳过空块，实现文档确认。
- SKL-05 写明 `./repo` 从"克隆指定提交"变为"直装当前工作树（含未提交文件）"。
- 测试：修正 `tests/test_skill_reference.py:179` 的 `or True` 恒真断言。

## 已确认无问题
- SKL-01 必须包含第五个注入面 `check_recovery`，否则每轮失败尾块仍注入被禁技能的 `pattern_dna`。
- SKL-03 在 `_skill_steps` 源头截断可保证 `<skill_content>` 闭合。
- SKL-05 在 `install_from_git` 内委托 `install_from_path`，`cli.py` 无需改动。
- 空 skills / 坏 frontmatter / 别名归一 / `./path` 不存在均有降级或明确报错。

## 进入 S3 清单
SKL-01 ~ SKL-06（SKL-06 仅 Dendritic + phase 校验）。
