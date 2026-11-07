# B4 编排 / 计划 方案评审

结论：**CHANGES**（阻塞项已裁决，进入 S3）

## 阻塞项与裁决

| # | 阻塞 | 裁决（S3 必须照做） |
|---|---|---|
| 1 | ORC-04 用 `_has_a_round_left` 判"最后一轮"是错的：C3 continuation 会让 `ceiling+1` 继续跑并读到该 nudge，现方案会砍掉 C3 续跑并把本应继续的回合改成 MAX_ITERATIONS | **不用 `_has_a_round_left`**。改为预测下一步决策：`self.policy.decide_next_iteration(replace(snapshot, iteration=index+1), budget_expired=...)`；只有返回 `FINISH` 时才不注入、把文本写入 `decision.note`（并以 `note or reason` 兜底）；返回 NUDGE/ITERATE 时照旧注入 |
| 2 | ORC-02 的 `level >= L3` 硬停会让 `max_strategy_nudges>3` 等价 3、负值静默关闭 | `cap = min(max(0, n), L3)` 并在注释写明；`n=0` 关闭；补充"显式 1/2 的用户会看到更少的 nudge"的行为变更说明 |

## 非阻塞（采纳）

- ORC-02 默认值 1→3：仅让默认配置与既有实际行为一致；同步 `config/models.py` 的字段注释（说明它是"级数上限"）。
- ORC-02：`strategy_nudges` 仍只作审计；补 `max_strategy_nudges=0/2` 用例。
- ORC-04：`decision.note` 为空的兜底为 `or reason`。
- ORC-05：只改 strategy 与 search 的 4 个精确位置（407/455/549/584），583 不动；token 预算 reason 不加后缀；**后缀**形式（`... [strategy ladder L4]`），因为 `domain/turn_digest.py:355-358` 用 `startswith`。
- ORC-03：阈值 guard 提前后，plan/readonly 下 repeat 会静默——补一条测试说明这是有意行为；`threshold=0` 仍保留 L3 硬停（见文档）。
- MCP-10a：采用"schema + catalog"加性统计，在配置注释说明默认 2000 会让大多数 catalog 触发告警；只在本批落地，B1 不再实现。
- ORC-01：注释措辞为"无**步数**与**墙钟**上限"（仍受窗口、阶梯、模型自停、goal 轮次约束）；补钉扎测试并标注为护栏。

## 已确认无问题
- 未改 `tests/golden/**`、未弱化 4 处精确断言、未改默认模型窗口。
- 默认 3 时 `test_loop_escalation`、`test_interventions_loop` 仍通过；`armed()` 时机不变。
- 后缀不破坏 `turn_digest` 的 `startswith` 语义。

## 待 W3 处理
`docs/features.md:853`、`docs/how-to-run.md:168`、`docs/data-contracts.md:576` 仍写 `max_strategy_nudges=1`，不在本批白名单，W3 统一更新。

## 进入 S3 清单
ORC-01（文档）、ORC-02、ORC-03、ORC-04、ORC-05、MCP-10a。
