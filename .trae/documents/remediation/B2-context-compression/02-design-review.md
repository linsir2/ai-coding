# B2 上下文工程 + 压缩 方案评审

评审对象：`01-research.md`（14 项）
结论：**CHANGES**（阻塞项已裁决为强制修正，进入 S3）

## 阻塞项与裁决

| # | 阻塞项 | 裁决（S3 必须照此实现） |
|---|---|---|
| 1 | CMP-03 把 48k 窗口的保护值 40k→16k，属默认行为变更，方案未声明 | 保留公式 `max(8_000, min(40_000, window//3))`；在实现文档与代码注释明确声明"仅 <120k 窗口行为改变"，并跑全量确认 `test_microcompact_and_refresh` 的 3k 用例 |
| 2 | CMP-01 `_attach_reinjection` 放在 hard_trim 前会被裁掉 | 挂载点移到 hard_trim 之后、`_ensure_pinned` 之前；断言"快照在最终列表里" |
| 3 | CMP-01 force/no-op 时会给未变化的列表加快照，`/compact` 误报 compacted | 仅当 `out` 与输入内容不同时挂载；补 `/compact` 无变化用例 |
| 4 | CMP-01 两个既有测试未列入：`test_microcompact_and_refresh.py:257` 变恒真、`:299` 必失败 | 两处按新契约更新（299 改为经 `compress_if_needed` 后断言），并在实现文档说明 |
| 5 | CMP-05 观察器只在 `live>0` 调用，turn 起点不复位，跨 turn 继承 | 状态以 `(session_id, turn_count)` 为键；`turn_count` 变化即清零。不复位 session 字段 |
| 6 | CTX-03 加"约"会打破 `test_file_mentions.py:367` 的精确断言 | 同步更新该期望（允许近似），实现文档记录 |
| 7 | CTX-06 删 `cache_hit_ratio` 会破坏 `test_turn_reporter.py:157` | **改变裁决：CTX-06 不做字段删除**，只补 docstring 标注"诊断用途、无生产消费者"；不新增/不删除字段 |
| 8 | CTX-04 方案 B 仍改 `PLAN_TEXT_RULE`，违背"只做 skill 去重" | 只做 skill 去重（同一 id 已在 @mention 全文块时，SOP 推荐块跳过）；plan 文本不动 |

## 非阻塞（记录）

- CMP-05 慢增长下可能永不触发；10% 简单规则不安全（现有测试 135k estimate vs 11k live）。保留 5%+连续 3 次，并加注释说明误判代价。
- CMP-02 key 缺 window/protect/session：补这两个维度到 key；不写"同长度改写应命中"的测试（避免固化缺陷）。
- CMP-04 CJK 字符比较与 token 不完全等价：接受，注释说明。
- CTX-01 降级行需"超预算即停止继续添加"，不能只等长替换。
- CTX-07a 不可传 `""` 永久覆盖 reporter 的 tools_text：改为"本步跳过 tools 文本"的显式参数。
- CMP-01 每请求重读 ≤5 文件：接受（内容稳定，mtime 不影响）；文件集合变化会使前缀变化，注释说明。
- 恒真用例：CMP-01"仅一份"、CTX-01 目录 token 上界、CTX-04 未命中 router——改用例为可失败形态或标为护栏。

## 通过项

- CTX-02 stat 估算正确；CTX-07b/07c 无新依赖。
- CTX-05 双向截断用例可红转绿。
- CMP-04 新用例（161 字符 body + 长 elide_root）旧实现确会失败。
- CMP-01(a)(b) 首轮与 /compact 不丢快照。
- 无新增依赖。

## 进入 S3 的强制清单

CTX-01、02、03、04、05、07a、07b、07c、CMP-01、02、03、04、05（13 项）；CTX-06 降级为注释任务。
